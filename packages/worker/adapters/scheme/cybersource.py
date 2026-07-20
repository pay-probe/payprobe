"""CyberSourceSimulator — a Visa Acceptance / CyberSource REST gateway simulator.

The REST counterpart of :class:`~worker.adapters.scheme.visa.VisaSimulator`:
where the VISA simulator answers ISO 8583 over a socket, this one answers
**CyberSource Payments REST** (`/pts/v2/...`) over HTTP — so PayProbe can stand
in for CyberSource during loopback testing of a RestPay-style integration with
no real gateway or merchant credentials.

It subclasses :class:`~worker.adapters.http.responder.HttpResponder` to inherit
all the HTTP lifecycle, live metrics and chaos/fault-injection machinery — only
the *routing* and the *reply decision* are CyberSource-specific. That also makes
it drop-in compatible with the orchestrator's saved-simulator registry and the
portal Simulators page, exactly like the payShield and VISA simulators.

Fidelity (read carefully)
--------------------------
This is a **functional** simulator of the *public* CyberSource REST contract:
the resource paths, the ``status`` value set (``AUTHORIZED`` /
``PARTIAL_AUTHORIZED`` / ``DECLINED`` / ``INVALID_REQUEST`` …), and the
``processorInformation`` / ``errorInformation`` envelope shape, as published in
CyberSource's REST reference and open-source client libraries. It is **not** a
byte-exact reproduction of every processor-specific ``responseCode`` /
``reason`` value — those depend on the merchant's configured processor. Pin
specific cards/amounts to specific outcomes via ``rules`` (evaluated first, they
win) and let everything else follow the gateway defaults here.

Supported flows
---------------
* **Authorization** ``POST /pts/v2/payments`` (optionally auth+capture when
  ``processingInformation.capture = true``)
* **Capture** ``POST /pts/v2/payments/{id}/captures``
* **Refund** ``POST /pts/v2/payments/{id}/refunds`` and
  ``POST /pts/v2/captures/{id}/refunds``
* **Auth reversal** ``POST /pts/v2/payments/{id}/reversals``
* **Void** ``POST /pts/v2/payments|captures|refunds/{id}/voids``
* **Retrieve** ``GET /pts/v2/payments/{id}``

Config (JSON-friendly)::

    {
      "protocol": "cybersource",
      "host": "0.0.0.0", "port": 8080,
      "cybersource": {
        "merchant_id": "payprobe_test",
        "require_auth": true,            # 401 unless a JWT or HTTP-Signature header is present
        "approve_response": "100",       # processorInformation.responseCode on approval
        "decline_over": 100000,          # orderInformation.amountDetails.totalAmount >= -> DECLINED
        "partial_over": 50000,           # totalAmount >= (and < decline_over) -> PARTIAL_AUTHORIZED
        "decline_response": "200",       # processorInformation.responseCode on decline
        "block_bins": ["400000"],        # card.number prefix -> DECLINED (DO_NOT_HONOR)
        "expiry_check": true,            # card expiry in the past -> DECLINED (EXPIRED_CARD)
        "cvn_decline_values": ["999"],   # card.securityCode match -> DECLINED (CVN_NOT_MATCH)
        "avs_code": "Y",                 # processorInformation.avs.code on approval
        "cvn_result": "M"                # processorInformation.cardVerification.resultCode
      },
      "rules": [ ... ],                  # explicit HTTP overrides win over the gateway logic
      "chaos": { ... }                   # fault injection
    }
"""

from __future__ import annotations

import logging
import random
import re
import string
from datetime import datetime, timezone
from typing import Any

from ..http.responder import HttpResponder, _dig

log = logging.getLogger(__name__)

# -- public CyberSource status values (PtsV2PaymentsPost201Response.status) -----
ST_AUTHORIZED = "AUTHORIZED"
ST_PARTIAL = "PARTIAL_AUTHORIZED"
ST_DECLINED = "DECLINED"
ST_INVALID = "INVALID_REQUEST"
ST_PENDING = "PENDING"  # captures / refunds
ST_REVERSED = "REVERSED"  # auth reversals
ST_VOIDED = "VOIDED"  # voids

# -- errorInformation.reason values (public, widely documented) -----------------
RE_PROCESSOR_DECLINED = "PROCESSOR_DECLINED"
RE_INSUFFICIENT_FUND = "INSUFFICIENT_FUND"
RE_EXPIRED_CARD = "EXPIRED_CARD"
RE_CVN_NOT_MATCH = "CVN_NOT_MATCH"
RE_INVALID_DATA = "INVALID_DATA"
RE_NOT_FOUND = "NOT_FOUND"
# follow-on lifecycle / state-transition reasons
RE_ALREADY_CAPTURED = "AUTH_ALREADY_CAPTURED"
RE_ALREADY_REVERSED = "AUTH_ALREADY_REVERSED"
RE_ALREADY_VOIDED = "TRANSACTION_ALREADY_VOIDED"
RE_NOT_CAPTURED = "AUTH_NOT_CAPTURED"
RE_DECLINED_PARENT = "INVALID_REQUEST"

# resource path: /pts/v2/{collection}/{id}/{sub}  (sub optional)
_PATH_RE = re.compile(
    r"^/pts/v2/(?P<coll>payments|captures|refunds)(?:/(?P<id>[^/]+)(?:/(?P<sub>[a-z]+))?)?/?$"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_id() -> str:
    """A CyberSource-style request id: a long numeric token."""
    return f"{random.randint(10**15, 10**16 - 1)}{random.randint(1000, 9999)}"


def _gen_approval() -> str:
    """A 6-char alphanumeric authorization/approval code."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _amount(parsed_body: Any) -> float | None:
    raw = _dig(parsed_body, "orderInformation.amountDetails.totalAmount")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class CyberSourceSimulator(HttpResponder):
    PROTOCOL = "cybersource"

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", self.PROTOCOL)
        super().__init__(config)
        cs = dict(config.get("cybersource") or {})
        self.merchant_id: str = str(cs.get("merchant_id", "payprobe_test"))
        self.require_auth: bool = bool(cs.get("require_auth", True))
        self.approve_response: str = str(cs.get("approve_response", "100"))
        self.decline_response: str = str(cs.get("decline_response", "200"))
        self.decline_over = cs.get("decline_over")
        self.partial_over = cs.get("partial_over")
        self.block_bins: list[str] = [str(b) for b in (cs.get("block_bins") or [])]
        self.expiry_check: bool = bool(cs.get("expiry_check", False))
        self.cvn_decline_values: list[str] = [str(v) for v in (cs.get("cvn_decline_values") or [])]
        self.avs_code: str = str(cs.get("avs_code", "Y"))
        self.cvn_result: str = str(cs.get("cvn_result", "M"))
        #: transaction ledger: id -> {status, amount, kind} for sub-resource lookups.
        self._txns: dict[str, dict] = {}

    # -- by-decision metric: bucket on the gateway status, not raw HTTP code ---
    def _response_code(self, action: dict) -> str:
        body = action.get("json") or {}
        return str(body.get("status") or action.get("status", 200))

    # -- routing + decision --------------------------------------------------

    def _resolve(self, parsed: dict) -> dict:
        # explicit HTTP rules win over gateway logic (same contract as VISA).
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})

        method = parsed.get("method", "GET")
        m = _PATH_RE.match(parsed.get("path", ""))
        if m is None:
            return self._error(404, ST_INVALID, RE_NOT_FOUND, "Unknown resource")

        coll, txn_id, sub = m.group("coll"), m.group("id"), m.group("sub")

        # credential gate (CyberSource: JWT bearer or HTTP-Signature headers)
        if self.require_auth and not self._has_auth(parsed.get("headers") or {}):
            return self._error(
                401,
                ST_INVALID,
                RE_INVALID_DATA,
                "Missing or invalid authentication credentials",
            )

        body = parsed.get("body")

        if method == "GET" and coll == "payments" and txn_id and not sub:
            return self._retrieve(txn_id)

        if method != "POST":
            return self._error(405, ST_INVALID, RE_INVALID_DATA, f"Method {method} not allowed")

        # POST /pts/v2/payments -> authorization (optionally auth+capture)
        if coll == "payments" and not txn_id:
            return self._authorize(parsed, body)

        # POST /pts/v2/{coll}/{id}/{sub} — follow-on services, lifecycle-checked
        if txn_id and sub:
            parent = self._txns.get(txn_id)
            if parent is None:
                return self._error(404, ST_INVALID, RE_NOT_FOUND, f"No transaction '{txn_id}'")
            if sub == "captures":
                return self._capture(parent, body)
            if sub == "reversals":
                return self._reversal(parent, body)
            if sub == "refunds":
                return self._refund(parent, body)
            if sub == "voids":
                return self._void(parent, body)

        return self._error(400, ST_INVALID, RE_INVALID_DATA, "Unsupported request")

    # -- authorization decision ----------------------------------------------

    def _authorize(self, parsed: dict, body: Any) -> dict:
        client_ref = _dig(body, "clientReferenceInformation.code")
        pan = str(_dig(body, "paymentInformation.card.number") or "")
        cvn = str(_dig(body, "paymentInformation.card.securityCode") or "")
        amount = _amount(body)
        capture = bool(_dig(body, "processingInformation.capture"))

        # --- decline ladder (first match wins) ---
        if any(pan.startswith(b) for b in self.block_bins):
            return self._decline(client_ref, RE_PROCESSOR_DECLINED, "Do not honor")
        if self.expiry_check and self._expired(body):
            return self._decline(client_ref, RE_EXPIRED_CARD, "Expired card")
        if cvn and cvn in self.cvn_decline_values:
            return self._decline(client_ref, RE_CVN_NOT_MATCH, "CVN does not match")
        if (
            self.decline_over is not None
            and amount is not None
            and amount >= float(self.decline_over)
        ):
            return self._decline(client_ref, RE_INSUFFICIENT_FUND, "Insufficient funds")

        # --- partial authorization ---
        if (
            self.partial_over is not None
            and amount is not None
            and amount >= float(self.partial_over)
        ):
            return self._approved(client_ref, ST_PARTIAL, amount, capture, partial=True)

        # --- full approval ---
        return self._approved(client_ref, ST_AUTHORIZED, amount, capture)

    def _approved(
        self,
        client_ref: Any,
        status: str,
        amount: float | None,
        capture: bool,
        partial: bool = False,
    ) -> dict:
        txn_id = _gen_id()
        # a partial auth approves a lower amount than requested.
        approved_amount = amount
        if partial and amount is not None:
            approved_amount = round(amount / 2, 2)
        self._txns[txn_id] = {
            "status": status,
            "amount": approved_amount,
            "kind": "auth",
            # an auth+capture in one call is born already captured.
            "captured": bool(capture),
            "reversed": False,
            "voided": False,
            "refunded": False,
        }
        proc: dict[str, Any] = {
            "approvalCode": _gen_approval(),
            "responseCode": self.approve_response,
            "avs": {"code": self.avs_code, "codeRaw": self.avs_code},
            "cardVerification": {"resultCode": self.cvn_result},
            "transactionId": txn_id,
            "networkTransactionId": _gen_id(),
        }
        order: dict[str, Any] = {}
        if approved_amount is not None:
            order = {
                "amountDetails": {
                    "authorizedAmount": f"{approved_amount:.2f}",
                    "currency": "USD",
                }
            }
        body: dict[str, Any] = {
            "id": txn_id,
            "status": status,
            "submitTimeUtc": _now_utc(),
            "reconciliationId": _gen_approval() + _gen_approval(),
            "clientReferenceInformation": {"code": client_ref} if client_ref else {},
            "processorInformation": proc,
            "orderInformation": order,
            "_links": {"self": {"href": f"/pts/v2/payments/{txn_id}", "method": "GET"}},
        }
        if capture:
            body["_links"]["capture"] = {  # type: ignore[index]
                "href": f"/pts/v2/payments/{txn_id}/captures",
                "method": "POST",
            }
        return {"status": 201, "json": body}

    def _decline(self, client_ref: Any, reason: str, message: str) -> dict:
        txn_id = _gen_id()
        self._txns[txn_id] = {
            "status": ST_DECLINED,
            "amount": None,
            "kind": "auth",
            "captured": False,
            "reversed": False,
            "voided": False,
            "refunded": False,
        }
        body = {
            "id": txn_id,
            "status": ST_DECLINED,
            "submitTimeUtc": _now_utc(),
            "clientReferenceInformation": {"code": client_ref} if client_ref else {},
            "processorInformation": {"responseCode": self.decline_response},
            "errorInformation": {"reason": reason, "message": message},
            "_links": {"self": {"href": f"/pts/v2/payments/{txn_id}", "method": "GET"}},
        }
        # CyberSource returns HTTP 201 for a processor decline (status DECLINED).
        return {"status": 201, "json": body}

    # -- follow-on services (capture / refund / reversal / void) -------------
    #
    # Each enforces the valid CyberSource state transitions: you capture an
    # un-captured authorization once; you reverse an un-captured authorization;
    # you refund a captured payment (or a capture); and you void a transaction
    # that has not already been voided/reversed. Invalid transitions return an
    # ``errorInformation`` with the relevant reason rather than silently
    # creating a follow-on against an impossible state.

    def _capture(self, parent: dict, body: Any) -> dict:
        if parent.get("kind") != "auth":
            return self._invalid(RE_INVALID_DATA, "Only an authorization can be captured")
        if parent.get("status") == ST_DECLINED:
            return self._invalid(RE_DECLINED_PARENT, "Cannot capture a declined authorization")
        if parent.get("voided"):
            return self._invalid(RE_ALREADY_VOIDED, "Authorization has been voided")
        if parent.get("reversed"):
            return self._invalid(RE_ALREADY_REVERSED, "Authorization has been reversed")
        if parent.get("captured"):
            return self._invalid(RE_ALREADY_CAPTURED, "Authorization has already been captured")
        parent["captured"] = True
        return self._service(body, ST_PENDING, "capture")

    def _reversal(self, parent: dict, body: Any) -> dict:
        if parent.get("kind") != "auth":
            return self._invalid(RE_INVALID_DATA, "Only an authorization can be reversed")
        if parent.get("status") == ST_DECLINED:
            return self._invalid(RE_DECLINED_PARENT, "Cannot reverse a declined authorization")
        if parent.get("voided"):
            return self._invalid(RE_ALREADY_VOIDED, "Authorization has been voided")
        if parent.get("reversed"):
            return self._invalid(RE_ALREADY_REVERSED, "Authorization has already been reversed")
        if parent.get("captured"):
            return self._invalid(
                RE_ALREADY_CAPTURED, "Cannot reverse a captured authorization; void the capture"
            )
        parent["reversed"] = True
        return self._service(body, ST_REVERSED, "reversal")

    def _refund(self, parent: dict, body: Any) -> dict:
        kind = parent.get("kind")
        if kind == "auth":
            if parent.get("status") == ST_DECLINED:
                return self._invalid(RE_DECLINED_PARENT, "Cannot refund a declined authorization")
            if not parent.get("captured"):
                return self._invalid(
                    RE_NOT_CAPTURED, "Cannot refund an authorization before capture"
                )
        elif kind == "capture":
            if parent.get("voided"):
                return self._invalid(RE_ALREADY_VOIDED, "Capture has been voided")
        else:
            return self._invalid(RE_INVALID_DATA, "Only a captured payment can be refunded")
        parent["refunded"] = True
        return self._service(body, ST_PENDING, "refund")

    def _void(self, parent: dict, body: Any) -> dict:
        if parent.get("voided"):
            return self._invalid(RE_ALREADY_VOIDED, "Transaction has already been voided")
        if parent.get("reversed"):
            return self._invalid(RE_ALREADY_REVERSED, "Transaction has been reversed")
        if parent.get("status") == ST_DECLINED:
            return self._invalid(RE_DECLINED_PARENT, "Cannot void a declined transaction")
        parent["voided"] = True
        return self._service(body, ST_VOIDED, "void")

    def _service(self, body: Any, status: str, kind: str) -> dict:
        """Build and ledger a follow-on service record (the child transaction)."""
        txn_id = _gen_id()
        client_ref = _dig(body, "clientReferenceInformation.code")
        amount = _amount(body)
        self._txns[txn_id] = {
            "status": status,
            "amount": amount,
            "kind": kind,
            "captured": False,
            "reversed": kind == "reversal",
            "voided": kind == "void",
            "refunded": False,
        }
        out: dict[str, Any] = {
            "id": txn_id,
            "status": status,
            "submitTimeUtc": _now_utc(),
            "reconciliationId": _gen_approval() + _gen_approval(),
            "clientReferenceInformation": {"code": client_ref} if client_ref else {},
            "_links": {"self": {"href": f"/pts/v2/{kind}s/{txn_id}", "method": "GET"}},
        }
        if amount is not None:
            out["orderInformation"] = {
                "amountDetails": {"totalAmount": f"{amount:.2f}", "currency": "USD"}
            }
        return {"status": 201, "json": out}

    def _invalid(self, reason: str, message: str) -> dict:
        """A follow-on rejected because the parent is in an incompatible state."""
        return self._error(400, ST_INVALID, reason, message)

    # -- retrieve ------------------------------------------------------------

    def _retrieve(self, txn_id: str) -> dict:
        txn = self._txns.get(txn_id)
        if txn is None:
            return self._error(404, ST_INVALID, RE_NOT_FOUND, f"No transaction '{txn_id}'")
        body: dict[str, Any] = {
            "id": txn_id,
            "status": txn.get("status"),
            "submitTimeUtc": _now_utc(),
        }
        if txn.get("amount") is not None:
            body["orderInformation"] = {
                "amountDetails": {"totalAmount": f"{txn['amount']:.2f}", "currency": "USD"}
            }
        return {"status": 200, "json": body}

    # -- helpers -------------------------------------------------------------

    def _error(self, http_status: int, status: str, reason: str, message: str) -> dict:
        return {
            "status": http_status,
            "json": {
                "submitTimeUtc": _now_utc(),
                "status": status,
                "reason": reason,
                "message": message,
                "errorInformation": {"reason": reason, "message": message},
            },
        }

    def _has_auth(self, headers: dict) -> bool:
        """CyberSource accepts a JWT (``Authorization: Bearer``) or HTTP
        Signature (``signature`` + ``v-c-merchant-id``) credential."""
        if str(headers.get("authorization", "")).strip():
            return True
        return bool(headers.get("signature") and headers.get("v-c-merchant-id"))

    def _expired(self, body: Any) -> bool:
        yr = _dig(body, "paymentInformation.card.expirationYear")
        mo = _dig(body, "paymentInformation.card.expirationMonth")
        if yr is None or mo is None:
            return False
        try:
            y, m = int(yr), int(mo)
        except (TypeError, ValueError):
            return False
        now = datetime.now(timezone.utc)
        return (y, m) < (now.year, now.month)
