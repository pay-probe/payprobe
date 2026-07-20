"""VisaSimulator — a VISA Base I-style scheme/host simulator.

The scheme counterpart of the generic :class:`~worker.adapters.tcp.responder.TcpResponder`:
instead of a blank rules engine, it ships VISA's authorization message flows,
response-code conventions, stand-in (STIP) behaviour and — optionally — real
card cryptography (CVV2/PVV/ARQC), so PayProbe can stand in for VisaNet during
loopback testing with no real scheme connection.

Fidelity (read carefully)
--------------------------
This is a **functional Base I** simulator built from the *public* ISO 8583:1987
message structure that VISA Base I derives from, plus the parts of the card-data
cryptography already implemented in :mod:`worker.engine.crypto_tools`.

It is **not** a byte-exact reproduction of VISA's confidential *V.I.P. System
Field-Level* / *Base I Technical* specifications. The exact internal layout of
VISA private fields (e.g. the sub-field structure of DE 62/63) and the BASE I/II
edit tables are member-only documents; supply those and they can be encoded
exactly as a later phase. Until then, treat DE 62/63 here as opaque echo fields.

Wire & framing
--------------
VISA Base I is ISO 8583, so this subclass reuses the base responder's framing and
ISO codec unchanged. Only the *reply decision* is VISA-specific, computed in
:meth:`_resolve` and returned in the same ``{echo, set, generate, mti}`` action
shape the base :meth:`_encode` already understands.

Supported message flows
------------------------
* **Authorization** ``0100/0110`` (auth) and ``0200/0210`` (financial request)
* **Reversal / advice** ``0400/0410`` and ``0420/0430``
* **Network management** ``0800/0810`` (sign-on / sign-off / echo-test)
* **Clearing advice** ``0220/0230`` and ``0520/0530`` (acknowledged at a
  functional level; full Base II / SMS batch record formats are out of scope)

Config (JSON-friendly)::

    {
      "protocol": "visa",
      "host": "0.0.0.0", "port": 7010,
      "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },

      "visa": {
        "stand_in": true,                 # answer auths locally (STIP) vs. decline 91
        "approve_code": "00",             # DE 39 on approval
        "decline_over": "000000100000",   # DE 4 >= this -> 61 (exceeds amount limit)
        "block_bins": ["400000"],         # DE 2 prefix match -> block_response
        "block_response": "05",           # DE 39 for a blocked BIN (05 = do not honor)
        "expiry_check": true,             # DE 14 (YYMM) in the past -> 54 (expired card)

        # --- optional real-crypto verification (only runs when present) ------
        "verify_cvv2": {"field": "48", "cvk": "<32H CVK>",
                         "service_code": "000", "decline": "N7"},
        "verify_pvv":  {"pin_field": "52", "pvv_field": "44", "pvk": "<32H PVK>",
                         "pvki": "1", "decline": "55"},
        "verify_arqc": {"field": "55", "session_key": "<32H SK>", "decline": "05"}
      },

      # standard responder extras still apply and take precedence:
      "rules": [ ... ],                   # explicit overrides win over VISA defaults
      "chaos": { ... }                    # fault injection
    }

Any explicit ``rules`` you supply are evaluated first and win; the VISA scheme
logic is the fallthrough default, so you can pin specific cards/BINs to specific
response codes and let everything else follow scheme behaviour.
"""

from __future__ import annotations

import logging
import random
import string

from ...engine import crypto_tools as ct
from ..tcp import iso8583
from ..tcp.responder import TcpResponder

log = logging.getLogger(__name__)

#: Registry id of the canonical VISA Base I DE table (scenario-service
#: ``BUILTIN_FORMATS``). When a VISA simulator is started through the orchestrator
#: it binds this format and the resolved ``fields`` are injected into the config;
#: ``VISA_FIELDS`` below is only the offline fallback (standalone / tests, or when
#: scenario-service can't be reached). Keep the two in sync.
DEFAULT_FORMAT_ID = "visa-base1"

#: DE table = the base 1987 set plus the VISA-relevant fields the flows touch.
#: Offline fallback for :data:`DEFAULT_FORMAT_ID` (see note above).
VISA_FIELDS: dict[str, dict] = {
    **iso8583.DEFAULT_FIELDS,
    "43": {"name": "Card Acceptor Name/Location", "len_type": "fixed", "length": 40},
    "44": {"name": "Additional Response Data", "len_type": "llvar", "length": 25},
    "48": {"name": "Additional Data (Private)", "len_type": "lllvar", "length": 999},
    "54": {"name": "Additional Amounts", "len_type": "lllvar", "length": 120},
    "60": {"name": "Reserved (Visa POS Data)", "len_type": "lllvar", "length": 999},
    "62": {"name": "Custom Payment Service (Visa)", "len_type": "lllvar", "length": 999},
    "63": {"name": "Network Data (Visa)", "len_type": "lllvar", "length": 999},
    "90": {"name": "Original Data Elements", "len_type": "fixed", "length": 42},
    "95": {"name": "Replacement Amounts", "len_type": "fixed", "length": 42},
}

# VISA-ish DE 39 response codes used as defaults below (public, widely documented).
RC_APPROVED = "00"
RC_REFER_TO_ISSUER = "01"
RC_INVALID_MERCHANT = "03"
RC_DO_NOT_HONOR = "05"
RC_EXPIRED_CARD = "54"
RC_EXCEEDS_AMOUNT_LIMIT = "61"
RC_CVV2_MISMATCH = "N7"
RC_PIN_INVALID = "55"
RC_ISSUER_UNAVAILABLE = "91"


def _gen_auth_id() -> str:
    """A 6-char alphanumeric DE 38 authorization identification response."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


class VisaSimulator(TcpResponder):
    PROTOCOL = "visa"
    #: Registry format bound by default when started via the orchestrator.
    DEFAULT_FORMAT_ID = DEFAULT_FORMAT_ID

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", "iso8583")  # wire really is ISO 8583
        # Prefer fields injected from the bound registry format; otherwise fall
        # back to the offline VISA_FIELDS copy.
        config.setdefault("fields", VISA_FIELDS)
        super().__init__(config)
        self.protocol = self.PROTOCOL  # advertise as "visa" to the dashboard
        self.visa: dict = config.get("visa") or {}

    # -- reply decision ------------------------------------------------------

    def _resolve(self, parsed: dict) -> dict:
        # Explicit user rules win, exactly like the base responder.
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return dict(rule.get("respond", {}))
        return self._visa_default(parsed)

    def _visa_default(self, parsed: dict) -> dict:
        mti = parsed.get("mti", "0200")
        family = mti[:2]
        if family == "08":
            return self._network_mgmt(parsed)
        if family == "04":
            return self._reversal(parsed)
        if family in ("01", "02"):
            return self._authorization(parsed)
        if family in ("22", "52"):  # 0220 / 0520 advice & clearing
            return self._advice(parsed)
        # Unknown family: acknowledge.
        return {"echo": ["7", "11", "37"], "set": {"39": RC_APPROVED}}

    # -- 0800 network management ---------------------------------------------

    def _network_mgmt(self, parsed: dict) -> dict:
        # Echo the network-management info code (DE 70) and approve.
        return {"echo": ["7", "11", "70"], "set": {"39": RC_APPROVED}}

    # -- 0400/0420 reversal & advice -----------------------------------------

    def _reversal(self, parsed: dict) -> dict:
        # Reversals/advices are acknowledged (approved) by the scheme.
        return {
            "echo": ["4", "7", "11", "37", "90"],
            "set": {"39": RC_APPROVED},
            "generate": {"38": _gen_auth_id()},
        }

    # -- 0220/0520 advice & clearing -----------------------------------------

    def _advice(self, parsed: dict) -> dict:
        return {"echo": ["7", "11", "37"], "set": {"39": RC_APPROVED}}

    # -- 0100/0200 authorization ---------------------------------------------

    def _authorization(self, parsed: dict) -> dict:
        de = parsed.get("de", {})
        echo = ["7", "11", "37", "41", "49"]

        decline = self._decline_reason(de)
        if decline is not None:
            return {"echo": echo, "set": {"39": decline}}

        if not self.visa.get("stand_in", True):
            # Not standing in and nothing pinned a reply -> issuer unavailable.
            return {"echo": echo, "set": {"39": RC_ISSUER_UNAVAILABLE}}

        approve = str(self.visa.get("approve_code", RC_APPROVED))
        return {"echo": echo, "set": {"39": approve}, "generate": {"38": _gen_auth_id()}}

    def _decline_reason(self, de: dict) -> str | None:
        """Return a DE 39 decline code if the auth should be declined, else None.

        Order: amount limit, blocked BIN, expired card, then optional real-crypto
        checks (CVV2 / PVV / ARQC) when their config block + keys are present.
        """
        v = self.visa
        pan = str(de.get("2", "") or "")
        amount = str(de.get("4", "") or "")

        over = v.get("decline_over")
        if over and amount and amount.isdigit() and amount >= str(over):
            return RC_EXCEEDS_AMOUNT_LIMIT

        for bin_prefix in v.get("block_bins", []) or []:
            if pan.startswith(str(bin_prefix)):
                return str(v.get("block_response", RC_DO_NOT_HONOR))

        if v.get("expiry_check") and self._is_expired(str(de.get("14", "") or "")):
            return RC_EXPIRED_CARD

        if (cfg := v.get("verify_cvv2")) and not self._cvv2_ok(de, cfg):
            return str(cfg.get("decline", RC_CVV2_MISMATCH))

        if (cfg := v.get("verify_pvv")) and not self._pvv_ok(de, cfg):
            return str(cfg.get("decline", RC_PIN_INVALID))

        if (cfg := v.get("verify_arqc")) and not self._arqc_ok(de, cfg):
            return str(cfg.get("decline", RC_DO_NOT_HONOR))

        return None

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _is_expired(expiry: str) -> bool:
        """DE 14 is YYMM. Expired when before the simulator's host clock."""
        import datetime as _dt

        expiry = "".join(c for c in expiry if c.isdigit())
        if len(expiry) < 4:
            return False
        yy, mm = int(expiry[:2]), int(expiry[2:4])
        if not 1 <= mm <= 12:
            return False
        now = _dt.date.today()
        exp = 2000 + yy
        # Card valid through the last day of its expiry month.
        return (exp, mm) < (now.year, now.month)

    def _cvv2_ok(self, de: dict, cfg: dict) -> bool:
        pan = str(de.get("2", "") or "")
        expiry = str(de.get(str(cfg.get("expiry_field", "14")), de.get("14", "")) or "")
        presented = str(de.get(str(cfg.get("field", "48")), "") or "").strip()
        if not (pan and expiry and presented and cfg.get("cvk")):
            return True  # nothing to check against -> don't fail the auth
        try:
            expected = ct.cvv(pan, expiry, str(cfg.get("service_code", "000")), cfg["cvk"])["cvv"]
        except Exception as exc:  # noqa: BLE001 — bad key/data -> skip the check
            log.warning("visa: CVV2 check skipped (%s)", exc)
            return True
        return presented.lstrip("0") == expected.lstrip("0") or presented == expected

    def _pvv_ok(self, de: dict, cfg: dict) -> bool:
        pan = str(de.get("2", "") or "")
        pin = str(de.get(str(cfg.get("pin_field", "52")), "") or "")
        presented = str(de.get(str(cfg.get("pvv_field", "44")), "") or "").strip()
        if not (pan and pin and presented and cfg.get("pvk")):
            return True
        try:
            expected = ct.pvv(pan, pin, str(cfg.get("pvki", "1")), cfg["pvk"])["pvv"]
        except Exception as exc:  # noqa: BLE001
            log.warning("visa: PVV check skipped (%s)", exc)
            return True
        return presented == expected

    def _arqc_ok(self, de: dict, cfg: dict) -> bool:
        arqc = str(de.get(str(cfg.get("field", "55")), "") or "").strip()
        if not (arqc and cfg.get("session_key") and cfg.get("data")):
            return True
        try:
            res = ct.arqc(cfg["session_key"], str(cfg["data"]), expected=arqc)
        except Exception as exc:  # noqa: BLE001
            log.warning("visa: ARQC check skipped (%s)", exc)
            return True
        return bool(res.get("match", res.get("arqc", "").upper() == arqc.upper()))

    @property
    def flows_supported(self) -> list[str]:
        return [
            "0100/0110",
            "0200/0210",
            "0400/0410",
            "0420/0430",
            "0800/0810",
            "0220/0230",
            "0520/0530",
        ]
