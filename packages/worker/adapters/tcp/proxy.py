"""TcpProxy — a transparent man-in-the-middle for a live TCP payment link.

The third posture alongside :class:`~.adapter.TcpAdapter` (which *originates*
traffic) and :class:`~.responder.TcpResponder` (which *terminates* it): the proxy
sits **between** a real client and a real upstream host, relaying frames both
ways while observing what flows through.

All three ADR-0002 modes are implemented. For each accepted client connection
the proxy opens a paired socket to ``upstream`` and runs two relay pumps
(client→upstream and upstream→client):

* ``tap`` (default) — byte-exact pass-through. Each frame is *decoded for
  visibility* (feeding the same ``received`` / ``by_mti`` counters the
  responder exposes) but **forwarded unchanged** — the decode never reshapes
  the bytes on the wire, and a frame that fails to decode is still relayed.
* ``intercept`` — same relay, but frames matching a rule are mutated /
  injected / delayed / dropped / corrupted in flight (responder rule + chaos
  vocabulary, either direction).
* ``stub`` — requests matching a rule are answered locally (no upstream hop);
  everything else forwards verbatim.

Every relayed frame lands in the bounded, **redacted** :class:`CaptureBuffer`
(``capture.py`` — PAN masking, logging rules, optional raw hex), readable over
``GET /simulators/{sid}/capture`` and convertible via
``POST .../capture/save-as-scenario``.

It subclasses :class:`TcpResponder` purely to reuse its framing
(``_read_frame`` / ``_frame``), protocol decode (``_decode``), live-session
bookkeeping (``_conn_meta`` / ``peers``), lifecycle and metrics. Only connection
handling is overridden: instead of resolving a reply locally it relays.

Config (extends the responder config)::

    {
      "kind": "proxy",                 # selects this class (see _responder_for)
      "protocol": "iso8583",           # wire protocol, used for decode/visibility
      "host": "0.0.0.0", "port": 7000, # listen side (reuses responder keys)
      "framing": { ... },              # same keys as the adapter/responder
      "upstream": {                    # the real host to relay to
        "host": "10.0.1.50", "port": 7000,
        "connect_timeout_sec": 10
      },
      "mode": "tap",                   # tap | intercept | stub
      "rules": [ ... ],                # intercept/stub matching (responder vocab)
      "capture": { "max_frames": ..., "store_raw": ..., "redact": ...,
                    "rules": [...], "mode": "all|rules" }
    }

Assumes **symmetric framing** — request and response share one framing config —
which holds for the ISO 8583 / header-echo links PayProbe targets. Capture is
a bounded in-memory ring (chosen over the ADR's file-backed store — save-as-
scenario is the durable path). Only **TLS** remains deferred
(see docs/adr/0002-tcp-proxy-tap-forwarding-mode.md).
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import iso8583
from .capture import CaptureBuffer, build_scenario_draft
from .responder import TcpResponder

log = logging.getLogger(__name__)


class TcpProxy(TcpResponder):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Upstream(s): a single ``upstream`` (back-compat) or a fleet of
        # ``upstreams`` selected per inbound connection (so a relay can forward to
        # a participant group / HSM fleet, balancing connections across members).
        up = config.get("upstream") or {}
        ups = config.get("upstreams") or ([up] if up.get("host") else [])
        self._upstreams: list[tuple[str, int]] = [
            (u["host"], int(u.get("port", 0))) for u in ups if u.get("host")
        ]
        #: round-robin cursor across ``_upstreams``.
        self._up_rr = 0
        self._up_policy = str((config.get("selection") or {}).get("policy", "round_robin")).lower()
        # Back-compat scalars (first member) for callers/tests that read them.
        self.up_host = self._upstreams[0][0] if self._upstreams else up.get("host")
        self.up_port = self._upstreams[0][1] if self._upstreams else int(up.get("port", 0))
        self.up_connect_timeout = float(up.get("connect_timeout_sec", 10))
        self.mode = str(config.get("mode", "tap")).lower()
        #: Decode each relayed frame for visibility/capture (the default). Set
        #: ``decode: false`` for a pure byte-exact passthrough that doesn't try to
        #: parse the wire at all — useful when the traffic is a protocol the
        #: responder can't model, or when you just want a blind relay. The bytes
        #: are forwarded identically either way; this only governs inspection.
        self.decode_frames = bool(config.get("decode", True))
        #: Bounded, redacted recording of relayed frames (ADR-0002 stage 2).
        cap = config.get("capture") or {}
        self.capture = CaptureBuffer(
            enabled=bool(cap.get("enabled", True)),
            max_frames=int(cap.get("max_frames", 5000)),
            store_raw=bool(cap.get("store_raw", False)),
            redact=bool(cap.get("redact", True)),
            rules=cap.get("rules") or [],
            mode=str(cap.get("mode", "all")),
        )
        #: Correlated Network-Trace records, one per relayed transaction (decode
        #: mode only). The orchestrator's /participants/traces index + drilldown
        #: pick these up via ``traces()`` — so a relay proxy shows up in Network
        #: Trace alongside flow listeners, fed by what it forwards.
        self._relay_traces: list[dict] = []
        self._open_relay: dict[str, dict] = {}  # cid -> in-flight request record
        # Rolling window of relayed transactions. Generous by default so a click
        # in Network Trace still finds a recently-listed transaction under load —
        # the payShield correlation header cycles 0001..9999, so we keep at least
        # a full cycle's worth. Override via ``trace_buffer``.
        self._relay_trace_max = int(config.get("trace_buffer", 10000))
        #: Live upstream sockets (one per inbound connection) — closed on stop().
        self._up_conns: set[asyncio.StreamWriter] = set()
        #: Per-upstream liveness metadata for the /peers *outbound* leg, keyed by
        #: the upstream writer. Mirrors the parent's inbound ``_conn_meta``.
        self._upstream_meta: dict[asyncio.StreamWriter, dict] = {}

    # -- lifecycle -----------------------------------------------------------

    async def stop(self) -> None:
        # Tear down the upstream legs first so both sides of every relayed
        # connection drop, then let the parent stop the listener + inbound socks.
        for writer in list(self._up_conns):
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("proxy: error closing upstream socket on stop: %s", exc)
        self._up_conns.clear()
        self._upstream_meta.clear()
        await super().stop()

    # -- live sessions -------------------------------------------------------

    def upstream_peers(self) -> list[dict]:
        """Outbound upstream sockets for the Live Sessions view — same row shape
        as the inherited (inbound) :meth:`peers`."""
        now = time.time()
        rows: list[dict] = []
        for meta in self._upstream_meta.values():
            since = meta.get("since", now)
            last = meta.get("last", since)
            rows.append(
                {
                    "peer": meta.get("peer") or "?",
                    "since": round(since, 3),
                    "connected_s": round(now - since, 1),
                    "messages": meta.get("msgs", 0),
                    "idle_s": round(now - last, 1),
                }
            )
        rows.sort(key=lambda r: r["since"])
        return rows

    def _pick_upstream(self) -> tuple[str | None, int]:
        """Choose the upstream for one inbound connection. A single upstream is
        always returned as-is; a fleet is balanced by ``selection.policy``
        (round_robin default, or random)."""
        ups = self._upstreams
        if len(ups) <= 1:
            return (self.up_host, self.up_port)
        if self._up_policy == "random":
            import random

            return random.choice(ups)
        host = ups[self._up_rr % len(ups)]
        self._up_rr += 1
        return host

    # -- connection handling -------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # inbound bookkeeping (mirror TcpResponder._handle)
        self._conns.add(writer)
        peer = writer.get_extra_info("peername")
        peer_str = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        in_meta = {"peer": peer_str, "since": time.time(), "msgs": 0, "last": time.time()}
        self._conn_meta[writer] = in_meta

        up_writer: asyncio.StreamWriter | None = None
        try:
            up_host, up_port = self._pick_upstream()
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(up_host, up_port),
                timeout=self.up_connect_timeout,
            )
            self._up_conns.add(up_writer)
            up_peer = up_writer.get_extra_info("peername")
            up_peer_str = (
                f"{up_peer[0]}:{up_peer[1]}" if isinstance(up_peer, tuple) else str(up_peer)
            )
            up_meta = {"peer": up_peer_str, "since": time.time(), "msgs": 0, "last": time.time()}
            self._upstream_meta[up_writer] = up_meta

            # Two relay pumps. When either direction ends (EOF or error) we cancel
            # the other so a half-open relay never lingers.
            c2u = asyncio.create_task(
                self._pump(reader, up_writer, "c2u", in_meta, back_writer=writer)
            )
            u2c = asyncio.create_task(
                self._pump(up_reader, writer, "u2c", up_meta, back_writer=None)
            )
            _done, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except (asyncio.TimeoutError, OSError) as exc:
            # Upstream unreachable / refused / reset: drop the client too.
            log.warning("proxy: upstream connect/relay failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("proxy connection error: %s", exc)
        finally:
            self._conns.discard(writer)
            self._conn_meta.pop(writer, None)
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("proxy: error closing client socket: %s", exc)
            if up_writer is not None:
                self._up_conns.discard(up_writer)
                self._upstream_meta.pop(up_writer, None)
                try:
                    up_writer.close()
                except Exception as exc:  # noqa: BLE001 — best-effort teardown
                    log.debug("proxy: error closing upstream socket: %s", exc)

    async def _pump(
        self,
        src_reader: asyncio.StreamReader,
        dst_writer: asyncio.StreamWriter,
        direction: str,
        meta: dict,
        *,
        back_writer: asyncio.StreamWriter | None,
    ) -> None:
        """Relay whole frames from ``src_reader`` to ``dst_writer``, decoding each
        for visibility/capture. ``direction`` is ``c2u`` (client→upstream) or
        ``u2c`` (upstream→client). Behaviour depends on ``mode``:

        * ``tap`` — forward byte-for-byte (the default).
        * ``intercept`` — forward, but mutate/inject/delay/drop/corrupt frames
          that match a rule (``rules``), in either direction.
        * ``stub`` — answer matched *requests* (``c2u``) locally on
          ``back_writer`` without hitting upstream; forward the rest. Responses
          (``u2c``) are relayed unchanged.
        """
        while True:
            body = await self._read_frame(src_reader)
            if body is None:
                break
            parsed = self._observe(body, direction)
            self.capture.add(direction, parsed, raw=body)
            meta["msgs"] = meta.get("msgs", 0) + 1
            meta["last"] = time.time()

            if self.mode == "intercept":
                await self._intercept(body, parsed, dst_writer)
            elif (
                self.mode == "stub"
                and direction == "c2u"
                and back_writer is not None
                and await self._stub_request(body, parsed, back_writer)
            ):
                continue  # answered locally — do not forward upstream
            else:
                # tap, or stub-unmatched, or stub response leg: forward verbatim.
                # _frame(_read_frame(x)) == x, so this is byte-exact.
                dst_writer.write(self._frame(body))
                await dst_writer.drain()

    # -- intercept / stub helpers -------------------------------------------

    def _match_action(self, parsed: dict) -> dict | None:
        """The ``respond`` action of the first rule whose ``when`` matches, or
        None when nothing matches (undecoded frames never match)."""
        if parsed.get("undecoded"):
            return None
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})
        return None

    def _chaos_for(self, action: dict):
        """Roll the chaos plan for an action (global block merged with the
        rule's override), bumping the fault counter when something fires."""
        outcome = self.chaos.plan({**self.global_chaos, **(action.get("chaos") or {})})
        if outcome.active:
            self.faults += 1
        return outcome

    def _mutate(self, parsed: dict, action: dict) -> bytes:
        """Overlay an action's ``set`` onto a decoded frame's DEs, keeping its
        MTI (unless the action overrides it), and re-encode. iso8583 only;
        callers forward verbatim for other protocols."""
        mti = str(action.get("mti") or parsed.get("mti") or "")
        values = {str(k): str(v) for k, v in (parsed.get("de") or {}).items() if v is not None}
        for de, val in (action.get("set") or {}).items():
            values[str(de)] = str(val)
        return iso8583.iso_pack(mti, values, self.fields).encode(self.encoding)

    async def _intercept(self, body: bytes, parsed: dict, dst_writer: asyncio.StreamWriter) -> None:
        """Forward a frame, mutating / delaying / dropping / corrupting it when a
        rule matches. Unmatched frames pass through byte-for-byte."""
        action = self._match_action(parsed)
        if action is None:
            dst_writer.write(self._frame(body))
            await dst_writer.drain()
            return
        outcome = self._chaos_for(action)
        if action.get("drop") or outcome.drop:
            return  # swallow the frame entirely
        delay = action.get("delay_ms", 0) + outcome.latency_ms
        if delay:
            await asyncio.sleep(delay / 1000)
        if self.protocol == "iso8583" and (action.get("set") or action.get("mti")):
            out_body = self._mutate(parsed, action)
        else:
            out_body = body
        frame = self._frame(out_body)
        if outcome.malformed:
            frame = self.chaos.corrupt(
                frame,
                outcome.malformed,
                prefix_bytes=self.prefix_bytes,
                byte_order=self.byte_order,
            )
        dst_writer.write(frame)
        await dst_writer.drain()

    async def _stub_request(
        self, body: bytes, parsed: dict, client_writer: asyncio.StreamWriter
    ) -> bool:
        """If a request matches a rule, answer it locally (like the responder)
        and return True so the caller skips the upstream hop. Return False to let
        the caller forward the request to the real host."""
        action = self._match_action(parsed)
        if action is None or self.protocol != "iso8583" or "de" not in parsed:
            return False
        outcome = self._chaos_for(action)
        if action.get("drop") or outcome.drop:
            return True  # matched but dropped → no reply (simulates a timeout)
        delay = action.get("delay_ms", 0) + outcome.latency_ms
        if delay:
            await asyncio.sleep(delay / 1000)
        resp_body = self._encode(parsed, action)
        # record the synthesised reply on the response leg for visibility
        resp_parsed = self._observe(resp_body, "u2c")
        self.capture.add("u2c", resp_parsed, raw=resp_body)
        frame = self._frame(resp_body)
        if outcome.malformed:
            frame = self.chaos.corrupt(
                frame,
                outcome.malformed,
                prefix_bytes=self.prefix_bytes,
                byte_order=self.byte_order,
            )
        client_writer.write(frame)
        await client_writer.drain()
        return True

    def _observe(self, body: bytes, direction: str) -> dict:
        """Decode a relayed frame for the live log/metrics and return the parsed
        view (for the capture buffer). Failure-tolerant: tap forwards bytes it
        cannot parse, so a decode error never stops the relay — the frame is
        logged as undecoded. With ``decode: false`` the frame is never parsed
        (raw passthrough) — it is counted but recorded only as opaque bytes."""
        if not self.decode_frames:
            parsed = {"raw": True, "bytes": len(body), "direction": direction}
            self.received.append(parsed)
            self.by_mti["(raw)"] = self.by_mti.get("(raw)", 0) + 1
            return parsed
        try:
            parsed = self._decode(body)
        except Exception:  # noqa: BLE001 — never gate the wire on parseability
            parsed = {"undecoded": True, "bytes": len(body)}
        parsed["direction"] = direction
        self.received.append(parsed)
        mkey = parsed.get("mti") or parsed.get("command") or "?"
        self.by_mti[mkey] = self.by_mti.get(mkey, 0) + 1
        self._record_relay_trace(parsed, direction)
        return parsed

    # -- Network Trace records (decode mode) ---------------------------------

    def _relay_trace_kind(self) -> str:
        """Adapter tag for the trace, from the wire protocol."""
        if self.protocol == "header_echo":
            return "hsm"
        if self.protocol == "http":
            return "http"
        return "tcp"

    def _relay_cid(self, parsed: dict) -> str | None:
        """Correlation id shared by a request and its reply. header_echo hosts
        echo the leading header; ISO 8583 echoes DE 11."""
        if self.protocol == "header_echo":
            return parsed.get("header") or None
        de = parsed.get("de") or {}
        return de.get("11") or de.get(11) or None

    def _record_relay_trace(self, parsed: dict, direction: str) -> None:
        """Pair a relayed request (c2u) with its reply (u2c) into one correlated
        Network-Trace record. Undecoded/raw frames carry no correlation id, so
        they're skipped (use decode mode to see commands in Network Trace)."""
        # NB: header_echo decode puts the raw frame TEXT under "raw", so only the
        # no-decode passthrough marker (boolean ``raw is True``) means "skip".
        # Capture gate — when paused (the Network Trace "stopped" state), frames
        # are still relayed but NOT recorded. Default-stopped is set at launch.
        if not getattr(self, "_capture", True):
            return
        if parsed.get("undecoded") or parsed.get("raw") is True:
            return
        cid = self._relay_cid(parsed)
        if not cid:
            return
        if direction == "c2u":  # request
            rec = {
                "correlation_id": cid,
                "flow_id": getattr(self, "_flow_id", None),
                "flow_name": getattr(self, "_flow_name", None),
                "kind": self._relay_trace_kind(),
                "port": self.port,
                "ts": time.time(),
                "mti": parsed.get("mti") or parsed.get("command"),
                "request": {
                    "mti": parsed.get("mti") or parsed.get("command"),
                    "de": parsed.get("de") or {},
                },
                "response_code": None,
                "status": "ok",
                "error": None,
                "ok": True,
                "calls": [],
                "relayed": True,
            }
            self._open_relay[cid] = rec
            self._relay_traces.append(rec)
            overflow = len(self._relay_traces) - self._relay_trace_max
            if overflow > 0:
                del self._relay_traces[:overflow]
        else:  # u2c — the reply: complete the matching request record
            rec = self._open_relay.pop(cid, None)
            if rec is None:
                return
            de = parsed.get("de") or {}
            # ISO replies carry the code in DE 39; header-echo (payShield) replies
            # carry a 2-char error code at the start of ``data`` after the response
            # command. Fall back to the response command code.
            rc = (
                de.get("39")
                or de.get(39)
                or parsed.get("error")
                or (parsed.get("data") or "")[:2]
                or parsed.get("command")
            )
            rec["response_code"] = rc
            rec["ts"] = time.time()

    def traces(self, correlation_id: str | None = None) -> list[dict]:
        """Relayed-transaction trace records (for the Network Trace index)."""
        if correlation_id is None:
            return list(self._relay_traces)
        return [t for t in self._relay_traces if t.get("correlation_id") == correlation_id]

    # -- capture API (used by the orchestrator capture endpoints) ------------

    def capture_rows(self) -> list[dict]:
        return self.capture.rows()

    def clear_capture(self) -> None:
        self.capture.clear()

    def capture_scenario_draft(self, name: str) -> dict:
        """A ScenarioDraft-shaped dict built from the current capture."""
        target = "tcp_iso8583" if self.protocol == "iso8583" else self.protocol
        return build_scenario_draft(self.capture.rows(), name, target=target)
