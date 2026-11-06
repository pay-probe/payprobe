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

      "framing": {                     # see framing.FrameSpec — one model, both ends
        "length_prefix_bytes": 2,      # width of the length prefix (>= 1)
        "length_encoding": "binary",   # "binary" integer | "ascii" digits (e.g. 0043;
                                       #   vendor alias "chardec") | "bcd"
        "length_byte_order": "big",    # "big" | "little" (binary prefixes only)
        "length_includes_prefix": false,  # does the length count its own prefix?
        "length_includes_header": true,   # does the length count the TPDU / header?
        "tpdu_bytes": 0,               # inbound TPDU header width to strip
        "tpdu_outbound_hex": "",       # static TPDU prepended on send (hex)
        "header": "",                  # fixed message header after the prefix (ASCII)
        "header_hex": "",              # … or as hex bytes (one of the two)
        "header_bytes": null,          # inbound header width to strip (default: len(header))
        "encoding": "ascii"            # body text encoding (protocol default)
      },
      "mhdr": "",                      # vendor-style alias of framing.header

      # protocol-specific keys (see protocols.py): correlation/fields/field_map
      # for iso8583; header_echo {...} for header_echo.

      "sign_on":   {"enabled": false, "action": "...", "payload": {...}},
      "keepalive": {"enabled": false, "interval_sec": 30, "action": "...",
                    "payload": {...}},
      "health_check": {"mode": "probe", "action": "...", "payload": {...},
                       "settle_ms": 200},
      "response_timeout_sec": 30,
      "connect_timeout_sec": 10
    }

``sign_on`` / ``keepalive`` default to the active protocol's health probe when
``action``/``payload`` are omitted (ISO 8583 ⇒ 0800 echo; header_echo ⇒ the
configured diagnostic command).

``health_check.mode`` decides what *Test connection* / diagnostics put on the
wire: ``probe`` (default, today's behaviour — the sign-on message or the
protocol's default probe, healthy when the host answers), ``connect`` (nothing
is sent: healthy when the socket is still open after ``settle_ms``, which
catches hosts that hang up on an unwelcome client within milliseconds) or
``message`` (send ``health_check.action``/``payload`` and judge the reply).

Every send and every inbound frame is recorded in a bounded **wire journal**
(:meth:`TcpAdapter.wire_events`) and logged on the ``worker.adapters.tcp.wire``
logger — the exact outbound bytes, the first bytes of inbound data, and the
timing of a peer close relative to the last send. The journal is DEBUG unless
``PAYPROBE_TCP_WIRE_LOG=1`` promotes it to INFO. Frames carry whatever the step
sent (PANs included), exactly like ``StepResult.raw_log`` already does.
:meth:`TcpAdapter.preview_frame` is the dry run: the frame a config would send
for a message, without opening a socket.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

from .. import socket_registry
from ..base.base_adapter import BaseAdapter, StepResult
from .framing import FrameSpec
from .protocols import EncodedMessage, make_protocol

log = logging.getLogger(__name__)
#: Byte-level trace of every TCP exchange. DEBUG by default; promoted to INFO
#: by ``PAYPROBE_TCP_WIRE_LOG=1`` so an operator can watch a host without
#: turning the whole worker to DEBUG.
wire_log = logging.getLogger("worker.adapters.tcp.wire")
_WIRE_LEVEL = (
    logging.INFO
    if os.environ.get("PAYPROBE_TCP_WIRE_LOG", "").lower() in ("1", "true", "yes")
    else logging.DEBUG
)
#: How much of a frame the journal / log keeps as hex (bytes) — the same cap
#: as ``StepResult.raw_log``; an ISO 8583 message fits whole.
WIRE_HEX_BYTES = 256
WIRE_JOURNAL_SIZE = 200

HEALTH_MODES = ("probe", "connect", "message")


class WireJournal:
    """Bounded, in-order record of what crossed one socket and when.

    Each event is a plain dict (JSON-friendly) so an API can return it:
    ``{"t_ms", "dir", "bytes", "hex", "note"}`` where ``dir`` is one of
    ``open`` / ``tx`` / ``rx`` / ``close`` / ``error`` and ``t_ms`` is the time
    since the journal was created. ``hex`` holds the first
    :data:`WIRE_HEX_BYTES` bytes of the frame — a whole ISO 8583 message in
    practice, bounded so a 200-entry journal stays small.
    """

    def __init__(self, label: str, size: int = WIRE_JOURNAL_SIZE) -> None:
        self.label = label
        self._events: deque[dict] = deque(maxlen=size)
        self._t0 = time.monotonic()
        self.last_tx_at: float | None = None
        self.bytes_since_tx = 0

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def record(self, direction: str, data: bytes = b"", note: str = "") -> dict:
        ev = {
            "t_ms": self._now_ms(),
            "dir": direction,
            "bytes": len(data),
            "hex": data[:WIRE_HEX_BYTES].hex(),
            "note": note,
        }
        if direction == "tx":
            self.last_tx_at = time.monotonic()
            self.bytes_since_tx = 0
        elif direction == "rx":
            self.bytes_since_tx += len(data)
        self._events.append(ev)
        wire_log.log(
            _WIRE_LEVEL,
            "%s %s %d bytes%s%s",
            self.label,
            {"tx": "→", "rx": "←", "open": "⇢", "close": "⇠", "error": "✗"}.get(
                direction, direction
            ),
            len(data),
            f" {ev['hex']}{'…' if len(data) > WIRE_HEX_BYTES else ''}" if data else "",
            f" · {note}" if note else "",
        )
        return ev

    def since_last_tx_ms(self) -> int | None:
        if self.last_tx_at is None:
            return None
        return int((time.monotonic() - self.last_tx_at) * 1000)

    def events(self) -> list[dict]:
        return list(self._events)


class TcpAdapter(BaseAdapter):

    # -- configuration (no socket needed) --------------------------------------

    def _configure(self) -> None:
        """Resolve every connection key the transport honours. Pure: reads
        ``self.config``, binds nothing, so dry runs can use it too."""
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

        self.frame_spec = FrameSpec.from_config(cfg)
        # Mirrors kept for callers that read the knobs directly (tests, proxy).
        self.prefix_bytes = self.frame_spec.prefix_bytes
        self.byte_order = self.frame_spec.byte_order
        self.length_encoding = self.frame_spec.length_encoding
        self.length_includes_prefix = self.frame_spec.length_includes_prefix
        self.length_includes_header = self.frame_spec.length_includes_header
        self.tpdu_bytes = self.frame_spec.tpdu_bytes
        self.tpdu_out = self.frame_spec.tpdu_out
        if "header" in cfg and self.protocol.name == "iso8583":
            # The one key people reach for first — and the one this transport
            # never read. Say so instead of dropping it on the floor.
            log.warning(
                "tcp %s:%s: top-level 'header' is not a TcpAdapter key and is ignored — "
                "a fixed ISO message header is framing.header / framing.header_hex "
                "(or the vendor-style 'mhdr')",
                self.host,
                self.port,
            )

        self.response_timeout = float(cfg.get("response_timeout_sec", 30))
        self.connect_timeout = float(cfg.get("connect_timeout_sec", 10))

        hc = cfg.get("health_check") or {}
        self.health_mode = str(hc.get("mode", "probe")).lower()
        if self.health_mode not in HEALTH_MODES:
            raise ValueError(
                f"health_check.mode must be one of {', '.join(HEALTH_MODES)}, "
                f"got {self.health_mode!r}"
            )
        self.health_settle = float(hc.get("settle_ms", 200)) / 1000
        if self.health_mode == "message" and not self._section_has_message(hc):
            raise ValueError(
                "health_check.mode 'message' needs the message to send: "
                "health_check.payload (and/or action), e.g. "
                '{"mode": "message", "payload": {"mti": "0800", "values": {"70": "001"}}}'
            )

        r = cfg.get("reconnect", {})
        self.reconnect_enabled = bool(r.get("enabled", True))
        self._backoff_initial = float(r.get("backoff_initial_sec", 0.5))
        self._backoff_max = float(r.get("backoff_max_sec", 30))
        self._reconnect_max = int(r.get("max_attempts", 0))  # 0 = unlimited

        self.journal = WireJournal(f"tcp {self.host}:{self.port}")
        #: Outcome of the last :meth:`health_check`, for callers that want the
        #: reason (Test connection, diagnostics), not just the boolean.
        self.last_health: dict = {}

    @staticmethod
    def _section_has_message(section: dict) -> bool:
        return section.get("payload") is not None or any(
            k in section for k in ("mti", "values", "command", "data", "header", "message")
        )

    def preview_frame(self, action: str = "", payload: dict | None = None) -> dict:
        """Dry run: the exact frame this config would send for ``(action,
        payload)`` — no socket. Returns the frame as hex plus its blocks
        (prefix / TPDU / header / body) and the human-readable request. With
        no action or payload the health-check message is previewed, which is
        what *Test connection* puts on the wire."""
        if not hasattr(self, "frame_spec"):
            self._configure()
        if not action and payload is None:
            action, payload = self._probe("health_check")
        payload = dict(payload or {})
        check = getattr(self.protocol, "check", None)
        issues = check(action, payload) if callable(check) else []
        if issues:
            raise ValueError("payload cannot be packed: " + "; ".join(issues))
        msg = self.protocol.encode(action, payload)
        out = self.frame_spec.parts(msg.body)
        out["framing"] = self.frame_spec.describe()
        out["request"] = msg.request
        out["protocol"] = self.protocol.name
        out["health_check_mode"] = self.health_mode
        return out

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        self._configure()
        cfg = self.config

        self._pending: dict[str, asyncio.Future] = {}
        self._rx_frames: dict[str, bytes] = {}  # correlation key -> raw reply frame
        self._events: list[dict] = []  # unsolicited / uncorrelated messages
        self._send_lock = asyncio.Lock()
        self._closing = False
        self._sock_token: int | None = None  # /peers outbound registration
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._last_disconnect: dict = {}

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
        self._last_disconnect = {}

        # surface this socket on the Live Sessions outbound side
        local = self._writer.get_extra_info("sockname")
        local_s = f"{local[0]}:{local[1]}" if isinstance(local, tuple) else ""
        self.journal.record(
            "open", note=f"connected from {local_s} · framing {self.frame_spec.describe()}"
        )
        self._sock_token = socket_registry.register(
            host=self.host,
            port=self.port,
            adapter=str(self.config.get("adapter") or self.config.get("type") or "tcp"),
            local=local_s,
            owner=str(self.config.get("name") or self.config.get("_catalog_target") or ""),
        )

        sign_on = self.config.get("sign_on", {})
        if sign_on.get("enabled"):
            action, payload = self._probe("sign_on")
            try:
                await self._exchange(
                    self.protocol.encode(action, payload), _allow_wait=False, note="sign-on"
                )
            except Exception as exc:  # noqa: BLE001 - deliberate catch-all
                log.warning("Sign-on to %s:%s failed: %s", self.host, self.port, exc)

    def _on_disconnect(self, exc: Exception) -> None:
        """Handle an unexpected socket drop: fail in-flight, maybe reconnect."""
        self._connected.clear()
        socket_registry.unregister(self._sock_token)
        self._sock_token = None
        since = self.journal.since_last_tx_ms()
        partial = getattr(exc, "partial", b"") or b""
        expected = getattr(exc, "expected", None)
        closed_by_peer = isinstance(exc, (asyncio.IncompleteReadError, ConnectionError, OSError))
        note = f"peer closed: {exc}" if closed_by_peer else f"reader stopped: {exc}"
        if since is not None:
            note += f" · {since} ms after the last send"
            note += f", {self.journal.bytes_since_tx} bytes read since"
        if expected:
            note += f" · {len(partial)} of {expected} expected bytes arrived"
        self._last_disconnect = {
            "error": str(exc),
            "ms_after_last_tx": since,
            "bytes_read_since_tx": self.journal.bytes_since_tx,
            "partial_hex": partial.hex(),
        }
        self.journal.record("close", partial, note)
        if not self._closing:
            log.warning("Connection lost to %s:%s: %s", self.host, self.port, note)
        self._fail_pending(exc)
        if (
            not self._closing
            and self.reconnect_enabled
            and (self._reconnect_task is None or self._reconnect_task.done())
        ):
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

    def wire_events(self) -> list[dict]:
        """The wire journal: every open / tx / rx / close on this socket, with
        the first bytes as hex and millisecond timing (see :class:`WireJournal`)."""
        journal = getattr(self, "journal", None)
        return journal.events() if journal else []

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
        """Phase-1 component check. Never raises; ``last_health`` holds the why.

        ``health_check.mode`` picks the check: ``probe`` sends the sign-on /
        protocol default probe and expects a reply, ``message`` sends the
        configured message, ``connect`` sends nothing and only requires the
        socket to survive ``settle_ms`` (a host that rejects the client closes
        within milliseconds — the journal records how many).
        """
        if not hasattr(self, "journal"):  # health_check() before connect()
            self.last_health = {"mode": None, "healthy": False, "error": "not connected"}
            return False
        start = time.monotonic()
        report: dict = {"mode": self.health_mode, "healthy": False, "error": None, "sent": None}
        try:
            if self.health_mode == "connect":
                healthy = await self._health_connect_only(report)
            else:
                section = "health_check" if self.health_mode == "message" else "sign_on"
                action, payload = self._probe(section)
                msg = self.protocol.encode(action, dict(payload))
                frame = self.frame_spec.frame(msg.body)
                report["sent"] = {
                    "bytes": len(frame),
                    "hex": frame.hex(),
                    "request": msg.request,
                    "source": (
                        "health_check message"
                        if self.health_mode == "message"
                        else (
                            "sign_on message"
                            if self._section_has_message(self.config.get("sign_on") or {})
                            else f"{self.protocol.name} default probe"
                        )
                    ),
                }
                parsed = await self._exchange(msg, note=f"health probe ({self.health_mode})")
                report["reply"] = {"bytes": len(msg.reply), "hex": msg.reply.hex()}
                healthy = self.protocol.is_healthy(parsed)
                if not healthy:
                    report["error"] = "the host answered, but not with a healthy reply"
        except TimeoutError:
            healthy = False
            report["error"] = self._with_hint(
                f"no reply within {self.response_timeout:g}s"
                " — the host did not answer this probe; for hosts that do not echo "
                "0800 set health_check.mode to 'connect' (send nothing) or 'message' "
                "(your own sign-on)"
            )
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            healthy = False
            report["error"] = self._with_hint(f"{type(exc).__name__}: {exc}")
        report["healthy"] = healthy
        report["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        report["wire"] = self.wire_events()
        self.last_health = report
        if not healthy:
            log.warning(
                "health_check failed for %s:%s (%s): %s",
                self.host,
                self.port,
                self.health_mode,
                report["error"],
            )
        return healthy

    async def _health_connect_only(self, report: dict) -> bool:
        if not self._connected.is_set():
            report["error"] = self._with_hint("not connected")
            return False
        # Give a host that hangs up on sight time to do so.
        await asyncio.sleep(self.health_settle)
        if self._connected.is_set():
            report["note"] = f"socket still open after {int(self.health_settle * 1000)} ms"
            return True
        report["error"] = self._with_hint("connection closed by the host")
        return False

    def _disconnect_hint(self) -> str:
        """`` (peer closed the connection 11 ms after the last send, 0 bytes
        read since)`` — appended to errors so the timing of a hang-up travels
        with the failure instead of living only in the log."""
        d = getattr(self, "_last_disconnect", None) or {}
        if not d:
            return ""
        if d.get("ms_after_last_tx") is None:
            return " (peer closed the connection before anything was sent)"
        return (
            f" (peer closed the connection {d['ms_after_last_tx']} ms after the last send, "
            f"{d['bytes_read_since_tx']} bytes read since)"
        )

    def _with_hint(self, text: str) -> str:
        """``text`` plus the disconnect hint, unless it already carries one."""
        return text if "peer closed the connection" in text else text + self._disconnect_hint()

    # -- step execution ------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        try:
            msg = self.protocol.encode(action, payload)
            parsed = await self._exchange(msg)
            shaped = self.protocol.shape_response(parsed)
            reason = self.protocol.reject_reason(parsed)
            if reason:
                return StepResult(
                    success=False,
                    request_payload=msg.request or payload,
                    response_payload=shaped,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    raw_log=self._wire_log(msg, parsed, shaped),
                    error=reason,
                )
            return StepResult(
                success=True,
                request_payload=msg.request or payload,
                response_payload=shaped,
                duration_ms=int((time.monotonic() - start) * 1000),
                raw_log=self._wire_log(msg, parsed, shaped),
            )
        except TimeoutError:
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=self._with_hint(f"No response within {self.response_timeout}s."),
            )
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )

    def _wire_log(self, msg: EncodedMessage, parsed: dict, shaped: dict) -> str:
        """Human-readable wire trace: framed bytes sent + framed bytes received
        + parsed reply.

        Best-effort only — never raises into the execution path.
        """
        try:
            proto = self.protocol.name
            frame = self.frame_spec.frame(msg.body)
            hexed = frame.hex()
            if len(hexed) > 512:
                hexed = hexed[:512] + "…"
            sent_mti = (msg.request or {}).get("mti")
            head = f"[{proto}] → sent {len(frame)} bytes"
            if sent_mti:
                head += f" · MTI {sent_mti}"
            lines = [head, f"  TX {hexed}"]
            recv_mti = parsed.get("mti")
            rx = msg.reply
            rhead = f"[{proto}] ← recv"
            if rx:
                rhead += f" {len(rx)} bytes"
            if recv_mti:
                rhead += f" · MTI {recv_mti}"
            if shaped.get("response_code") is not None:
                rhead += f" · DE39={shaped['response_code']}"
            lines.append(rhead)
            if rx:
                rhex = rx.hex()
                if len(rhex) > 512:
                    rhex = rhex[:512] + "…"
                lines.append(f"  RX {rhex}")
            de_list = parsed.get("de_list") or shaped.get("de_list")
            if de_list:
                lines.append("  DE " + ", ".join(str(d) for d in de_list))
            return "\n".join(lines)
        except Exception:  # pragma: no cover - must not break a run  # noqa: BLE001
            return f"{self.protocol.name}: sent {msg.request}"

    # -- send / correlate ----------------------------------------------------

    async def _exchange(
        self, msg: EncodedMessage, _allow_wait: bool = True, note: str = ""
    ) -> dict:
        if not msg.key:
            raise ValueError("Protocol produced an empty correlation key.")
        # If the socket is down, wait for an in-progress reconnect to restore it
        # (bounded by the response timeout) before giving up.
        if _allow_wait and not self._connected.is_set():
            if not self.reconnect_enabled:
                raise ConnectionError(self._with_hint(f"{self.host}:{self.port} is not connected"))
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=self.response_timeout)
            except TimeoutError:
                raise ConnectionError(f"{self.host}:{self.port} did not reconnect in time")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg.key] = fut
        try:
            frame = self.frame_spec.frame(msg.body)
            async with self._send_lock:
                self._writer.write(frame)
                await self._writer.drain()
                mti = (msg.request or {}).get("mti")
                self.journal.record(
                    "tx", frame, " · ".join(x for x in (note, f"MTI {mti}" if mti else "") if x)
                )
            parsed = await asyncio.wait_for(fut, timeout=self.response_timeout)
            msg.reply = self._rx_frames.pop(msg.key, b"")
            return parsed
        finally:
            self._pending.pop(msg.key, None)
            self._rx_frames.pop(msg.key, None)

    def _probe(self, section: str) -> tuple[str, dict]:
        """Resolve (action, payload) for sign-on / keep-alive / health.

        ``health_check`` without a message of its own falls back to
        ``sign_on``'s (so one configured sign-on also serves as the probe);
        every section finally falls back to the protocol's default probe.
        """
        cfg = self.config.get(section) or {}
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
            if section == "health_check" and self._section_has_message(
                self.config.get("sign_on") or {}
            ):
                return self._probe("sign_on")
            return self.protocol.health_probe()
        return action, payload

    # -- framing -------------------------------------------------------------

    def _frame(self, body: bytes) -> bytes:
        return self.frame_spec.frame(body)

    # -- background reader ---------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            while not self._closing:
                frame = await self._read_frame()
                if frame is None:
                    break
                fixed, body = frame
                self._dispatch(self.protocol.decode(body), fixed + body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            self._on_disconnect(exc)
            return
        # clean EOF (peer closed) while we weren't shutting down
        if not self._closing:
            self._on_disconnect(ConnectionError("connection closed by peer"))

    async def _read_frame(self) -> tuple[bytes, bytes] | None:
        """One inbound frame as ``(prefix + fixed blocks, body)``; ``None`` on
        an empty frame."""
        prefix = await self._reader.readexactly(self.frame_spec.prefix_bytes)
        try:
            remaining = self.frame_spec.remaining_after_prefix(prefix)
        except ValueError as exc:
            self.journal.record("rx", prefix, f"unreadable length prefix: {exc}")
            raise
        if remaining <= 0:
            self.journal.record("rx", prefix, "empty frame")
            return None
        data = await self._reader.readexactly(remaining)
        fixed, body = self.frame_spec.strip_inbound(data)
        self.journal.record("rx", prefix + data)
        return prefix + fixed, body

    def _dispatch(self, parsed: dict, raw: bytes = b"") -> None:
        key = self.protocol.correlation_key(parsed)
        fut = self._pending.get(key) if key is not None else None
        if fut and not fut.done():
            if raw and key is not None:
                self._rx_frames[key] = raw
            fut.set_result(parsed)
        else:
            self._events.append(parsed)  # unsolicited / late / unmatched

    def _fail_pending(self, exc: Exception) -> None:
        err = ConnectionError(self._with_hint(f"Connection lost: {exc}"))
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _keepalive_loop(self, ka: dict) -> None:
        interval = float(ka.get("interval_sec", 30))
        action, payload = self._probe("keepalive")
        while not self._closing:
            await asyncio.sleep(interval)
            try:
                await self._exchange(self.protocol.encode(action, dict(payload)), note="keepalive")
            except Exception as exc:  # noqa: BLE001 - keepalive must survive any send failure
                log.warning("Keepalive failed for %s:%s: %s", self.host, self.port, exc)


#: Backwards-compatible alias — the adapter began life as an ISO 8583-only class.
TcpIso8583Adapter = TcpAdapter
