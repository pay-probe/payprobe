"""Bounded, redacted capture for the transparent proxy (ADR-0002, stage 2).

A :class:`~.proxy.TcpProxy` in tap mode sees a real client↔host conversation in
the clear — including PANs, tracks and PIN blocks. :class:`CaptureBuffer` records
each relayed frame as a decoded row, with two safety controls:

* **Redaction (on by default).** Sensitive ISO 8583 data elements are masked
  before they are ever stored — PAN keeps only its BIN + last 4, tracks / expiry
  / PIN block are fully masked. So the capture (and anything built from it) never
  holds cleartext cardholder data.
* **Bounded.** A ring buffer capped at ``max_frames`` — capture never grows
  unbounded on a busy link; the oldest rows are dropped first.

Raw wire bytes are kept only when ``store_raw`` is explicitly enabled (hex), and
even then the decoded view is still redacted.

The buffer also knows how to turn a captured session into a scenario draft
(:func:`build_scenario_draft`) so observed traffic becomes a replayable
regression test — the highest-value use of the tap.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

#: ISO 8583 DEs masked entirely (track data, expiry, PIN block, CVV-ish).
_FULL_MASK_DE = {"14", "35", "45", "52", "53"}
#: DE2 (PAN) gets BIN+last4 masking rather than a full wipe.
_PAN_DE = "2"


def mask_pan(pan: str) -> str:
    """Mask a PAN to ``BIN(6) + middle '*' + last4``. Short values are fully
    masked (nothing safe to reveal)."""
    s = str(pan or "")
    if len(s) <= 10:
        return "*" * len(s)
    return s[:6] + ("*" * (len(s) - 10)) + s[-4:]


def redact_values(values: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an ISO 8583 DE map with sensitive elements masked."""
    out: dict[str, Any] = {}
    for de, val in (values or {}).items():
        key = str(de)
        if key == _PAN_DE:
            out[key] = mask_pan(str(val))
        elif key in _FULL_MASK_DE:
            out[key] = "*" * len(str(val))
        else:
            out[key] = val
    return out


# -- rule-based logging -------------------------------------------------------
#
# A capture rule selects frames by MTI / DE values / direction and attaches a
# log action (tag / level / note). Rules drive two things: tagging matched
# operations for later filtering, and — in ``mode: "matched"`` — deciding which
# frames are logged at all. Matching runs on the *original* decoded values
# (before redaction) so e.g. a BIN prefix still matches; the stored row stays
# redacted. The condition vocabulary mirrors the responder's rule matcher
# (eq/prefix/contains/in/gte/lte) plus ``ne``.


def _cond_ok(value: str, cond: Any) -> bool:
    if isinstance(cond, dict):
        if "eq" in cond and value != str(cond["eq"]):
            return False
        if "ne" in cond and value == str(cond["ne"]):
            return False
        if "prefix" in cond and not value.startswith(str(cond["prefix"])):
            return False
        if "contains" in cond and str(cond["contains"]) not in value:
            return False
        if "in" in cond and value not in [str(x) for x in cond["in"]]:
            return False
        if "gte" in cond and not (value >= str(cond["gte"])):
            return False
        if "lte" in cond and not (value <= str(cond["lte"])):
            return False
        return True
    return value == str(cond)


def _rule_matches(when: dict, parsed: dict) -> bool:
    """Does a decoded frame satisfy a rule's ``when``? An empty ``when`` matches
    everything. ``direction`` (``c2u``/``u2c``) is optional — omit it to match
    both legs."""
    if not when:
        return True
    if "direction" in when and parsed.get("direction") != when["direction"]:
        return False
    if "mti" in when and parsed.get("mti") != when["mti"]:
        return False
    de = parsed.get("de") or {}
    for k, cond in (when.get("de") or {}).items():
        # An absent DE never satisfies a value condition — so a response-only
        # field (e.g. DE39) won't spuriously match requests or undecoded frames.
        if str(k) not in de:
            return False
        if not _cond_ok(str(de[str(k)]), cond):
            return False
    return True


def match_log_rules(parsed: dict, rules: list[dict]) -> dict | None:
    """The ``log`` action of the first rule whose ``when`` matches, else None.
    Undecoded frames (no mti/de) only match a rule with an empty/absent ``when``."""
    for rule in rules or []:
        if _rule_matches(rule.get("when") or {}, parsed):
            return rule.get("log") or {}
    return None


class CaptureBuffer:
    """Bounded, redacted ring buffer of relayed frames."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_frames: int = 5000,
        store_raw: bool = False,
        redact: bool = True,
        rules: list[dict] | None = None,
        mode: str = "all",
    ) -> None:
        self.enabled = enabled
        self.max_frames = max(1, int(max_frames))
        self.store_raw = store_raw
        self.redact = redact
        #: rule-based logging — tag/level/note matched frames; ``mode: "matched"``
        #: logs *only* matched frames (selective capture), ``"all"`` logs every
        #: frame and tags the ones that match.
        self.rules = rules or []
        self.mode = (mode or "all").lower()
        self._rows: deque[dict] = deque(maxlen=self.max_frames)
        self._seq = 0
        self.dropped = 0  # frames evicted by the cap (observability)
        self.skipped = 0  # frames not logged because no rule matched (mode=matched)

    def __len__(self) -> int:
        return len(self._rows)

    def add(self, direction: str, parsed: dict, raw: bytes | None = None) -> None:
        """Record one relayed frame. ``parsed`` is the proxy's decoded view
        (``{mti, de, ...}`` or ``{undecoded: True}``); ``raw`` is the on-wire
        application body, stored (hex) only when ``store_raw``."""
        if not self.enabled:
            return
        # Evaluate logging rules on the ORIGINAL (unredacted) decoded view.
        log = match_log_rules(parsed, self.rules) if self.rules else None
        if self.mode == "matched" and log is None:
            self.skipped += 1
            return  # selective capture: only matched frames are logged
        if len(self._rows) == self.max_frames:
            self.dropped += 1
        self._seq += 1
        row: dict[str, Any] = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "direction": direction,
        }
        if parsed.get("undecoded"):
            row["undecoded"] = True
        else:
            row["mti"] = parsed.get("mti")
            values = parsed.get("de") or {}
            row["values"] = redact_values(values) if self.redact else dict(values)
        if log:
            if log.get("tag"):
                row["tag"] = log["tag"]
            if log.get("level"):
                row["level"] = log["level"]
            if log.get("note"):
                row["note"] = log["note"]
        if self.store_raw and raw is not None:
            row["raw"] = raw.hex()
        self._rows.append(row)

    def rows(self) -> list[dict]:
        return list(self._rows)

    def clear(self) -> None:
        self._rows.clear()
        self._seq = 0
        self.dropped = 0
        self.skipped = 0


def build_scenario_draft(rows: list[dict], name: str, *, target: str = "tcp_iso8583") -> dict:
    """Turn captured rows into a ScenarioDraft-shaped dict.

    Pairs each client→upstream request (``c2u``) with the next upstream→client
    response (``u2c``): the request becomes a ``send_message`` step (replaying its
    MTI + DE values), the response becomes a ``response_code`` assertion. Sensitive
    fields are already masked in the rows, so the draft is a template — replace
    masked card data (or wire a test-data pool) before running against a real host.
    """
    steps: list[dict] = []
    pending: dict | None = None
    for row in rows:
        if row.get("undecoded"):
            continue
        if row.get("direction") == "c2u":
            pending = row
        elif row.get("direction") == "u2c" and pending is not None:
            sid = f"step_{len(steps) + 1:03d}"
            assertions: list[dict] = []
            rc = (row.get("values") or {}).get("39")
            if rc is not None:
                assertions.append({"field": "response_code", "operator": "eq", "expected": rc})
            steps.append(
                {
                    "id": sid,
                    "target": target,
                    "action": "send_message",
                    "payload": {"mti": pending.get("mti"), "values": pending.get("values", {})},
                    "assertions": assertions,
                }
            )
            pending = None
    return {
        "name": name,
        "description": (
            "Built from captured proxy traffic. Sensitive fields are masked — "
            "replace card data (or attach a test-data pool) before running "
            "against a real host."
        ),
        "tags": ["captured", "proxy"],
        "steps": steps,
    }
