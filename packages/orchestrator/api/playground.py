"""Playground — ad-hoc execution helpers (ADR-0007).

The Playground is a *thin composition surface*: execution is fully delegated to
the one execution owner (``WorkerEngine`` / the adapter registry), target
resolution happens in ``main.py`` next to the run-path resolvers it mirrors
(``_connection_effective``, ``_group_adapter_config``). This module holds the
pieces that are pure enough to unit-test standalone:

* **Masking** — echoed request/response payloads are redacted with the same
  rules the proxy-tap :class:`~worker.adapters.tcp.capture.CaptureBuffer`
  applies (PAN keeps BIN + last 4; tracks/PIN/CVV are wiped), extended to
  common flat key names (``pan``, ``track2``, ``pin_block`` …) so JSON/HTTP
  payloads are covered too. Invariant #8: secrets and cardholder data never
  round-trip to the client in clear.
* **History** — a per-user, bounded, in-memory ring of recent interactions
  (the CaptureBuffer pattern: in-memory by design, not a file store). The raw
  request is kept *in memory only* so an entry can be re-fired verbatim; every
  view handed to a client is masked.
* **Promote** — ``build_playground_scenario`` turns history entries into a
  ScenarioDraft-shaped dict (the proxy-capture save-as-scenario precedent), so
  an exploration becomes a durable, replayable test. Values are the *masked*
  view: like a proxy capture, the promoted scenario is a template — replace
  masked card data (or attach a test-data pool) before running it for real.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

from worker.adapters.tcp.capture import mask_pan, redact_values

#: dict keys inside a payload that hold an ISO 8583 DE map (masked as DEs).
_DE_MAP_KEYS = {"values", "fields", "de"}
#: flat payload keys masked entirely (JSON/HTTP payload equivalents of the
#: fully-masked DEs: tracks, expiry, PIN material, CVV).
_FULL_MASK_KEYS = {"track1", "track2", "pin", "pin_block", "cvv", "cvv2",
                   "expiry", "expiration_date"}
#: flat payload keys that get PAN-style masking (BIN + last 4).
_PAN_KEYS = {"pan", "card_number", "cardnumber", "account_number"}


def mask_payload(payload: Any, _depth: int = 0) -> Any:
    """A deep copy of ``payload`` with sensitive material masked.

    ISO 8583 DE maps (under ``values``/``fields``/``de``) go through the
    proxy-tap redaction rules; flat keys that *name* card data are masked by
    key. Everything else passes through untouched. Never raises — masking is a
    display concern and must not fail an execute."""
    if _depth > 8 or not isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, list):
        return [mask_payload(v, _depth + 1) for v in payload]
    out: dict[str, Any] = {}
    for k, v in payload.items():
        lk = str(k).lower()
        if lk in _DE_MAP_KEYS and isinstance(v, dict):
            out[k] = redact_values(v)
        elif lk in _PAN_KEYS and isinstance(v, (str, int)):
            out[k] = mask_pan(str(v))
        elif lk in _FULL_MASK_KEYS and isinstance(v, (str, int)):
            out[k] = "*" * len(str(v))
        else:
            out[k] = mask_payload(v, _depth + 1)
    return out


class PlaygroundHistory:
    """Per-user bounded ring of recent playground interactions.

    In-memory by design (the CaptureBuffer precedent): a restart forgets it,
    which is fine — anything worth keeping is promoted via save-as-scenario.
    The stored entry keeps the *raw* request so re-fire replays it verbatim;
    :meth:`rows` / :meth:`get` return masked views only.
    """

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max(1, int(max_entries))
        self._rings: dict[str, deque[dict]] = {}
        self._seq = 0

    def add(self, user: str, *, target: dict, action: str, payload: dict,
            message_format_id: str | None, ok: bool, status: str,
            request: Any, response: Any, error: str | None,
            duration_ms: int, label: str | None = None) -> dict:
        """Record one interaction; returns the (masked) view of the new entry."""
        self._seq += 1
        entry = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "user": user,
            "source": "playground",
            "target": target,
            "action": action,
            "message_format_id": message_format_id,
            "label": label,
            "ok": ok,
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
            # raw request kept in memory only — re-fire needs it verbatim.
            "_payload": payload,
            "request": mask_payload(request),
            "response": mask_payload(response),
        }
        ring = self._rings.setdefault(user, deque(maxlen=self.max_entries))
        ring.append(entry)
        return self._view(entry)

    @staticmethod
    def _view(entry: dict) -> dict:
        """The client-facing (masked) shape: no raw payload."""
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def rows(self, user: str) -> list[dict]:
        """Masked entries for one user, newest first."""
        return [self._view(e) for e in reversed(self._rings.get(user, ()))]

    def get(self, user: str, seq: int) -> dict | None:
        """One RAW entry (for re-fire). Callers must not echo ``_payload``."""
        for e in self._rings.get(user, ()):
            if e["seq"] == seq:
                return e
        return None

    def clear(self, user: str) -> int:
        ring = self._rings.pop(user, None)
        return len(ring) if ring else 0


def build_playground_scenario(entries: list[dict], name: str) -> dict:
    """Turn playground history entries into a ScenarioDraft-shaped dict.

    Each entry becomes one step replaying its action + (masked) request against
    the same target reference; a successful ISO 8583 response contributes a
    ``response_code`` assertion (the proxy save-as-scenario precedent). Function
    (crypto) entries become ``crypto`` nodes. Entries are replayed oldest-first.
    """
    steps: list[dict] = []
    for e in sorted(entries, key=lambda x: x["seq"]):
        sid = f"step_{len(steps) + 1:03d}"
        target = e.get("target") or {}
        kind = target.get("kind")
        if kind == "function":
            steps.append({
                "id": sid, "kind": "crypto",
                "config": {"operation": e.get("action"),
                           **(e.get("request") or {})},
            })
            continue
        payload = dict(e.get("request") or {})
        if e.get("message_format_id"):
            payload.setdefault("message_format_id", e["message_format_id"])
        assertions: list[dict] = []
        rc = ((e.get("response") or {}).get("response_code")
              if isinstance(e.get("response"), dict) else None)
        if rc is not None:
            assertions.append(
                {"field": "response_code", "operator": "eq", "expected": rc})
        step: dict[str, Any] = {
            "id": sid,
            "target": target.get("id") or "tcp_iso8583",
            "action": e.get("action"),
            "payload": payload,
            "assertions": assertions,
        }
        if kind in ("connection", "group") and target.get("id"):
            # the editor's connection binding — the run path re-points the step
            # exactly like _attach_connections does for authored scenarios.
            step["config"] = {"connection": target["id"]}
            if target.get("environment"):
                step["environment_override"] = target["environment"]
        steps.append(step)
    return {
        "name": name,
        "description": (
            "Promoted from Playground interactions. Sensitive fields are "
            "masked — replace card data (or attach a test-data pool) before "
            "running against a real host."
        ),
        "tags": ["captured", "playground"],
        "steps": steps,
    }
