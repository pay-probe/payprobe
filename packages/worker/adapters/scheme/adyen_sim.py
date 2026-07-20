"""AdyenCheckoutSimulator — an Adyen Checkout API simulator (/payments family).

The second PSP simulator of ADR-0009 (Stripe's sibling, and deliberately a
*different integration shape*: JSON bodies + ``X-API-Key`` header where Stripe
is form-encoded + bearer). It answers **Adyen's Checkout API** — ``/payments``,
``/payments/details``, ``/payments/{pspReference}/refunds`` — so PayProbe can
stand in for Adyen during loopback testing of a merchant integration, and the
chaos/load machinery can storm an "Adyen" that is safe to break (the
``external: true`` guardrail forbids doing that to the real test environment).

Subclasses :class:`~worker.adapters.http.responder.HttpResponder` for the HTTP
lifecycle, metrics and chaos machinery — only routing and the reply decision
are Adyen-specific (the CyberSource/Stripe precedent).

Fidelity (read carefully)
--------------------------
Functional simulator of the *public* Checkout contract: the ``resultCode``
value set (``Authorised`` / ``Refused`` / ``ChallengeShopper``), the
``refusalReason`` / ``refusalReasonCode`` pairs, the error envelope
(``status`` / ``errorCode`` / ``message`` / ``errorType``), asynchronous
refunds (``status: "received"`` — Adyen validates refunds async, so the sim
accepts them the way the real API does), and **Adyen's documented test
triggers**: a magic ``paymentMethod.holderName`` (``DECLINED``,
``CARD_EXPIRED``, ``NOT_ENOUGH_BALANCE``, …) or a numeric
``additionalData.RequestedTestAcquirerResponseCode`` forces the matching
refusal, exactly like the real test environment. Version prefixes are
tolerated (``/v71/payments`` ≡ ``/payments``). Not a byte-exact reproduction
of every response field. Pin extra outcomes via ``rules`` (evaluated first).

Supported flows
---------------
* **Payment** ``POST /payments`` (Authorised / Refused ladder; the 3DS2 test
  card ``4212345678901237`` parks in ``ChallengeShopper`` with an ``action``)
* **Details** ``POST /payments/details`` (completes a pending challenge)
* **Refund** ``POST /payments/{pspReference}/refunds`` (``status: received``)

Config (JSON-friendly)::

    {
      "protocol": "adyen",
      "host": "0.0.0.0", "port": 8086,
      "adyen": {
        "require_auth": true,        # 401 unless an X-API-Key header is present
        "merchant_account": null,    # if set, body.merchantAccount must match
        "decline_over": null         # amount.value >= -> NOT_ENOUGH_BALANCE
      },
      "rules": [ ... ],              # explicit HTTP overrides win
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
from ..http.webhooks import adyen_hmac_signature

log = logging.getLogger(__name__)

RC_AUTHORISED = "Authorised"
RC_REFUSED = "Refused"
RC_CHALLENGE = "ChallengeShopper"

#: documented holderName test triggers -> (refusalReason, refusalReasonCode)
#: (docs.adyen.com "Testing result codes"; APPROVED simply authorises)
_HOLDER_TRIGGERS: dict[str, tuple[str, str]] = {
    "DECLINED": ("Refused", "2"),
    "CARD_EXPIRED": ("Expired Card", "6"),
    "INVALID_CARD_NUMBER": ("Invalid Card Number", "8"),
    "NOT_3D_AUTHENTICATED": ("3d-secure: Authentication failed", "11"),
    "NOT_ENOUGH_BALANCE": ("Not enough balance", "12"),
    "CVC_DECLINED": ("CVC Declined", "24"),
}

#: the numeric RequestedTestAcquirerResponseCode alternative (same table)
_ACQUIRER_CODES: dict[str, tuple[str, str]] = {
    "2": ("Refused", "2"),
    "6": ("Expired Card", "6"),
    "8": ("Invalid Card Number", "8"),
    "11": ("3d-secure: Authentication failed", "11"),
    "12": ("Not enough balance", "12"),
    "24": ("CVC Declined", "24"),
}

#: Adyen's documented 3DS2 challenge test card
_CHALLENGE_CARDS = {"4212345678901237"}

_PAY_RE = re.compile(r"^(?:/v\d+)?/payments/?$")
_DETAILS_RE = re.compile(r"^(?:/v\d+)?/payments/details/?$")
_REFUND_RE = re.compile(r"^(?:/v\d+)?/payments/(?P<ref>[^/]+)/refunds/?$")


def _psp_ref() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=16))


class AdyenCheckoutSimulator(HttpResponder):
    PROTOCOL = "adyen"

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", self.PROTOCOL)
        super().__init__(config)
        ad = dict(config.get("adyen") or {})
        self.require_auth: bool = bool(ad.get("require_auth", True))
        self.merchant_account = ad.get("merchant_account")
        self.decline_over = ad.get("decline_over")
        #: authorised payments: pspReference -> {amount, merchantReference}
        self._payments: dict[str, dict] = {}
        #: pending 3DS challenges: paymentData token -> the pending payment body
        self._pending: dict[str, dict] = {}

    # -- by-decision metric ----------------------------------------------------
    def _response_code(self, action: dict) -> str:
        body = action.get("json") or {}
        if "resultCode" in body:
            if body["resultCode"] == RC_REFUSED:
                return str(body.get("refusalReason") or RC_REFUSED)
            return str(body["resultCode"])
        if "errorCode" in body:
            return f"error:{body['errorCode']}"
        return str(body.get("status") or action.get("status", 200))

    # -- routing ---------------------------------------------------------------

    def _resolve(self, parsed: dict) -> dict:
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})

        headers = parsed.get("headers") or {}
        if self.require_auth and not headers.get("x-api-key"):
            return self._error(
                401, "000",
                "HTTP Status Response - Unauthorized: Payment Request not authorised",
                "security",
            )

        method = parsed.get("method", "GET")
        path = parsed.get("path", "")
        body = parsed.get("body") if isinstance(parsed.get("body"), dict) else {}

        if method != "POST":
            return self._error(405, "000", f"Method {method} not allowed", "validation")
        if _PAY_RE.match(path):
            return self._payment(body)
        if _DETAILS_RE.match(path):
            return self._details(body)
        m = _REFUND_RE.match(path)
        if m:
            return self._refund(m.group("ref"), body)
        return self._error(404, "000", "Unknown resource", "validation")


    # -- webhook emission (ADR-0009 phase 4) -----------------------------------

    def _emit_notification(self, item: dict) -> None:
        """Emit one Adyen-shaped notification (fire-and-forget): the standard
        webhook envelope with a single ``NotificationRequestItem``, HMAC-signed
        per Adyen's documented colon-joined scheme (hex key, base64 digest) in
        ``additionalData.hmacSignature``."""
        if not self.webhooks.enabled:
            return
        item = dict(item)
        item.setdefault("originalReference", "")
        item.setdefault("merchantReference", "")
        item.setdefault("eventDate",
                        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
        item["additionalData"] = {
            "hmacSignature": adyen_hmac_signature(self.webhooks.secret, item),
        }
        body = json.dumps({
            "live": "false",
            "notificationItems": [{"NotificationRequestItem": item}],
        })
        self.webhooks.emit(str(item.get("eventCode") or ""), body, {})

    # -- /payments -------------------------------------------------------------

    def _payment(self, body: dict) -> dict:
        merchant = _dig(body, "merchantAccount")
        if not merchant or (self.merchant_account and merchant != self.merchant_account):
            return self._error(403, "901", "Invalid Merchant Account", "security")
        value = self._amount_value(body)
        if value is None:
            return self._error(422, "100", "Amount is not valid", "validation")

        # documented test triggers: holderName, then the numeric acquirer code
        holder = str(_dig(body, "paymentMethod.holderName") or "").strip().upper()
        trigger = _HOLDER_TRIGGERS.get(holder)
        if trigger is None:
            code = str(_dig(body, "additionalData.RequestedTestAcquirerResponseCode") or "")
            trigger = _ACQUIRER_CODES.get(code)
        if trigger:
            reason, reason_code = trigger
            return self._refused(body, reason, reason_code)

        if (
            self.decline_over is not None
            and value >= float(self.decline_over)
        ):
            return self._refused(body, "Not enough balance", "12")

        pan = str(_dig(body, "paymentMethod.number") or "")
        if pan in _CHALLENGE_CARDS:
            token = f"pd_{_psp_ref()}"
            self._pending[token] = body
            return {
                "status": 200,
                "json": {
                    "resultCode": RC_CHALLENGE,
                    "action": {
                        "type": "threeDS2",
                        "subtype": "challenge",
                        "paymentData": token,
                        "paymentMethodType": "scheme",
                    },
                },
            }

        return self._authorised(body)

    def _authorised(self, body: dict) -> dict:
        ref = _psp_ref()
        amount = _dig(body, "amount") or {}
        self._payments[ref] = {
            "amount": amount,
            "merchantReference": _dig(body, "reference"),
        }
        doc: dict[str, Any] = {
            "resultCode": RC_AUTHORISED,
            "pspReference": ref,
            "amount": amount,
        }
        if _dig(body, "reference") is not None:
            doc["merchantReference"] = _dig(body, "reference")
        self._emit_notification({
            "pspReference": ref,
            "merchantAccountCode": str(_dig(body, "merchantAccount") or ""),
            "merchantReference": str(_dig(body, "reference") or ""),
            "amount": amount,
            "eventCode": "AUTHORISATION",
            "success": "true",
        })
        return {"status": 200, "json": doc}

    def _refused(self, body: dict, reason: str, reason_code: str) -> dict:
        doc: dict[str, Any] = {
            "resultCode": RC_REFUSED,
            "pspReference": _psp_ref(),
            "refusalReason": reason,
            "refusalReasonCode": reason_code,
        }
        if _dig(body, "reference") is not None:
            doc["merchantReference"] = _dig(body, "reference")
        self._emit_notification({
            "pspReference": doc["pspReference"],
            "merchantAccountCode": str(_dig(body, "merchantAccount") or ""),
            "merchantReference": str(_dig(body, "reference") or ""),
            "amount": _dig(body, "amount") or {},
            "eventCode": "AUTHORISATION",
            "success": "false",
            "reason": reason,
        })
        # Adyen answers a refusal with HTTP 200 — the verdict is resultCode.
        return {"status": 200, "json": doc}

    # -- /payments/details (3DS completion) ------------------------------------

    def _details(self, body: dict) -> dict:
        token = str(_dig(body, "paymentData") or "")
        pending = self._pending.pop(token, None)
        if pending is None:
            return self._error(
                422, "704", "request already processed or contains invalid data", "validation"
            )
        return self._authorised(pending)

    # -- /payments/{ref}/refunds ------------------------------------------------

    def _refund(self, ref: str, body: dict) -> dict:
        parent = self._payments.get(ref)
        if parent is None:
            return self._error(422, "731", "PaymentDetail not found", "validation")
        merchant = _dig(body, "merchantAccount")
        if not merchant or (self.merchant_account and merchant != self.merchant_account):
            return self._error(403, "901", "Invalid Merchant Account", "security")
        # Adyen validates refunds asynchronously — the API accepts the request
        # (even over-refunds; those fail later via webhook), so the sim does too.
        refund_ref = _psp_ref()
        amount = _dig(body, "amount") or parent.get("amount")
        self._emit_notification({
            "pspReference": refund_ref,
            "originalReference": ref,
            "merchantAccountCode": str(merchant or ""),
            "merchantReference": str(parent.get("merchantReference") or ""),
            "amount": amount,
            "eventCode": "REFUND",
            "success": "true",
        })
        return {
            "status": 201,
            "json": {
                "merchantAccount": merchant,
                "paymentPspReference": ref,
                "pspReference": refund_ref,
                "status": "received",
                "amount": amount,
            },
        }

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _amount_value(body: dict) -> float | None:
        raw = _dig(body, "amount.value")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error(status: int, code: str, message: str, error_type: str) -> dict:
        return {
            "status": status,
            "json": {
                "status": status,
                "errorCode": code,
                "message": message,
                "errorType": error_type,
            },
        }


__all__ = ["AdyenCheckoutSimulator"]
