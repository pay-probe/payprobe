"""Outbound alert webhook (ADR-0010 D11, Stage 0).

agent-hub tells one HTTP endpoint when a heartbeat ends badly (``failed``,
``budget_exceeded``, ``timed_out``) and when an advisor reports a finding at
``warn`` or above. That is the whole surface: the receiver (a pager, a chat
bridge, the orchestrator's own alert route) decides what to do with it.

Design, mirroring the ADR-0009 simulator webhooks:

* **Fire-and-forget.** ``emit()`` schedules the delivery and returns; a slow or
  dead receiver never delays or fails a heartbeat. ``drain()`` awaits pending
  deliveries (tests, graceful shutdown); ``cancel()`` abandons them.
* **Signed.** ``X-PayProbe-Signature: t=<unix ts>,v1=<HMAC-SHA256>`` over
  ``"<ts>.<body>"`` with the shared secret (the documented Stripe scheme, so
  receivers can reuse a verifier they already have). No secret = unsigned.
* **Retried.** Three attempts with backoff on a 5xx or a transport error; a 4xx
  is final (the receiver rejected it on purpose).
* **Never raises.** Every failure is a counter and a log line.

Env: ``AGENT_HUB_ALERT_WEBHOOK_URL`` (unset = disabled),
``AGENT_HUB_ALERT_WEBHOOK_SECRET``, ``AGENT_HUB_ALERT_TIMEOUT_S`` (default 5).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: heartbeat statuses that always alert
ALERT_STATUSES = frozenset({"failed", "budget_exceeded", "timed_out"})

#: advisor finding severities, ranked; ``warn`` and above alert
_SEVERITY_RANK = {
    "info": 0,
    "low": 0,
    "warn": 1,
    "warning": 1,
    "medium": 1,
    "high": 2,
    "error": 2,
    "critical": 2,
}
_MIN_SEVERITY = 1

Transport = Callable[[str, bytes, dict[str, str]], Awaitable[int]]


def sign(secret: str, body: str, ts: int | None = None) -> str:
    """``t=<ts>,v1=<hex HMAC-SHA256("<ts>.<body>")>`` (Stripe's documented scheme)."""
    ts = int(time.time()) if ts is None else int(ts)
    mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def verify(secret: str, body: str, header: str, *, tolerance_s: int = 300) -> bool:
    """Receiver-side check for :func:`sign` (used by tests; offered to receivers)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _heartbeat_payload(hb: dict) -> dict:
    return {
        "agent": hb.get("agent"),
        "version": hb.get("version"),
        "heartbeat_id": hb.get("id"),
        "status": hb.get("status"),
        "error": hb.get("error"),
        "wake": hb.get("wake"),
        "invoked_by": hb.get("invoked_by"),
        "model": hb.get("model"),
        "tokens_in": hb.get("tokens_in"),
        "tokens_out": hb.get("tokens_out"),
        "started_at": hb.get("started_at"),
        "finished_at": hb.get("finished_at"),
    }


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str | None) -> Any:
    """The JSON an agent's final answer carries, or None.

    Models rarely answer with bare JSON even when told to: the observer's real
    first wake wrapped its findings in a markdown report with a heading, a
    fenced ``json`` block and a prose summary. So: the whole text first, then
    every fenced block in order, then the outermost ``[...]`` / ``{...}``
    slice. Only a dict or list counts; nothing here ever raises.
    """
    s = (text or "").strip()
    if not s:
        return None
    candidates = [s]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(s)]
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = s.find(opener), s.rfind(closer)
        if 0 <= i < j:
            candidates.append(s[i : j + 1])
    for cand in candidates:
        if not cand or cand[0] not in "[{":
            continue
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, (dict, list)):
            return data
    return None


def findings_of(result: str | None) -> list[dict]:
    """Parse an advisor's final answer as a findings list: a bare list or
    ``{"findings": [...]}``, wherever :func:`extract_json` finds it. Anything
    else is no findings (the model did not follow its output contract, which
    the heartbeat record already shows)."""
    data = extract_json(result)
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return []
    return [f for f in data if isinstance(f, dict)]


def events_for(hb: dict, mode: str | None) -> list[tuple[str, dict]]:
    """The (event, payload) pairs one finished heartbeat produces. Pure."""
    out: list[tuple[str, dict]] = []
    status = str(hb.get("status") or "")
    if status in ALERT_STATUSES:
        out.append((f"heartbeat.{status}", _heartbeat_payload(hb)))
    if mode == "advisor" and status == "done":
        head = _heartbeat_payload(hb)
        for finding in findings_of(hb.get("result")):
            sev = str(finding.get("severity") or "info").lower()
            if _SEVERITY_RANK.get(sev, 0) >= _MIN_SEVERITY:
                out.append(("finding", {**head, "finding": finding}))
    return out


async def _httpx_transport(url: str, body: bytes, headers: dict[str, str]) -> int:
    import httpx

    timeout = float(os.environ.get("AGENT_HUB_ALERT_TIMEOUT_S") or 5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, content=body, headers=headers)
    return resp.status_code


class Alerter:
    def __init__(
        self,
        url: str = "",
        secret: str = "",
        *,
        backoff: tuple[float, ...] = (1.0, 4.0),
        transport: Transport | None = None,
    ) -> None:
        self.url = url
        self.secret = secret
        #: sleeps between attempts; len(backoff) + 1 attempts in total
        self.backoff = tuple(backoff)
        self.transport: Transport = transport or _httpx_transport
        self.sent = 0
        self.failed = 0
        self.attempts = 0
        #: recent deliveries (newest last): {ts, event, status, attempts}
        self.log: deque[dict] = deque(maxlen=50)
        self._pending: set[asyncio.Task] = set()

    @classmethod
    def from_env(cls) -> Alerter:
        return cls(
            url=(os.environ.get("AGENT_HUB_ALERT_WEBHOOK_URL") or "").strip(),
            secret=(os.environ.get("AGENT_HUB_ALERT_WEBHOOK_SECRET") or "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # -- emission ----------------------------------------------------------------

    def emit_for(self, hb: dict, mode: str | None) -> int:
        """Schedule every alert a finished heartbeat warrants; returns how many."""
        events = events_for(hb, mode) if self.enabled else []
        for event, payload in events:
            self.emit(event, payload)
        return len(events)

    def emit(self, event: str, payload: dict) -> None:
        """Schedule one delivery. Never raises, never blocks the caller."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync unit tests): nothing to schedule on
            return
        body = json.dumps(
            {
                "event": event,
                "at": datetime.now(UTC).isoformat(),
                "source": "agent-hub",
                "data": payload,
            },
            default=str,
        )
        task = loop.create_task(self._deliver(event, body))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _deliver(self, event: str, body: str) -> None:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "payprobe-agent-hub",
            "X-PayProbe-Event": event,
        }
        if self.secret:
            headers["X-PayProbe-Signature"] = sign(self.secret, body)
        raw = body.encode("utf-8")
        status: Any = None
        attempts = 0
        for i in range(len(self.backoff) + 1):
            attempts += 1
            self.attempts += 1
            try:
                status = await self.transport(self.url, raw, headers)
            except Exception as exc:  # noqa: BLE001 — delivery failure is a stat, not a crash
                status = f"({type(exc).__name__})"
                log.warning("alert %s → %s attempt %d failed: %s", event, self.url, attempts, exc)
            else:
                if status < 500:
                    break  # delivered, or rejected on purpose (4xx): both final
                log.warning("alert %s → %s attempt %d: HTTP %s", event, self.url, attempts, status)
            if i < len(self.backoff):
                await asyncio.sleep(self.backoff[i])
        if isinstance(status, int) and status < 400:
            self.sent += 1
        else:
            self.failed += 1
        self.log.append({"ts": time.time(), "event": event, "status": status, "attempts": attempts})

    # -- lifecycle -----------------------------------------------------------------

    async def drain(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def cancel(self) -> None:
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()

    def stats(self) -> dict:
        return {
            "configured": self.enabled,
            "signed": bool(self.secret),
            "sent": self.sent,
            "failed": self.failed,
            "pending": len(self._pending),
            "recent": list(self.log)[-5:],
        }


__all__ = [
    "ALERT_STATUSES",
    "Alerter",
    "events_for",
    "extract_json",
    "findings_of",
    "sign",
    "verify",
]
