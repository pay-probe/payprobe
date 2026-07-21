"""PayPalOrdersSimulator — a PayPal v2 Checkout Orders simulator (+ OAuth2).

The third PSP simulator of ADR-0009, covering the integration shape the other
two don't: **OAuth2 client-credentials**. It serves PayPal's token endpoint
(``POST /v1/oauth2/token``) alongside the Orders API, so the generic ``http``
adapter's ``oauth2_client_credentials`` strategy is exercised end-to-end
against the sim — token exchange, caching, Bearer attach — with no real PayPal
account. The ``external: true`` guardrail keeps load off the real sandbox;
this is the thing that is safe to hammer.

Subclasses :class:`~worker.adapters.http.responder.HttpResponder` for
lifecycle/metrics/chaos (the CyberSource/Stripe/Adyen precedent).

Fidelity (read carefully)
--------------------------
Functional simulator of the *public* v2 Orders contract: the order status
machine (``CREATED`` → ``COMPLETED``), capture results embedded under
``purchase_units[].payments.captures[]``, refunds against a capture, and the
error envelope (``name`` / ``message`` / ``details[].issue`` / ``debug_id``) —
including the classic ``INSTRUMENT_DECLINED`` 422 on capture, which is what a
merchant's retry logic must handle. **PayPal's documented negative-testing
header is honored**: ``PayPal-Mock-Response: {"mock_application_codes":
"INSTRUMENT_DECLINED"}`` forces that issue on capture/refund, exactly like the
real sandbox — so pack cases written against the sim run unchanged against
PayPal's sandbox. Buyer approval is deliberately skipped
(there is no shopper here): a created order is immediately capturable.
Not byte-exact. Pin extra outcomes via ``rules`` (evaluated first).

Supported flows
---------------
* **Token** ``POST /v1/oauth2/token`` (client_credentials; Basic or body creds)
* **Create** ``POST /v2/checkout/orders`` → 201 ``CREATED``
* **Retrieve** ``GET /v2/checkout/orders/{id}``
* **Capture** ``POST /v2/checkout/orders/{id}/capture`` → ``COMPLETED``,
  or 422 ``INSTRUMENT_DECLINED`` via the ``decline_over`` trigger
* **Refund** ``POST /v2/payments/captures/{id}/refund`` (full/partial,
  over-refund → 422 ``REFUND_AMOUNT_EXCEEDED`` / ``CAPTURE_FULLY_REFUNDED``)

Config (JSON-friendly)::

    {
      "protocol": "paypal",
      "host": "0.0.0.0", "port": 8087,
      "paypal": {
        "require_auth": true,     # Bearer required on /v2/*
        "strict_tokens": false,   # true -> only tokens THIS sim issued pass
        "decline_over": null      # amount.value >= -> INSTRUMENT_DECLINED on capture
      },
      "rules": [ ... ],
      "chaos": { ... }
    }
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import time
from typing import Any

from ..http.responder import HttpResponder, _dig
from ..http.webhooks import paypal_transmission_headers

log = logging.getLogger(__name__)

ST_CREATED = "CREATED"
ST_COMPLETED = "COMPLETED"

_TOKEN_RE = re.compile(r"^/v1/oauth2/token/?$")
_ORDERS_RE = re.compile(r"^/v2/checkout/orders(?:/(?P<id>[^/]+)(?:/(?P<sub>capture))?)?/?$")
_REFUND_RE = re.compile(r"^/v2/payments/captures/(?P<id>[^/]+)/refund/?$")


def _gen_id(n: int = 17) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _money(amount: Any) -> float | None:
    try:
        return float(str(_dig(amount, "value")))
    except (TypeError, ValueError):
        return None


class PayPalOrdersSimulator(HttpResponder):
    PROTOCOL = "paypal"

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", self.PROTOCOL)
        super().__init__(config)
        pp = dict(config.get("paypal") or {})
        self.require_auth: bool = bool(pp.get("require_auth", True))
        self.strict_tokens: bool = bool(pp.get("strict_tokens", False))
        self.decline_over = pp.get("decline_over")
        self._tokens: set[str] = set()
        #: order ledger: id -> {status, purchase_units, intent}
        self._orders: dict[str, dict] = {}
        #: capture ledger: id -> {amount value, currency, refunded}
        self._captures: dict[str, dict] = {}
        #: refund ledger: id -> public refund doc
        self._refunds: dict[str, dict] = {}

    # -- by-decision metric ----------------------------------------------------
    def _response_code(self, action: dict) -> str:
        body = action.get("json") or {}
        details = body.get("details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            return str(details[0].get("issue") or body.get("name"))
        if body.get("name"):
            return str(body["name"])
        return str(body.get("status") or action.get("status", 200))

    # -- routing ---------------------------------------------------------------

    def _resolve(self, parsed: dict) -> dict:
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})

        method = parsed.get("method", "GET")
        path = parsed.get("path", "")
        headers = parsed.get("headers") or {}
        body = parsed.get("body") if isinstance(parsed.get("body"), dict) else {}

        if _TOKEN_RE.match(path):
            return self._token(method, parsed)

        if self.require_auth:
            auth = str(headers.get("authorization") or "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if not token or (self.strict_tokens and token not in self._tokens):
                return {
                    "status": 401,
                    "json": {
                        "name": "AUTHENTICATION_FAILURE",
                        "message": "Authentication failed due to invalid authentication "
                        "credentials or a missing Authorization header.",
                    },
                }

        # PayPal's documented sandbox negative-testing mechanism: a
        # PayPal-Mock-Response header with {"mock_application_codes": "..."}
        # forces that business error on the mutating call — supported here
        # exactly so packs/tests written against the sim also work against the
        # real sandbox.
        mock_issue = self._mock_issue(headers)

        m = _ORDERS_RE.match(path)
        if m:
            oid, sub = m.group("id"), m.group("sub")
            if method == "POST" and not oid:
                return self._create_order(body)
            if method == "GET" and oid and not sub:
                return self._get_order(oid)
            if method == "POST" and oid and sub == "capture":
                if mock_issue:
                    return self._mock_error(mock_issue)
                return self._capture(oid)
            return self._err(
                405,
                "METHOD_NOT_SUPPORTED",
                "The server does not " "implement the requested HTTP method.",
            )
        m = _REFUND_RE.match(path)
        if m:
            if method != "POST":
                return self._err(
                    405,
                    "METHOD_NOT_SUPPORTED",
                    "The server does not " "implement the requested HTTP method.",
                )
            if mock_issue:
                return self._mock_error(mock_issue)
            return self._refund(m.group("id"), body)
        return self._err(
            404,
            "RESOURCE_NOT_FOUND",
            "The specified resource " "does not exist.",
            issue="INVALID_RESOURCE_ID",
        )

    # -- webhook emission (ADR-0009 phase 4) -----------------------------------

    def _emit_event(self, event_type: str, resource: dict) -> None:
        """Emit one PayPal-shaped webhook event (fire-and-forget) with the
        transmission headers. The signature is an HMAC stand-in labelled
        ``Paypal-Auth-Algo: HMAC-SHA256`` — PayPal's real scheme is cert-based
        and verified via their API, which an offline sim cannot reproduce."""
        if not self.webhooks.enabled:
            return
        event = {
            "id": f"WH-{_gen_id(14)}",
            "event_version": "1.0",
            "event_type": event_type,
            "resource_type": "capture" if "CAPTURE" in event_type else "refund",
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "resource": resource,
        }
        body = json.dumps(event)
        headers = paypal_transmission_headers(
            self.webhooks.secret,
            transmission_id=_gen_id(24).lower(),
            transmission_time=event["create_time"],
            payload=body,
        )
        self.webhooks.emit(event_type, body, headers)

    # -- /v1/oauth2/token ------------------------------------------------------

    def _token(self, method: str, parsed: dict) -> dict:
        if method != "POST":
            return self._err(
                405,
                "METHOD_NOT_SUPPORTED",
                "The server does not implement the requested HTTP method.",
            )
        headers = parsed.get("headers") or {}
        raw = str(parsed.get("raw") or "")
        body = parsed.get("body")
        has_basic = str(headers.get("authorization") or "").startswith("Basic ")
        has_body_creds = "client_id=" in raw or (isinstance(body, dict) and body.get("client_id"))
        if not (has_basic or has_body_creds):
            return {
                "status": 401,
                "json": {
                    "error": "invalid_client",
                    "error_description": "Client Authentication failed",
                },
            }
        token = f"A21.{_gen_id(40)}"
        self._tokens.add(token)
        return {
            "status": 200,
            "json": {
                "scope": "https://uri.paypal.com/services/payments/payment",
                "access_token": token,
                "token_type": "Bearer",
                "app_id": "APP-80W284485P519543T",
                "expires_in": 32400,
                "nonce": _gen_id(24),
            },
        }

    # -- orders ----------------------------------------------------------------

    def _create_order(self, body: dict) -> dict:
        units = body.get("purchase_units")
        if not isinstance(units, list) or not units or _money(units[0].get("amount")) is None:
            return self._err(
                400,
                "INVALID_REQUEST",
                "Request is not well-formed, syntactically incorrect, or " "violates schema.",
                issue="MISSING_REQUIRED_PARAMETER",
                field="/purchase_units/@reference_id=='default'/amount",
            )
        oid = _gen_id()
        self._orders[oid] = {
            "status": ST_CREATED,
            "intent": str(body.get("intent") or "CAPTURE"),
            "purchase_units": units,
        }
        return {"status": 201, "json": self._order_doc(oid)}

    def _get_order(self, oid: str) -> dict:
        if oid not in self._orders:
            return self._not_found()
        return {"status": 200, "json": self._order_doc(oid)}

    def _capture(self, oid: str) -> dict:
        order = self._orders.get(oid)
        if order is None:
            return self._not_found()
        if order["status"] == ST_COMPLETED:
            return self._err(
                422,
                "UNPROCESSABLE_ENTITY",
                "The requested action could not be performed, semantically "
                "incorrect, or failed business validation.",
                issue="ORDER_ALREADY_CAPTURED",
            )
        amount = (order["purchase_units"][0] or {}).get("amount") or {}
        value = _money(amount)
        if (
            self.decline_over is not None
            and value is not None
            and value >= float(self.decline_over)
        ):
            return self._err(
                422,
                "UNPROCESSABLE_ENTITY",
                "The requested action could not be performed, semantically "
                "incorrect, or failed business validation.",
                issue="INSTRUMENT_DECLINED",
                description="The instrument presented was either declined by "
                "the processor or bank, or it can't be used for "
                "this payment.",
            )
        order["status"] = ST_COMPLETED
        cap_id = _gen_id()
        self._captures[cap_id] = {
            "value": value or 0.0,
            "currency": str(_dig(amount, "currency_code") or "USD"),
            "refunded": 0.0,
        }
        order["capture_id"] = cap_id
        self._emit_event(
            "PAYMENT.CAPTURE.COMPLETED",
            {
                "id": cap_id,
                "status": ST_COMPLETED,
                "amount": {
                    "currency_code": self._captures[cap_id]["currency"],
                    "value": f"{self._captures[cap_id]['value']:.2f}",
                },
                "final_capture": True,
            },
        )
        return {"status": 201, "json": self._order_doc(oid)}

    def _order_doc(self, oid: str) -> dict:
        order = self._orders[oid]
        units = []
        for i, u in enumerate(order["purchase_units"]):
            unit = {
                "reference_id": (u or {}).get("reference_id", "default"),
                "amount": (u or {}).get("amount"),
            }
            if order["status"] == ST_COMPLETED and i == 0 and order.get("capture_id"):
                cap = self._captures[order["capture_id"]]
                unit["payments"] = {
                    "captures": [
                        {
                            "id": order["capture_id"],
                            "status": ST_COMPLETED,
                            "amount": {
                                "currency_code": cap["currency"],
                                "value": f"{cap['value']:.2f}",
                            },
                            "final_capture": True,
                        }
                    ]
                }
            units.append(unit)
        base = f"/v2/checkout/orders/{oid}"
        return {
            "id": oid,
            "intent": order["intent"],
            "status": order["status"],
            "purchase_units": units,
            "links": [
                {"href": base, "rel": "self", "method": "GET"},
                {"href": f"{base}/capture", "rel": "capture", "method": "POST"},
            ],
        }

    # -- refunds ---------------------------------------------------------------

    def _refund(self, cap_id: str, body: dict) -> dict:
        cap = self._captures.get(cap_id)
        if cap is None:
            return self._not_found()
        remaining = round(cap["value"] - cap["refunded"], 2)
        if remaining <= 0:
            return self._err(
                422,
                "UNPROCESSABLE_ENTITY",
                "The requested action could not be performed, semantically "
                "incorrect, or failed business validation.",
                issue="CAPTURE_FULLY_REFUNDED",
            )
        amount = _money(body.get("amount")) if body.get("amount") else remaining
        if amount is None or amount <= 0 or round(amount, 2) > remaining:
            return self._err(
                422,
                "UNPROCESSABLE_ENTITY",
                "The requested action could not be performed, semantically "
                "incorrect, or failed business validation.",
                issue="REFUND_AMOUNT_EXCEEDED",
            )
        cap["refunded"] = round(cap["refunded"] + amount, 2)
        doc = {
            "id": _gen_id(),
            "status": ST_COMPLETED,
            "amount": {"currency_code": cap["currency"], "value": f"{amount:.2f}"},
            "links": [{"href": f"/v2/payments/captures/{cap_id}", "rel": "up", "method": "GET"}],
        }
        self._refunds[doc["id"]] = doc
        self._emit_event("PAYMENT.CAPTURE.REFUNDED", doc)
        return {"status": 201, "json": doc}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _mock_issue(headers: dict) -> str | None:
        raw = headers.get("paypal-mock-response")
        if not raw:
            return None
        try:
            code = json.loads(raw).get("mock_application_codes")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
        return str(code).upper() if code else None

    def _mock_error(self, issue: str) -> dict:
        description = None
        if issue == "INSTRUMENT_DECLINED":
            description = (
                "The instrument presented was either declined by the "
                "processor or bank, or it can't be used for this payment."
            )
        return self._err(
            422,
            "UNPROCESSABLE_ENTITY",
            "The requested action could not be performed, semantically "
            "incorrect, or failed business validation.",
            issue=issue,
            description=description,
        )

    def _not_found(self) -> dict:
        return self._err(
            404,
            "RESOURCE_NOT_FOUND",
            "The specified resource does not exist " "or cannot be found.",
            issue="INVALID_RESOURCE_ID",
        )

    @staticmethod
    def _err(
        status: int,
        name: str,
        message: str,
        issue: str | None = None,
        description: str | None = None,
        field: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "message": message, "debug_id": _gen_id(12).lower()}
        if issue:
            detail: dict[str, Any] = {"issue": issue}
            if description:
                detail["description"] = description
            if field:
                detail["field"] = field
            body["details"] = [detail]
        return {"status": status, "json": body}


__all__ = ["PayPalOrdersSimulator"]
