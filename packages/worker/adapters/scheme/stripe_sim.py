"""StripeSimulator — a Stripe Payments REST simulator (PaymentIntents + Refunds).

The PSP counterpart of :class:`~worker.adapters.scheme.cybersource.CyberSourceSimulator`
(ADR-0009): where that one answers CyberSource's ``/pts/v2`` contract, this one
answers **Stripe's PaymentIntents API** (``/v1/payment_intents``, ``/v1/refunds``)
— so PayProbe can stand in for Stripe during loopback testing of a merchant
integration with no real account, and the chaos/load machinery can storm a
"Stripe" that is safe to break (the ``external: true`` guardrail forbids doing
that to the real sandbox).

It subclasses :class:`~worker.adapters.http.responder.HttpResponder` to inherit
all the HTTP lifecycle, live metrics and chaos/fault-injection machinery — only
the *routing*, the *body decoding* and the *reply decision* are Stripe-specific,
which keeps it drop-in compatible with the orchestrator's saved-simulator
registry and the portal Simulators page.

Fidelity (read carefully)
--------------------------
This is a **functional** simulator of the *public* Stripe contract: the
resource paths, the PaymentIntent ``status`` machine (``requires_payment_method``
/ ``requires_confirmation`` / ``requires_action`` / ``requires_capture`` /
``succeeded`` / ``canceled``), the ``error`` envelope (``type`` / ``code`` /
``decline_code``), **form-encoded request bodies with bracket notation** (the
encoding Stripe actually requires — JSON is tolerated here for convenience but
the packed scenarios send form), and the **documented test-card ladder**
(``4242…4242`` succeeds, ``…0002`` declines, ``…9995`` insufficient funds,
``…3155`` requires 3DS action, and so on — the same numbers Stripe's own test
mode reacts to). It is *not* a byte-exact reproduction of every field of a real
PaymentIntent. Pin extra cards/amounts to outcomes via ``rules`` (evaluated
first, they win) and let everything else follow the gateway logic here.

Supported flows
---------------
* **Create** ``POST /v1/payment_intents`` (``confirm=true`` decides immediately;
  ``capture_method=manual`` parks an approval in ``requires_capture``)
* **Confirm** ``POST /v1/payment_intents/{id}/confirm``
* **Capture** ``POST /v1/payment_intents/{id}/capture``
* **Cancel**  ``POST /v1/payment_intents/{id}/cancel``
* **Retrieve** ``GET /v1/payment_intents/{id}`` and ``GET /v1/refunds/{id}``
* **Refund** ``POST /v1/refunds`` (full or partial by ``amount``)

Config (JSON-friendly)::

    {
      "protocol": "stripe",
      "host": "0.0.0.0", "port": 8085,
      "stripe": {
        "require_auth": true,     # 401 unless an Authorization header is present
        "decline_over": null,     # amount (minor units) >= -> insufficient_funds
        "default_currency": "usd"
      },
      "rules": [ ... ],           # explicit HTTP overrides win over gateway logic
      "chaos": { ... }            # fault injection (drop / latency / malformed)
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
from urllib.parse import parse_qs

from ..http.responder import HttpResponder, _dig
from ..http.webhooks import stripe_signature

log = logging.getLogger(__name__)

# -- PaymentIntent statuses (the public state machine) --------------------------
ST_REQUIRES_PM = "requires_payment_method"
ST_REQUIRES_CONFIRM = "requires_confirmation"
ST_REQUIRES_ACTION = "requires_action"
ST_REQUIRES_CAPTURE = "requires_capture"
ST_SUCCEEDED = "succeeded"
ST_CANCELED = "canceled"

# -- documented test cards (the same PANs Stripe's test mode reacts to) ---------
#: PAN -> ("ok" | "action") or (code, decline_code, message)
_CARD_LADDER: dict[str, Any] = {
    "4242424242424242": "ok",
    "4000056655665556": "ok",  # Visa debit
    "5555555555554444": "ok",  # Mastercard
    "4000000000000002": ("card_declined", "generic_decline", "Your card was declined."),
    "4000000000009995": (
        "card_declined",
        "insufficient_funds",
        "Your card has insufficient funds.",
    ),
    "4000000000009987": ("card_declined", "lost_card", "Your card was declined."),
    "4000000000009979": ("card_declined", "stolen_card", "Your card was declined."),
    "4000000000000069": ("expired_card", None, "Your card has expired."),
    "4000000000000127": ("incorrect_cvc", None, "Your card's security code is incorrect."),
    "4000000000000119": (
        "processing_error",
        None,
        "An error occurred while processing your card.",
    ),
    "4000002500003155": "action",  # 3DS authentication required
    "4000002760003184": "action",
}

#: payment-method test tokens -> the PAN whose ladder entry decides the outcome
_PM_TOKENS: dict[str, str] = {
    "pm_card_visa": "4242424242424242",
    "pm_card_visa_debit": "4000056655665556",
    "pm_card_mastercard": "5555555555554444",
    "pm_card_chargeDeclined": "4000000000000002",
    "pm_card_chargeDeclinedInsufficientFunds": "4000000000009995",
    "pm_card_chargeDeclinedExpiredCard": "4000000000000069",
    "pm_card_chargeDeclinedIncorrectCvc": "4000000000000127",
    "pm_card_chargeDeclinedProcessingError": "4000000000000119",
    "pm_card_threeDSecure2Required": "4000002500003155",
}

_PATH_RE = re.compile(
    r"^/v1/(?P<coll>payment_intents|refunds)(?:/(?P<id>[^/]+)(?:/(?P<sub>[a-z_]+))?)?/?$"
)


def _gen_id(prefix: str) -> str:
    body = "".join(random.choices(string.ascii_letters + string.digits, k=24))
    return f"{prefix}_{body}"


def _unflatten_form(raw: str) -> dict:
    """Decode a url-encoded body with bracket notation into a nested dict —
    the inverse of :func:`worker.engine.http_runner.form_flatten`:
    ``card[number]=4242&items[0][price]=5`` →
    ``{"card": {"number": "4242"}, "items": {"0": {"price": "5"}}}``.
    Digit segments stay dict keys ("0"), which dotted-path matching digs fine.
    """
    root: dict = {}
    for key, values in parse_qs(raw, keep_blank_values=True).items():
        parts = re.findall(r"[^\[\]]+", key)
        if not parts:
            continue
        cur = root
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = values[-1]
    return root


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class StripeSimulator(HttpResponder):
    PROTOCOL = "stripe"

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", self.PROTOCOL)
        super().__init__(config)
        st = dict(config.get("stripe") or {})
        self.require_auth: bool = bool(st.get("require_auth", True))
        self.decline_over = st.get("decline_over")
        self.default_currency: str = str(st.get("default_currency", "usd"))
        #: intent ledger: id -> mutable state for follow-on calls
        self._intents: dict[str, dict] = {}
        #: refund ledger: id -> public refund doc
        self._refunds: dict[str, dict] = {}

    # -- by-decision metric: bucket on the intent status / error code ----------
    def _response_code(self, action: dict) -> str:
        body = action.get("json") or {}
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("decline_code") or err.get("code") or err.get("type"))
        return str(body.get("status") or action.get("status", 200))

    # -- decode: Stripe bodies are form-encoded (JSON tolerated) ---------------

    async def _decode(self, request) -> dict:
        parsed = await super()._decode(request)
        body, raw = parsed.get("body"), parsed.get("raw") or ""
        ctype = (parsed.get("headers") or {}).get("content-type", "")
        looks_form = "form-urlencoded" in ctype or (
            isinstance(body, str) and "=" in body and not body.lstrip().startswith("{")
        )
        if looks_form and raw:
            try:
                parsed["body"] = _unflatten_form(raw)
            except Exception:  # noqa: BLE001 — keep the raw string view
                log.debug("stripe sim: could not parse form body", exc_info=True)
        return parsed

    # -- routing + decision ----------------------------------------------------

    def _resolve(self, parsed: dict) -> dict:
        # explicit HTTP rules win over gateway logic (same contract as the
        # VISA / CyberSource simulators).
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})

        if self.require_auth and not (parsed.get("headers") or {}).get("authorization"):
            return self._err(
                401,
                "invalid_request_error",
                "You did not provide an API key.",
            )

        method = parsed.get("method", "GET")
        m = _PATH_RE.match(parsed.get("path", ""))
        if m is None:
            return self._err(404, "invalid_request_error", "Unknown request URL")
        coll, obj_id, sub = m.group("coll"), m.group("id"), m.group("sub")
        body = parsed.get("body") if isinstance(parsed.get("body"), dict) else {}

        if coll == "payment_intents":
            if method == "GET" and obj_id and not sub:
                return self._retrieve_intent(obj_id)
            if method != "POST":
                return self._err(405, "invalid_request_error", f"Method {method} not allowed")
            if not obj_id:
                return self._create_intent(body)
            state = self._intents.get(obj_id)
            if state is None:
                return self._missing(obj_id)
            if sub == "confirm":
                return self._confirm(state, body)
            if sub == "capture":
                return self._capture(state, body)
            if sub == "cancel":
                return self._cancel(state)
            return self._err(400, "invalid_request_error", f"Unknown action '{sub}'")

        # refunds
        if method == "GET" and obj_id:
            doc = self._refunds.get(obj_id)
            if doc is None:
                return self._missing(obj_id)
            return {"status": 200, "json": doc}
        if method == "POST" and not obj_id:
            return self._create_refund(body)
        return self._err(400, "invalid_request_error", "Unsupported request")


    # -- webhook emission (ADR-0009 phase 4) -----------------------------------

    def _emit_event(self, event_type: str, obj: dict) -> None:
        """Emit one Stripe-shaped, Stripe-signed event (fire-and-forget): the
        documented envelope with the intent/refund under ``data.object`` and
        the ``t=…,v1=HMAC`` signature over the exact bytes sent."""
        if not self.webhooks.enabled:
            return
        event = {
            "id": _gen_id("evt"),
            "object": "event",
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": obj},
        }
        body = json.dumps(event)
        self.webhooks.emit(
            event_type, body,
            {"Stripe-Signature": stripe_signature(self.webhooks.secret, body)},
        )

    # -- create / confirm ------------------------------------------------------

    def _create_intent(self, body: dict) -> dict:
        amount = _to_int(_dig(body, "amount"))
        if amount is None or amount <= 0:
            return self._err(
                400, "invalid_request_error", "Missing required param: amount.", code="parameter_missing"
            )
        state = {
            "id": _gen_id("pi"),
            "amount": amount,
            "currency": str(_dig(body, "currency") or self.default_currency),
            "capture_method": str(_dig(body, "capture_method") or "automatic"),
            "pan": self._pan_of(body),
            "status": ST_REQUIRES_PM,
            "amount_received": 0,
            "refunded": 0,
            "last_payment_error": None,
            "latest_charge": None,
        }
        self._intents[state["id"]] = state
        if state["pan"]:
            state["status"] = ST_REQUIRES_CONFIRM
        if str(_dig(body, "confirm")).lower() == "true":
            return self._confirm(state, body)
        return {"status": 200, "json": self._intent_doc(state)}

    def _confirm(self, state: dict, body: dict) -> dict:
        if state["status"] in (ST_SUCCEEDED, ST_CANCELED):
            return self._err(
                400,
                "invalid_request_error",
                f"This PaymentIntent's state is {state['status']}; it cannot be confirmed.",
                code="payment_intent_unexpected_state",
            )
        pan = self._pan_of(body) or state.get("pan") or ""
        state["pan"] = pan
        if not pan:
            return self._err(
                400,
                "invalid_request_error",
                "You cannot confirm this PaymentIntent because it's missing a payment method.",
                code="payment_intent_unexpected_state",
            )

        verdict = _CARD_LADDER.get(pan, "ok")
        amount = state["amount"]
        if (
            verdict == "ok"
            and self.decline_over is not None
            and amount >= int(self.decline_over)
        ):
            verdict = (
                "card_declined",
                "insufficient_funds",
                "Your card has insufficient funds.",
            )

        if verdict == "action":
            state["status"] = ST_REQUIRES_ACTION
            doc = self._intent_doc(state)
            doc["next_action"] = {"type": "use_stripe_sdk"}
            return {"status": 200, "json": doc}

        if verdict != "ok":
            code, decline_code, message = verdict
            state["status"] = ST_REQUIRES_PM
            state["last_payment_error"] = {
                "type": "card_error",
                "code": code,
                **({"decline_code": decline_code} if decline_code else {}),
                "message": message,
            }
            self._emit_event("payment_intent.payment_failed",
                             self._intent_doc(state))
            # Stripe answers a declined confirm with HTTP 402 and the intent
            # embedded in the error, so the merchant can inspect its state.
            return self._err(
                402,
                "card_error",
                message,
                code=code,
                decline_code=decline_code,
                intent=self._intent_doc(state),
            )

        state["last_payment_error"] = None
        state["latest_charge"] = state["latest_charge"] or _gen_id("ch")
        if state["capture_method"] == "manual":
            state["status"] = ST_REQUIRES_CAPTURE
            self._emit_event("payment_intent.amount_capturable_updated",
                             self._intent_doc(state))
        else:
            state["status"] = ST_SUCCEEDED
            state["amount_received"] = amount
            self._emit_event("payment_intent.succeeded", self._intent_doc(state))
        return {"status": 200, "json": self._intent_doc(state)}

    # -- follow-ons ------------------------------------------------------------

    def _capture(self, state: dict, body: dict) -> dict:
        if state["status"] != ST_REQUIRES_CAPTURE:
            return self._err(
                400,
                "invalid_request_error",
                f"This PaymentIntent's state is {state['status']}; only "
                "requires_capture intents can be captured.",
                code="payment_intent_unexpected_state",
            )
        amount = _to_int(_dig(body, "amount_to_capture")) or state["amount"]
        state["status"] = ST_SUCCEEDED
        state["amount_received"] = min(amount, state["amount"])
        self._emit_event("payment_intent.succeeded", self._intent_doc(state))
        return {"status": 200, "json": self._intent_doc(state)}

    def _cancel(self, state: dict) -> dict:
        if state["status"] == ST_SUCCEEDED:
            return self._err(
                400,
                "invalid_request_error",
                "You cannot cancel this PaymentIntent because it has already succeeded.",
                code="payment_intent_unexpected_state",
            )
        state["status"] = ST_CANCELED
        self._emit_event("payment_intent.canceled", self._intent_doc(state))
        return {"status": 200, "json": self._intent_doc(state)}

    def _create_refund(self, body: dict) -> dict:
        pid = str(_dig(body, "payment_intent") or "")
        state = self._intents.get(pid)
        if state is None:
            return self._missing(pid or "payment_intent")
        if state["status"] != ST_SUCCEEDED:
            return self._err(
                400,
                "invalid_request_error",
                "This PaymentIntent has no successful charge to refund.",
                code="charge_not_captured" if state["status"] == ST_REQUIRES_CAPTURE else None,
            )
        remaining = state["amount_received"] - state["refunded"]
        amount = _to_int(_dig(body, "amount"))
        amount = remaining if amount is None else amount
        if amount <= 0 or amount > remaining:
            return self._err(
                400,
                "invalid_request_error",
                f"Refund amount ({amount}) is greater than unrefunded amount "
                f"on charge ({remaining}).",
                code="amount_too_large",
            )
        state["refunded"] += amount
        doc = {
            "id": _gen_id("re"),
            "object": "refund",
            "amount": amount,
            "currency": state["currency"],
            "payment_intent": state["id"],
            "charge": state["latest_charge"],
            "status": "succeeded",
        }
        self._refunds[doc["id"]] = doc
        self._emit_event("refund.created", doc)
        return {"status": 200, "json": doc}

    def _retrieve_intent(self, pid: str) -> dict:
        state = self._intents.get(pid)
        if state is None:
            return self._missing(pid)
        return {"status": 200, "json": self._intent_doc(state)}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _pan_of(body: dict) -> str:
        pan = str(
            _dig(body, "payment_method_data.card.number")
            or _dig(body, "payment_method_options.card.number")
            or ""
        )
        if pan:
            return pan
        pm = str(_dig(body, "payment_method") or "")
        return _PM_TOKENS.get(pm, "") or ("4242424242424242" if pm.startswith("pm_") else "")

    def _intent_doc(self, state: dict) -> dict:
        doc = {
            "id": state["id"],
            "object": "payment_intent",
            "amount": state["amount"],
            "amount_received": state["amount_received"],
            "currency": state["currency"],
            "capture_method": state["capture_method"],
            "status": state["status"],
            "client_secret": f"{state['id']}_secret_{state['id'][-6:]}",
            "latest_charge": state["latest_charge"],
        }
        if state.get("last_payment_error"):
            doc["last_payment_error"] = state["last_payment_error"]
        return doc

    def _missing(self, obj_id: str) -> dict:
        return self._err(
            404,
            "invalid_request_error",
            f"No such object: '{obj_id}'",
            code="resource_missing",
        )

    @staticmethod
    def _err(
        status: int,
        type_: str,
        message: str,
        code: str | None = None,
        decline_code: str | None = None,
        intent: dict | None = None,
    ) -> dict:
        err: dict[str, Any] = {"type": type_, "message": message}
        if code:
            err["code"] = code
        if decline_code:
            err["decline_code"] = decline_code
        if intent is not None:
            err["payment_intent"] = intent
        return {"status": status, "json": {"error": err}}


__all__ = ["StripeSimulator"]
