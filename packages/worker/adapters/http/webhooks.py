"""WebhookEmitter — the simulator→merchant back-channel (ADR-0009 phase 4).

Real PSPs call the merchant back: Stripe events, Adyen notifications, PayPal
webhook events. Merchant webhook handling — signature verification, retries,
duplicates, out-of-order delivery — is exactly the logic teams need to test,
so the provider simulators can emit provider-shaped, provider-signed webhooks
on state transitions. That closes the twin offline: the simulated Stripe
calls the merchant participant back on the same canvas.

Design:

* **Fire-and-forget.** ``emit()`` schedules the delivery as a task and returns
  immediately — the simulator's reply path is never delayed or broken by a
  slow/dead webhook endpoint (matching real PSPs, whose webhooks are async).
  ``drain()`` awaits pending deliveries (tests / graceful shutdown);
  ``cancel()`` abandons them (stop path).
* **Chaos on the emission leg.** The ``webhooks.chaos`` block reuses
  :class:`~worker.adapters.tcp.chaos.ChaosEngine`: *drop* loses the delivery,
  *latency* delays it, *malformed* corrupts the body **after signing** — which
  breaks the signature exactly the way wire corruption would, so a verifying
  receiver rejects it. Delayed/dropped/duplicate-tolerant webhook handling is
  the failure mode merchants mishandle; now it is testable.
* **Signatures are provider-correct where the provider's scheme is
  reproducible.** Stripe: the documented ``Stripe-Signature: t=…,v1=HMAC``
  scheme. Adyen: the documented colon-joined NotificationRequestItem HMAC
  (hex key, base64 output). PayPal verifies webhooks via an API call against
  certs in reality — the sim sends an **HMAC stand-in** in
  ``Paypal-Transmission-Sig`` with ``Paypal-Auth-Algo: HMAC-SHA256`` (honest
  label, not PayPal's cert scheme) so receiver logic still has something to
  verify offline.

Config (``webhooks`` block on a simulator)::

    "webhooks": {
      "url": "http://127.0.0.1:9099/webhooks/stripe",
      "secret": "whsec_test",          # signing secret (Adyen: hex HMAC key)
      "events": [],                    # filter by event type; empty = all
      "timeout_sec": 5,
      "headers": {"X-Extra": "1"},
      "chaos": {"drop_pct": 0, "latency_ms": 0, "malformed_pct": 0}
    }
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections import deque
from typing import Any

from ..tcp.chaos import ChaosEngine

log = logging.getLogger(__name__)


class WebhookEmitter:
    def __init__(self, cfg: dict | None, label: str = "sim") -> None:
        cfg = dict(cfg or {})
        self.url = str(cfg.get("url") or "")
        self.secret = str(cfg.get("secret") or "")
        self.events = [str(e) for e in (cfg.get("events") or [])]
        try:
            self.timeout = float(cfg.get("timeout_sec") or 5.0)
        except (TypeError, ValueError):
            self.timeout = 5.0
        self.headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.chaos_cfg = dict(cfg.get("chaos") or {})
        self.chaos = ChaosEngine(self.chaos_cfg.get("seed"))
        self.label = label
        self.enabled = bool(self.url)

        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.faults = 0
        #: recent deliveries (newest last): {ts, event, status}
        self.log: deque[dict] = deque(maxlen=50)
        self._pending: set[asyncio.Task] = set()

    # -- emission --------------------------------------------------------------

    def emit(self, event_type: str, body: str, headers: dict[str, str]) -> None:
        """Schedule one delivery. Callable from the sync decision path; never
        raises and never blocks the caller."""
        if not self.enabled:
            return
        if self.events and event_type not in self.events:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync tests) — nothing to schedule on
            return
        task = loop.create_task(self._send(event_type, body, headers))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, event_type: str, body: str, headers: dict[str, str]) -> None:
        outcome = self.chaos.plan(self.chaos_cfg)
        if outcome.active:
            self.faults += 1
        if outcome.drop:
            self.dropped += 1
            self.log.append({"ts": time.time(), "event": event_type, "status": "(dropped)"})
            return
        if outcome.latency_ms:
            await asyncio.sleep(outcome.latency_ms / 1000)
        text = body
        if outcome.malformed and len(text) > 1:
            # corrupt AFTER signing — the signature no longer matches, exactly
            # like wire corruption; a verifying receiver must reject it.
            text = text[:-1] + ',"<<malformed>>'

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.url,
                    content=text.encode("utf-8"),
                    headers={"Content-Type": "application/json", **self.headers, **headers},
                )
            status: Any = resp.status_code
            if resp.status_code < 400:
                self.sent += 1
            else:
                self.failed += 1
        except Exception as exc:  # noqa: BLE001 — delivery failure is a stat, not a crash
            self.failed += 1
            status = f"({type(exc).__name__})"
            log.warning("webhook %s → %s failed: %s", event_type, self.url, exc)
        self.log.append({"ts": time.time(), "event": event_type, "status": status})

    # -- lifecycle -------------------------------------------------------------

    async def drain(self) -> None:
        """Await every pending delivery (tests / graceful shutdown)."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def cancel(self) -> None:
        """Abandon pending deliveries (stop path)."""
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()

    def stats(self) -> dict:
        return {
            "url": self.url,
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "recent": list(self.log)[-10:],
        }


# -- provider signature schemes -------------------------------------------------


def stripe_signature(secret: str, payload: str, ts: int | None = None) -> str:
    """Stripe's documented webhook signature: ``t=<ts>,v1=<HMAC-SHA256>`` over
    ``"<ts>.<payload>"`` with the endpoint secret."""
    ts = int(time.time()) if ts is None else int(ts)
    mac = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def _adyen_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("\\", "\\\\").replace(":", "\\:")


def adyen_hmac_signature(secret: str, item: dict) -> str:
    """Adyen's documented NotificationRequestItem HMAC: base64(HMAC-SHA256)
    over the colon-joined, escaped payload fields, keyed by the **hex-decoded**
    HMAC key (falls back to raw utf-8 bytes if the secret isn't hex)."""
    amount = item.get("amount") or {}
    payload = ":".join(
        _adyen_escape(x)
        for x in (
            item.get("pspReference"),
            item.get("originalReference"),
            item.get("merchantAccountCode"),
            item.get("merchantReference"),
            amount.get("value"),
            amount.get("currency"),
            item.get("eventCode"),
            item.get("success"),
        )
    )
    try:
        key = binascii.unhexlify(secret)
    except (binascii.Error, ValueError):
        key = secret.encode("utf-8")
    mac = hmac.new(key, payload.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def paypal_transmission_headers(
    secret: str, transmission_id: str, transmission_time: str, payload: str
) -> dict[str, str]:
    """PayPal-shaped transmission headers with an **HMAC stand-in** signature
    (PayPal's real scheme is cert-based and verified via their API — labelled
    honestly as HMAC-SHA256 so offline receiver logic can still verify)."""
    mac = hmac.new(
        secret.encode(),
        f"{transmission_id}|{transmission_time}|{payload}".encode(),
        hashlib.sha256,
    )
    return {
        "Paypal-Transmission-Id": transmission_id,
        "Paypal-Transmission-Time": transmission_time,
        "Paypal-Transmission-Sig": mac.hexdigest(),
        "Paypal-Auth-Algo": "HMAC-SHA256",
    }


__all__ = [
    "WebhookEmitter",
    "stripe_signature",
    "adyen_hmac_signature",
    "paypal_transmission_headers",
]
