"""NatsResponder — a rules-driven NATS subscriber simulator (ADR-0006).

The NATS counterpart of :class:`~worker.adapters.tcp.responder.TcpResponder` and
:class:`~worker.adapters.http.responder.HttpResponder`: instead of binding a
socket, it connects *out* to the broker and **subscribes** to one or more subjects
(optionally in a queue group), decodes each message through the codec layer, and —
for request/reply messages (those carrying a reply inbox) — publishes a templated
answer. So PayProbe can stand in for a NATS-based switch/issuer/event consumer.

It deliberately mirrors the responder public surface (``start``/``stop``/
``serve_forever``/``port``/``protocol``/``rules``/``received``/``faults``/
``stats``/``peers``/``reset_stats``/``set_chaos``/``global_chaos``/``_label``/
``by_mti``/``by_rc``), so it is drop-in compatible with the orchestrator's
saved-simulator registry, Live Sessions and the portal Simulators page.

Because NATS is broker-mediated it binds **no port** — ``port`` is always None; the
uniqueness resource is ``(server, subject, queue_group)``, handled by the planner
(ADR-0006 invariant #4 reading). ``by_mti`` is keyed by **subject** (the NATS analog
of by-MTI); ``by_rc`` by the reply's response code.

Config (JSON-friendly)::

    {
      "protocol": "nats",
      "host": "nats-1", "port": 4222,
      "subjects": ["pay.auth", "pay.reversal"],   # or a single "subject"
      "queue_group": "issuers",                    # optional load-balanced group
      "codec": "json",                             # json | bytes | iso8583
      "rules": [
        {"when": {"subject": {"eq": "pay.auth"}, "body": {"amount": {"gte": 1000}}},
         "respond": {"json": {"decision": "DECLINE"}}}
      ],
      "default": {"json": {"decision": "APPROVE"}}
    }

Chaos / fault injection reuses :mod:`worker.adapters.tcp.chaos`: *latency* delays
the reply; *drop* publishes nothing (bucketed ``(drop)``); *malformed* corrupts the
reply body; *partial* truncates it. A top-level ``chaos`` block applies to every
reply; a rule's ``respond.chaos`` overrides it — so the chaos dial and storms work
against a NATS simulator unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..tcp.chaos import ChaosEngine
from . import codecs
from .adapter import _connect_opts, _nats_connect, _servers_from

log = logging.getLogger(__name__)


def _match_condition(value: Any, cond: Any) -> bool:
    """Does a message value satisfy a rule condition? Same operator vocabulary as
    the HTTP/TCP responders (eq/prefix/contains/in/regex/gte/lte)."""
    sval = "" if value is None else str(value)
    if isinstance(cond, dict):
        if "eq" in cond and sval != str(cond["eq"]):
            return False
        if "prefix" in cond and not sval.startswith(str(cond["prefix"])):
            return False
        if "contains" in cond and str(cond["contains"]) not in sval:
            return False
        if "in" in cond and sval not in [str(x) for x in cond["in"]]:
            return False
        if "regex" in cond:
            import re

            if not re.search(str(cond["regex"]), sval):
                return False
        if "gte" in cond and not _num_ge(value, cond["gte"]):
            return False
        if "lte" in cond and not _num_le(value, cond["lte"]):
            return False
        return True
    return sval == str(cond)


def _num_ge(value: Any, bound: Any) -> bool:
    try:
        return float(value) >= float(bound)
    except (TypeError, ValueError):
        return str(value) >= str(bound)


def _num_le(value: Any, bound: Any) -> bool:
    try:
        return float(value) <= float(bound)
    except (TypeError, ValueError):
        return str(value) <= str(bound)


def _dig(body: Any, path: str) -> Any:
    """Resolve a dotted ``a.b.c`` path into a decoded body (dicts/lists)."""
    cur = body
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


class NatsResponder:
    PROTOCOL = "nats"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.protocol = config.get("protocol", self.PROTOCOL)
        self.codec = str(config.get("codec") or codecs.DEFAULT_CODEC).lower()
        self.rules = config.get("rules", [])
        self.default = config.get("default", {"json": {"status": "ok"}})

        #: subjects this responder answers on, and the optional queue group.
        subj = config.get("subjects") or config.get("subject") or []
        self.subjects: list[str] = [subj] if isinstance(subj, str) else list(subj)
        self.queue_group: str = str(config.get("queue_group") or "")
        #: JetStream **consumer** mode (ADR-0006): when the config carries a
        #: ``jetstream`` block with a ``stream``, subscribe as a durable JS
        #: consumer and ACK each message — draining the stream (with workqueue
        #: retention the message is then deleted) — instead of a core subscribe
        #: that receives but never acks, leaving messages to pile up in storage.
        self._js_cfg: dict = config.get("jetstream") or {}

        # chaos / fault injection
        self.global_chaos: dict = config.get("chaos") or {}
        self.chaos = ChaosEngine(self.global_chaos.get("seed"))
        self.faults = 0

        self.received: list[dict] = []

        # broker plumbing
        self._nc = None
        self._subs: list = []
        self._closed: asyncio.Event = asyncio.Event()

        # -- live metrics (kept key-compatible with TcpResponder.stats) --------
        self.started_at = time.time()
        #: request volume keyed by subject (the NATS analog of by-MTI).
        self.by_mti: dict[str, int] = {}
        #: reply volume keyed by response code.
        self.by_rc: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect to the broker and subscribe to every configured subject.
        Binds no port (returns None) — NATS is broker-mediated."""
        if not self.subjects:
            raise ValueError(
                "nats responder has no subjects to subscribe to — set 'subject' "
                "or 'subjects' on the connection"
            )
        self._closed = asyncio.Event()
        servers = _servers_from(self.config)
        self._nc = await _nats_connect(servers, **_connect_opts(self.config))
        if self._js_cfg.get("stream"):
            # JetStream consumer: a durable push subscription that ACKs, so the
            # stream is drained (produce → persist → consume → ack), not filled.
            js = self._nc.jetstream()
            stream = self._js_cfg["stream"]
            durable = self._js_cfg.get("durable")
            for subject in self.subjects:
                sub = await js.subscribe(
                    subject, stream=stream, durable=durable, cb=self._handle_js, manual_ack=True
                )
                self._subs.append(sub)
        else:
            queue = self.queue_group or ""
            for subject in self.subjects:
                sub = await self._nc.subscribe(subject, queue=queue, cb=self._handle)
                self._subs.append(sub)
        return None

    async def _handle_js(self, msg: Any) -> None:
        """JetStream delivery: process like any message, then ACK so the broker
        advances the consumer (and, under workqueue retention, deletes it)."""
        try:
            await self._handle(msg)
        finally:
            try:
                await msg.ack()
            except Exception as exc:  # noqa: BLE001 — a failed ack redelivers, fine
                log.debug("nats responder: ack error: %s", exc)

    @property
    def port(self) -> None:
        """NATS binds no port — the uniqueness resource is the subject claim."""
        return None

    def listening(self) -> bool:
        """Liveness for a **port-less** responder: True while connected to the
        broker with active subscriptions. Readiness checks that count port-bound
        listeners use this instead of ``port`` for NATS (ADR-0006)."""
        return self._nc is not None and len(self._subs) > 0

    async def stop(self) -> None:
        self._closed.set()
        # drain only the subscriptions we created (stop-ownership: never touch
        # the broker or subscriptions another run owns).
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("nats responder: unsubscribe error: %s", exc)
        self._subs.clear()
        nc = self._nc
        self._nc = None
        if nc is not None:
            try:
                await nc.drain()
            except Exception:  # noqa: BLE001
                try:
                    await nc.close()
                except Exception:  # noqa: BLE001
                    pass

    async def serve_forever(self) -> None:
        """Block until :meth:`stop` is called (subscriptions run in the loop)."""
        await self._closed.wait()

    def set_chaos(self, cfg: dict | None) -> dict:
        """Swap the live global chaos block at runtime (mirrors the other
        responders' control surface). Empty/None turns chaos off."""
        cfg = dict(cfg or {})
        if cfg.get("seed") is not None:
            self.chaos = ChaosEngine(cfg.get("seed"))
        self.global_chaos = cfg
        self.config["chaos"] = cfg
        return cfg

    # -- metrics -------------------------------------------------------------

    def reset_stats(self) -> None:
        self.received.clear()
        self.faults = 0
        self.by_mti.clear()
        self.by_rc.clear()
        self.started_at = time.time()

    def peers(self) -> list[dict]:
        """NATS is broker-mediated — no client holds a socket to us, so Live
        Sessions shows per-subject subscription rows instead of remote peers."""
        now = time.time()
        return [
            {
                "peer": f"subject:{s}" + (f"@{self.queue_group}" if self.queue_group else ""),
                "since": round(self.started_at, 3),
                "connected_s": round(now - self.started_at, 1),
                "messages": self.by_mti.get(s, 0),
                "idle_s": 0.0,
            }
            for s in self.subjects
        ]

    def stats(self) -> dict:
        elapsed = max(1e-6, time.time() - self.started_at)
        total = len(self.received)
        return {
            "received": total,
            "faults": self.faults,
            "elapsed_s": round(elapsed, 1),
            "avg_rps": round(total / elapsed, 2),
            "by_mti": dict(self.by_mti),
            "by_response_code": dict(self.by_rc),
        }

    def _response_code(self, action: dict) -> str:
        """The reply's response code — drives by-response-code. For JSON payloads
        a ``status`` field is the natural code; otherwise 'reply'/'drop'."""
        if action.get("drop"):
            return "(drop)"
        body = action.get("json", action.get("body"))
        if isinstance(body, dict):
            for k in ("status", "decision", "response_code", "code"):
                if body.get(k) is not None:
                    return str(body[k])
        return "reply"

    # -- message handling ----------------------------------------------------

    async def _handle(self, msg: Any) -> None:
        parsed = self._decode(msg)
        self.received.append(parsed)
        subject = parsed.get("subject", "?")
        self.by_mti[subject] = self.by_mti.get(subject, 0) + 1

        action = await self._resolve_async(parsed)

        merged = {**self.global_chaos, **(action.get("chaos") or {})}
        outcome = self.chaos.plan(merged)
        if outcome.active:
            self.faults += 1
            parsed["chaos"] = outcome.summary()

        delay = action.get("delay_ms", 0) + outcome.latency_ms
        if delay:
            await asyncio.sleep(delay / 1000)

        reply_to = action.get("subject") or parsed.get("reply")
        if action.get("drop") or outcome.drop:
            self.by_rc["(drop)"] = self.by_rc.get("(drop)", 0) + 1
            return  # publish nothing — the requester times out

        self.by_rc[self._response_code(action)] = self.by_rc.get(self._response_code(action), 0) + 1
        if not reply_to:
            return  # fire-and-forget inbound (publish, no inbox) — nothing to answer

        body_obj = action.get("json", action.get("body", {}))
        data = codecs.encode(body_obj, self.codec, self.config)
        if outcome.malformed and self.codec != "iso8583":
            data = (data or b"{}")[:-1] + b',"<<malformed>>' if len(data) > 1 else b"{"
        if outcome.partial:
            data = data[: max(1, len(data) // 2)]
        await self._nc.publish(reply_to, data)

    def _decode(self, msg: Any) -> dict:
        """Parse an inbound NATS message into the dict ``_resolve`` matches on."""
        raw = getattr(msg, "data", b"") or b""
        return {
            "subject": getattr(msg, "subject", None),
            "reply": getattr(msg, "reply", "") or "",
            "body": codecs.decode(raw, self.codec, self.config),
            "raw": raw,
        }

    # -- rule resolution -----------------------------------------------------

    async def _resolve_async(self, parsed: dict) -> dict:
        """Async hook (the flow responder overrides this to walk a graph). The
        base responder is rules-driven and synchronous."""
        return self._resolve(parsed)

    def _resolve(self, parsed: dict) -> dict:
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})
        return dict(self.default)

    def _matches(self, when: dict, parsed: dict) -> bool:
        if "subject" in when and not _match_condition(parsed.get("subject", ""), when["subject"]):
            return False
        body = parsed.get("body")
        for key, cond in (when.get("body") or {}).items():
            if not _match_condition(_dig(body, key), cond):
                return False
        return True
