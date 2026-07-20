"""NatsAdapter — the outbound NATS client connection (ADR-0006).

Where TCP/HTTP/gRPC adapters *dial* a peer, NATS is broker-mediated: this adapter
connects *out* to a NATS server (or cluster) and rendezvouses on subjects. It is a
reusable connection — a broker address plus a default subject/codec — callable by
name from scenarios, participant flows and groups, and overridable per environment.

Config (worker-shaped, e.g. an environment ``adapters`` entry)::

    {
      "adapter": "nats",
      "host": "nats-1", "port": 4222,          # single URL (invariant #7: one port)
      "servers": ["nats://nats-1:4222",         # …or an explicit multi-URL cluster
                  "nats://nats-2:4223"],
      "subject": "pay.auth",                    # default subject for publish/request
      "codec": "json",                          # json | bytes | iso8583
      "request_timeout_sec": 2.0,
      "auth": {"token": "…"},                   # or {user,password} / {nkey_seed} / {creds}
      "jetstream": {"stream": "PAY", "subjects": ["pay.>"], "durable": "pp"}
    }

Actions (the ``action`` a step / flow names):

* ``publish``    — fire-and-forget publish to ``subject`` (payload ``.subject``
  overrides the connection default).
* ``request``    — request/reply: publish and await one reply within
  ``request_timeout_sec``; the decoded reply is the StepResult response.
* ``js_publish`` — JetStream publish; the broker ack (stream + sequence) is
  returned, so a durable write is confirmed rather than fire-and-forget.

Any other action name is treated as a plain ``publish`` (the safe default).

``nats-py`` is imported lazily via :func:`_nats_connect` so a missing dependency
degrades to "adapter unavailable" at registry import (see ``registry.py``) and so
tests can substitute a fake client without a running broker.
"""

from __future__ import annotations

import time
from typing import Any

from ..base.base_adapter import BaseAdapter, StepResult
from . import codecs


async def _nats_connect(servers: list[str], **opts: Any):
    """Open a NATS connection. Isolated in one function so ``nats-py`` is imported
    lazily (optional dep) and tests can monkeypatch the whole connect."""
    import nats  # noqa: PLC0415 — lazy: keep nats-py optional

    return await nats.connect(servers=servers, **opts)


def _servers_from(config: dict) -> list[str]:
    """Resolve the broker URL list. An explicit ``servers`` wins; otherwise build
    one URL from ``host``/``port`` (the single-``port`` connection model)."""
    servers = config.get("servers")
    if servers:
        return [str(s) for s in (servers if isinstance(servers, list) else [servers])]
    host = str(config.get("host") or "127.0.0.1")
    port = int(config.get("port") or 4222)
    if "://" in host:  # host already a full nats:// url
        return [host]
    return [f"nats://{host}:{port}"]


def _stream_limits(js_cfg: dict) -> dict:
    """Translate a connection's ``jetstream`` block into add_stream kwargs that
    BOUND the stream so a producer-only load test can't fill storage forever:
    ``retention`` (``limits``/``workqueue``/``interest`` — workqueue deletes a
    message once a consumer acks it), ``max_msgs``, ``max_bytes``, ``max_age``
    (seconds). Absent keys keep NATS defaults (unbounded limits retention)."""
    out: dict[str, Any] = {}
    ret = js_cfg.get("retention")
    if ret:
        try:  # map the string to the nats-py enum when the lib is present
            from nats.js.api import RetentionPolicy  # noqa: PLC0415

            out["retention"] = RetentionPolicy(str(ret).lower())
        except Exception:  # noqa: BLE001 — pass the raw value (fakes/older libs)
            out["retention"] = ret
    for k in ("max_msgs", "max_bytes"):
        if js_cfg.get(k) is not None:
            out[k] = int(js_cfg[k])
    if js_cfg.get("max_age") is not None:
        # nats-py expects nanoseconds; accept seconds on the connection.
        out["max_age"] = float(js_cfg["max_age"]) * 1e9
    return out


def _connect_opts(config: dict) -> dict:
    """Translate the connection's ``auth`` block into nats-py connect kwargs.
    Credentials ride the connection like every other secret (encrypted at rest,
    masked on read — invariant #8); here they are already decrypted."""
    auth = config.get("auth") or {}
    opts: dict[str, Any] = {}
    if auth.get("token"):
        opts["token"] = str(auth["token"])
    if auth.get("user"):
        opts["user"] = str(auth["user"])
    if auth.get("password"):
        opts["password"] = str(auth["password"])
    # nkey seed (string form) and JWT creds file — pass through whichever is set.
    if auth.get("nkey_seed"):
        opts["nkeys_seed_str"] = str(auth["nkey_seed"])
    if auth.get("creds"):
        opts["user_credentials"] = str(auth["creds"])
    if config.get("name"):
        opts["name"] = str(config["name"])
    # keep a bounded connect so a dead broker fails a health_check fast
    opts.setdefault("connect_timeout", float(config.get("connect_timeout_sec", 5)))
    opts.setdefault("max_reconnect_attempts", int(config.get("max_reconnect_attempts", 3)))
    return opts


class NatsAdapter(BaseAdapter):
    async def connect(self) -> None:
        cfg = self.config
        self.servers = _servers_from(cfg)
        self.subject = cfg.get("subject") or ""
        self.codec = str(cfg.get("codec") or codecs.DEFAULT_CODEC).lower()
        self.request_timeout = float(cfg.get("request_timeout_sec", 2.0))
        self._js_cfg = cfg.get("jetstream") or {}
        #: stop-ownership (invariant #5): remember what THIS connection created in
        #: JetStream so teardown removes only its own consumers, never a stream it
        #: found pre-existing or the cluster itself.
        self._created_stream: str | None = None
        self._created_consumer: tuple[str, str] | None = None
        self._nc = await _nats_connect(self.servers, **_connect_opts(cfg))
        self._js = None
        if self._js_cfg.get("stream"):
            await self.ensure_jetstream()

    def _jetstream(self):
        if self._js is None:
            self._js = self._nc.jetstream()
        return self._js

    async def ensure_jetstream(self) -> dict:
        """Declare the connection's stream/consumer, **created-if-missing** and
        ownership-tracked. A stream/consumer that already exists is left as found
        (not owned). ``jetstream`` block::

            {"stream": "PAY", "subjects": ["pay.>"], "durable": "pp",
             "ack_wait": 30, "delete_on_stop": false}

        Idempotent — safe to call from every connect (each load worker, etc.)."""
        cfg = self._js_cfg
        stream = cfg.get("stream")
        if not stream:
            return {}
        js = self._jetstream()
        created_stream = False
        try:
            await js.stream_info(stream)  # exists → pre-existing, not ours
        except Exception:  # noqa: BLE001 — NotFound (or any lookup miss) → create
            subjects = cfg.get("subjects") or ([self.subject] if self.subject else [f"{stream}.>"])
            await js.add_stream(name=stream, subjects=list(subjects), **_stream_limits(cfg))
            created_stream = True
        self._created_stream = stream if created_stream else None

        durable = cfg.get("durable")
        created_consumer = False
        if durable:
            try:
                await js.consumer_info(stream, durable)  # exists → not ours
            except Exception:  # noqa: BLE001 — create the durable consumer
                from nats.js.api import ConsumerConfig  # noqa: PLC0415 — lazy

                cc = ConsumerConfig(
                    durable_name=durable,
                    ack_wait=float(cfg["ack_wait"]) if cfg.get("ack_wait") else None,
                )
                await js.add_consumer(stream, cc)
                created_consumer = True
        self._created_consumer = (stream, durable) if created_consumer else None
        return {
            "stream": stream,
            "created_stream": created_stream,
            "durable": durable,
            "created_consumer": created_consumer,
        }

    async def _teardown_jetstream(self) -> None:
        """Remove only what this connection created (invariant #5). Consumers are
        always cleaned up; a stream is deleted only when explicitly opted in via
        ``jetstream.delete_on_stop`` — streams are durable shared infrastructure
        that other participants may still use."""
        js = self._js
        if js is None:
            return
        if self._created_consumer:
            stream, durable = self._created_consumer
            try:
                await js.delete_consumer(stream, durable)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._created_consumer = None
        if self._created_stream and self._js_cfg.get("delete_on_stop"):
            try:
                await js.delete_stream(self._created_stream)
            except Exception:  # noqa: BLE001
                pass
            self._created_stream = None

    def _subject_for(self, action: str, payload: dict) -> str:
        subj = (payload or {}).get("subject") or self.subject
        if not subj:
            raise ValueError(
                f"nats {action}: no subject — set one on the connection or in the "
                "payload ('subject')"
            )
        return str(subj)

    #: Payload keys that control *how* a message is sent, not its body. Stripped
    #: before encoding so they never leak into the message a subscriber sees.
    _CONTROL_KEYS = ("subject", "timeout")

    def _body_of(self, payload: Any) -> Any:
        """The message body to encode. Control keys (subject/timeout) are removed;
        an explicit ``body`` key (as the catalog steps produce) is unwrapped so the
        message is exactly that body, not ``{"body": {...}}``."""
        if not isinstance(payload, dict):
            return payload
        if "body" in payload and set(payload) <= {"body", *self._CONTROL_KEYS}:
            return payload["body"]
        return {k: v for k, v in payload.items() if k not in self._CONTROL_KEYS}

    async def execute(self, action: str, payload: dict) -> StepResult:
        act = (action or "publish").lower()
        start = time.monotonic()
        try:
            data = codecs.encode(self._body_of(payload), self.codec, self.config)
            if act == "request":
                resp = await self._do_request(payload, data)
            elif act == "js_publish":
                resp = await self._do_js_publish(payload, data)
            else:  # publish (and any unrecognised verb) — fire and forget
                resp = await self._do_publish(payload, data)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a StepResult
            return StepResult(
                success=False,
                request_payload={"action": act, "payload": payload},
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        return StepResult(
            success=resp.get("success", True),
            request_payload={"action": act, "subject": resp.get("subject"), "payload": payload},
            response_payload=resp,
            duration_ms=int((time.monotonic() - start) * 1000),
            error=resp.get("error"),
        )

    async def _do_publish(self, payload: dict, data: bytes) -> dict:
        subject = self._subject_for("publish", payload)
        await self._nc.publish(subject, data)
        return {"subject": subject, "published": True, "bytes": len(data)}

    async def _do_request(self, payload: dict, data: bytes) -> dict:
        subject = self._subject_for("request", payload)
        timeout = float((payload or {}).get("timeout") or self.request_timeout)
        msg = await self._nc.request(subject, data, timeout=timeout)
        return {
            "subject": subject,
            "reply_subject": getattr(msg, "subject", None),
            "data": codecs.decode(getattr(msg, "data", b""), self.codec, self.config),
        }

    async def _do_js_publish(self, payload: dict, data: bytes) -> dict:
        subject = self._subject_for("js_publish", payload)
        js = self._jetstream()
        ack = await js.publish(subject, data)
        return {
            "subject": subject,
            "stream": getattr(ack, "stream", None),
            "seq": getattr(ack, "seq", None),
            "duplicate": bool(getattr(ack, "duplicate", False)),
            "acked": True,
        }

    async def health_check(self) -> bool:
        """Best-effort reachability: connect (if needed) and round-trip a flush.
        Never raises — returns False on any transport error."""
        try:
            if getattr(self, "_nc", None) is None:
                await self.connect()
            await self._nc.flush(timeout=float(self.config.get("connect_timeout_sec", 5)))
            return bool(getattr(self._nc, "is_connected", True))
        except Exception:  # noqa: BLE001
            return False

    async def disconnect(self) -> None:
        nc = getattr(self, "_nc", None)
        if nc is None:
            return
        await self._teardown_jetstream()  # before the connection drops
        try:
            await nc.drain()
        except Exception:  # noqa: BLE001 — best-effort; fall back to hard close
            try:
                await nc.close()
            except Exception:  # noqa: BLE001
                pass
        self._nc = None
        self._js = None
