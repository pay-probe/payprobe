"""TcpAdapter — a universal, persistent TCP transport for request/response hosts.

Designed for the *long-running* "exchange" use-case: one TCP connection is
opened at ``connect()`` and reused for the whole run. Many requests can be in
flight at once over the single socket — a background reader correlates each
response back to its request using a key supplied by the active **protocol**.

The transport is protocol-agnostic. It owns the socket, the reader loop,
correlation, framing, sign-on, keep-alive and timeouts; what goes on the wire
is decided by a pluggable :class:`~.protocols.TcpProtocol`:

* ``protocol: "iso8583"``     — ASCII ISO 8583 (switch, acquirer, issuer sim).
* ``protocol: "header_echo"`` — host-command links that echo a leading header
                                (HSMs such as Thales payShield, and similar).

So the same adapter backs the ISO 8583 switch/acquirer targets *and* an HSM —
only the ``protocol`` and a few protocol-specific keys differ in config.

Config (all keys optional unless noted)::

    {
      "host": "10.0.1.50",            # required
      "port": 7000,                    # required
      "protocol": "iso8583",           # "iso8583" | "header_echo"

      "framing": {
        "length_prefix_bytes": 2,      # width of the length prefix (>= 1)
        "length_byte_order": "big",    # "big" | "little"
        "length_includes_prefix": false,  # does the length count its own prefix?
        "length_includes_header": true,   # does the length count the TPDU header?
        "tpdu_bytes": 0,               # inbound TPDU header width to strip
        "tpdu_outbound_hex": "",       # static TPDU prepended on send (hex)
        "encoding": "ascii"            # body text encoding (protocol default)
      },

      # protocol-specific keys (see protocols.py): correlation/fields/field_map
      # for iso8583; header_echo {...} for header_echo.

      "sign_on":   {"enabled": false, "action": "...", "payload": {...}},
      "keepalive": {"enabled": false, "interval_sec": 30, "action": "...",
                    "payload": {...}},
      "response_timeout_sec": 30,
      "connect_timeout_sec": 10
    }

``sign_on`` / ``keepalive`` default to the active protocol's health probe when
``action``/``payload`` are omitted (ISO 8583 ⇒ 0800 echo; header_echo ⇒ the
configured diagnostic command).
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..base.base_adapter import BaseAdapter, StepResult
from .. import socket_registry
from .protocols import EncodedMessage, make_protocol

log = logging.getLogger(__name__)


class TcpAdapter(BaseAdapter):

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        cfg = self.config
        # A bare catalog target (e.g. "hsm") with no backing connection/env adapter
        # arrives here as an (almost) empty config. Fail with an actionable message
        # instead of a raw ``KeyError: 'host'`` so the cause is obvious: the step
        # resolved to the universal TCP adapter but nothing supplied an endpoint.
        if "host" not in cfg or cfg.get("port") in (None, ""):
            name = cfg.get("adapter") or cfg.get("type") or "tcp"
            raise ValueError(
                f"TCP adapter '{name}' has no host/port configured — the step's "
                "target isn't bound to a connection (and no inline adapter or "
                "default connection supplies one). Pick a connection on the step, "
                "or mark a connection of this adapter type as default."
            )
        self.host = cfg["host"]
        self.port = int(cfg["port"])
        self.protocol = make_protocol(cfg)

        f = cfg.get("framing", {})
        self.prefix_bytes = int(f.get("length_prefix_bytes", 2))
        if self.prefix_bytes < 1:
            raise ValueError(
                "framing.length_prefix_bytes must be >= 1 — a length prefix is "
                "required to frame messages on a multiplexed connection."
            )
        self.byte_order = f.get("length_byte_order", "big")
        self.length_includes_prefix = bool(f.get("length_includes_prefix", False))
        self.length_includes_header = bool(f.get("length_includes_header", True))
        self.tpdu_bytes = int(f.get("tpdu_bytes", 0))
        self.tpdu_out = bytes.fromhex(f.get("tpdu_outbound_hex", "") or "")

        self.response_timeout = float(cfg.get("response_timeout_sec", 30))
        self.connect_timeout = float(cfg.get("connect_timeout_sec", 10))

        r = cfg.get("reconnect", {})
        self.reconnect_enabled = bool(r.get("enabled", True))
        self._backoff_initial = float(r.get("backoff_initial_sec", 0.5))
        self._backoff_max = float(r.get("backoff_max_sec", 30))
        self._reconnect_max = int(r.get("max_attempts", 0))  # 0 = unlimited

        self._pending: dict[str, asyncio.Future] = {}
        self._events: list[dict] = []  # unsolicited / uncorrelated messages
        self._send_lock = asyncio.Lock()
        self._closing = False
        self._sock_token: int | None = None  # /peers outbound registration
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connected = asyncio.Event()

        await self._open()

        ka = cfg.get("keepalive", {})
        if ka.get("enabled"):
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(ka))

    async def _open(self) -> None:
        """(Re)establish the socket, start the reader, and sign on."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._connected.set()

        # surface this socket on the Live Sessions outbound side
        local = self._writer.get_extra_info("sockname")
        self._sock_token = socket_registry.register(
            host=self.host,
            port=self.port,
            adapter=str(self.config.get("adapter") or self.config.get("type") or "tcp"),
            local=f"{local[0]}:{local[1]}" if isinstance(local, tuple) else "",
            owner=str(self.config.get("name") or self.config.get("_catalog_target") or ""),
        )

        sign_on = self.config.get("sign_on", {})
        if sign_on.get("enabled"):
            action, payload = self._probe("sign_on")
            try:
                await self._exchange(self.protocol.encode(action, payload), _allow_wait=False)
            except Exception as exc:
                log.warning("Sign-on to %s:%s failed: %s", self.host, self.port, exc)

    def _on_disconnect(self, exc: Exception) -> None:
        """Handle an unexpected socket drop: fail in-flight, maybe reconnect."""
        self._connected.clear()
        socket_registry.unregister(self._sock_token)
        self._sock_token = None
        if not self._closing:
            log.warning("Connection lost to %s:%s: %s", self.host, self.port, exc)
        self._fail_pending(exc)
        if not self._closing and self.reconnect_enabled:
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        backoff, attempt = self._backoff_initial, 0
        while not self._closing:
            attempt += 1
            await asyncio.sleep(backoff)
            if self._closing:
                return
            try:
                await self._open()
                log.info("Reconnected to %s:%s (attempt %d)", self.host, self.port, attempt)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Reconnect attempt %d to %s:%s failed: %s", attempt, self.host, self.port, exc
                )
                backoff = min(backoff * 2, self._backoff_max)
                if self._reconnect_max and attempt >= self._reconnect_max:
                    log.error(
                        "Giving up reconnecting to %s:%s after %d attempts",
                        self.host,
                        self.port,
                        attempt,
                    )
                    return

    # -- unsolicited / uncorrelated message sink -----------------------------

    def events(self) -> list[dict]:
        """Parsed messages the reader could not correlate (unsolicited / late)."""
        return list(self._events)

    def drain_events(self) -> list[dict]:
        """Return and clear the collected unsolicited messages."""
        evs, self._events = self._events, []
        return evs

    async def disconnect(self) -> None:
        self._closing = True
        socket_registry.unregister(self._sock_token)
        self._sock_token = None
        for task in (self._keepalive_task, self._reader_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._connected.clear()
        writer = getattr(self, "_writer", None)
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as exc:  # noqa: BLE001 — best-effort socket teardown
                log.debug("tcp adapter: error closing connection: %s", exc)

    async def health_check(self) -> bool:
        """Phase-1 component check via the protocol's health probe. Never raises."""
        try:
            action, payload = self._probe("sign_on")
            parsed = await self._exchange(self.protocol.encode(action, payload))
            return self.protocol.is_healthy(parsed)
        except Exception as exc:
            log.warning("health_check failed for %s:%s: %s", self.host, self.port, exc)
            return False

    # -- step execution ------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        try:
            msg = self.protocol.encode(action, payload)
            parsed = await self._exchange(msg)
            shaped = self.protocol.shape_response(parsed)
            return StepResult(
                success=True,
                request_payload=msg.request or payload,
                response_payload=shaped,
                duration_ms=int((time.monotonic() - start) * 1000),
                raw_log=self._wire_log(msg, parsed, shaped),
            )
        except asyncio.TimeoutError:
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"No response within {self.response_timeout}s.",
            )
        except Exception as exc:
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )

    def _wire_log(self, msg: EncodedMessage, parsed: dict, shaped: dict) -> str:
        """Human-readable wire trace: framed bytes sent + parsed reply.

        Best-effort only — never raises into the execution path.
        """
        try:
            proto = self.protocol.name
            frame = self._frame(msg.body)
            hexed = frame.hex()
            if len(hexed) > 512:
                hexed = hexed[:512] + "…"
            sent_mti = (msg.request or {}).get("mti")
            head = f"[{proto}] → sent {len(frame)} bytes"
            if sent_mti:
                head += f" · MTI {sent_mti}"
            lines = [head, f"  TX {hexed}"]
            recv_mti = parsed.get("mti")
            rhead = f"[{proto}] ← recv"
            if recv_mti:
                rhead += f" · MTI {recv_mti}"
            if shaped.get("response_code") is not None:
                rhead += f" · DE39={shaped['response_code']}"
            lines.append(rhead)
            de_list = parsed.get("de_list") or shaped.get("de_list")
            if de_list:
                lines.append("  DE " + ", ".join(str(d) for d in de_list))
            return "\n".join(lines)
        except Exception:  # pragma: no cover - logging must not break a run
            return f"{self.protocol.name}: sent {msg.request}"

    # -- send / correlate ----------------------------------------------------

    async def _exchange(self, msg: EncodedMessage, _allow_wait: bool = True) -> dict:
        if not msg.key:
            raise ValueError("Protocol produced an empty correlation key.")
        # If the socket is down, wait for an in-progress reconnect to restore it
        # (bounded by the response timeout) before giving up.
        if _allow_wait and not self._connected.is_set():
            if not self.reconnect_enabled:
                raise ConnectionError(f"{self.host}:{self.port} is not connected")
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=self.response_timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(f"{self.host}:{self.port} did not reconnect in time")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg.key] = fut
        try:
            frame = self._frame(msg.body)
            async with self._send_lock:
                self._writer.write(frame)
                await self._writer.drain()
            return await asyncio.wait_for(fut, timeout=self.response_timeout)
        finally:
            self._pending.pop(msg.key, None)

    def _probe(self, section: str) -> tuple[str, dict]:
        """Resolve (action, payload) for sign-on / keep-alive / health."""
        cfg = self.config.get(section, {})
        action = cfg.get("action", "")
        payload = cfg.get("payload")
        if payload is None:
            # legacy/explicit keys, else the protocol's own default probe
            legacy = {
                k: cfg[k]
                for k in ("mti", "values", "command", "data", "header", "message")
                if k in cfg
            }
            if legacy:
                return action, legacy
            return self.protocol.health_probe()
        return action, payload

    # -- framing -------------------------------------------------------------

    def _frame(self, body: bytes) -> bytes:
        payload = self.tpdu_out + body
        counted = len(payload) if self.length_includes_header else len(body)
        length_value = counted + (self.prefix_bytes if self.length_includes_prefix else 0)
        prefix = length_value.to_bytes(self.prefix_bytes, self.byte_order)
        return prefix + payload

    # -- background reader ---------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            while not self._closing:
                body = await self._read_frame()
                if body is None:
                    break
                self._dispatch(self.protocol.decode(body))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_disconnect(exc)
            return
        # clean EOF (peer closed) while we weren't shutting down
        if not self._closing:
            self._on_disconnect(ConnectionError("connection closed by peer"))

    async def _read_frame(self) -> bytes | None:
        prefix = await self._reader.readexactly(self.prefix_bytes)
        length_value = int.from_bytes(prefix, self.byte_order)
        core = length_value - (self.prefix_bytes if self.length_includes_prefix else 0)
        remaining = core if self.length_includes_header else core + self.tpdu_bytes
        if remaining <= 0:
            return None
        data = await self._reader.readexactly(remaining)
        return data[self.tpdu_bytes :]  # strip inbound TPDU header

    def _dispatch(self, parsed: dict) -> None:
        key = self.protocol.correlation_key(parsed)
        fut = self._pending.get(key) if key is not None else None
        if fut and not fut.done():
            fut.set_result(parsed)
        else:
            self._events.append(parsed)  # unsolicited / late / unmatched

    def _fail_pending(self, exc: Exception) -> None:
        err = ConnectionError(f"Connection lost: {exc}")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _keepalive_loop(self, ka: dict) -> None:
        interval = float(ka.get("interval_sec", 30))
        action, payload = self._probe("keepalive")
        try:
            while not self._closing:
                await asyncio.sleep(interval)
                try:
                    await self._exchange(self.protocol.encode(action, dict(payload)))
                except Exception as exc:
                    log.warning("Keepalive failed for %s:%s: %s", self.host, self.port, exc)
        except asyncio.CancelledError:
            raise


#: Backwards-compatible alias — the adapter began life as an ISO 8583-only class.
TcpIso8583Adapter = TcpAdapter
