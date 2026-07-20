"""TcpResponder — a rules-driven host/acquirer/issuer simulator.

The flip side of :class:`~.adapter.TcpAdapter`: instead of *sending* messages and
asserting responses, the responder *listens* and answers — so PayProbe can stand
in for the switch, acquirer or HSM during loopback testing, with no real host.

It reuses the same framing config and protocol codecs as the adapter, so the two
interoperate over the same wire. Behaviour is data-driven: a list of **rules**
matches an incoming message (by MTI and/or field values) and dictates the reply
(set response fields, echo request fields, generate missing fields, decline, delay, or drop).
The first matching rule wins; otherwise a configurable default applies.

Config (JSON-friendly)::

    {
      "host": "127.0.0.1", "port": 7000,
      "protocol": "iso8583",            # or "header_echo"
      "framing": { ... },               # same keys as the adapter
      "fields": { "<de>": {...} },      # DE table (defaults to 1987)
      "rules": [
        {"when": {"mti": "0200", "de": {"4": {"gte": "000000100000"}}},
         "respond": {"set": {"39": "61"}}},          # over-limit -> 61
        {"when": {"de": {"2": {"prefix": "400000"}}},
         "respond": {"set": {"39": "05"}}},          # BIN block -> 05
        {"when": {"mti": "0200"},
         "respond": {"set": {"39": "00", "38": "AUTH99"}}}
      ],
      "default": {"echo": ["11"], "generate": {"37": "random_rrn"}, "set": {"39": "00"}}
    }

Chaos / fault injection (see :mod:`.chaos`) can be added to deliberately
misbehave — inject latency, drop replies, corrupt frames, or hang up
mid-message — for resilience testing beyond the happy path. A ``chaos`` block
at the top level applies to every reply; a ``chaos`` block inside a rule's
``respond`` overrides it for matching messages::

    {
      "chaos": {"seed": 1, "drop_pct": 5,
                "latency_ms": {"min": 50, "max": 300, "pct": 25}},
      "rules": [
        {"when": {"mti": "0200"},
         "respond": {"set": {"39": "00"},
                     "chaos": {"malformed_pct": 100, "malformed_mode": "garbage"}}}
      ]
    }
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from . import iso8583
from .chaos import ChaosEngine

log = logging.getLogger(__name__)


def _match_condition(value: str, cond: Any) -> bool:
    """Does a request field ``value`` satisfy a rule condition?"""
    if isinstance(cond, dict):
        if "eq" in cond and value != str(cond["eq"]):
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


def _next_mti(mti: str) -> str:
    """Request -> response MTI (0200->0210, 0800->0810, …)."""
    if len(mti) == 4 and mti[2].isdigit():
        return mti[0] + mti[1] + str(int(mti[2]) + 1) + mti[3]
    return mti


class TcpResponder:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.protocol = config.get("protocol", "iso8583")
        self.fields = config.get("fields") or iso8583.DEFAULT_FIELDS
        self.rules = config.get("rules", [])
        self.default = config.get("default", {"echo": ["11", "37"], "set": {"39": "00"}})

        # Dialect validation. A ``validate`` block turns the bound DE table + an
        # optional per-MTI presence matrix into an inbound conformance check::
        #
        #     "validate": {"mode": "warn"|"reject"|"off",
        #                  "on_error": {"39": "30"},
        #                  "presence": {"0200": {"mandatory": ["2","4","11"]}}}
        #
        # ``warn`` records violations on the decoded message (visible in the
        # trace) but still answers normally; ``reject`` short-circuits to a
        # format-error reply (DE 39 = 30 by default) before the rules run.
        vcfg = config.get("validate") or {}
        self.validate_mode = str(vcfg.get("mode", "off")).lower()
        self.validate_presence: dict = vcfg.get("presence") or {}
        self.validate_on_error: dict = vcfg.get("on_error") or {"39": "30"}

        f = config.get("framing", {})
        self.prefix_bytes = int(f.get("length_prefix_bytes", 2))
        self.byte_order = f.get("length_byte_order", "big")
        self.length_includes_prefix = bool(f.get("length_includes_prefix", False))
        self.length_includes_header = bool(f.get("length_includes_header", True))
        self.tpdu_bytes = int(f.get("tpdu_bytes", 0))
        self.tpdu_out = bytes.fromhex(f.get("tpdu_outbound_hex", "") or "")
        self.encoding = f.get("encoding", "ascii")

        # header_echo specifics
        he = config.get("header_echo", {})
        self.header_bytes = int(he.get("header_bytes", 4))
        self.resp_cmd_bytes = int(he.get("response_command_bytes", 2))

        # chaos / fault injection — see chaos.py. A global block applies to
        # every reply; a rule's respond.chaos overrides it for matched messages.
        self.global_chaos: dict = config.get("chaos") or {}
        self.chaos = ChaosEngine(self.global_chaos.get("seed"))
        self.faults = 0  # count of replies in which a fault was injected
        self.invalid = 0  # count of requests that failed dialect validation

        self.received: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        #: Live client connections. ``stop()`` closes these too — otherwise
        #: ``server.close()`` only stops *accepting*, the established sockets keep
        #: being served by their handlers, and a client (e.g. a group member)
        #: never notices the simulator went away or reconnects to its restart.
        self._conns: set[asyncio.StreamWriter] = set()
        #: Per-connection liveness metadata for the Live Sessions view, keyed by
        #: the same writer used in ``_conns``: remote peer address, connect time,
        #: message count and last-seen. Populated/torn down alongside ``_conns``.
        self._conn_meta: dict[asyncio.StreamWriter, dict] = {}

        # -- live metrics (for the simulator dashboard) ----------------------
        #: wall-clock start of the current stats window (reset by reset_stats).
        self.started_at = time.time()
        #: request volume by MTI (iso8583) or command (header_echo).
        self.by_mti: dict[str, int] = {}
        #: reply volume by response code (DE39 / header-echo error). Dropped
        #: replies are bucketed under "(drop)".
        self.by_rc: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> int:
        """Begin listening. Returns the bound port (useful when port=0)."""
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 0))
        self._server = await asyncio.start_server(self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    @property
    def port(self) -> int | None:
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Drop every live client connection so callers see the socket close and
        # reconnect to the next instance instead of lingering on a dead handler.
        for writer in list(self._conns):
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("responder: error closing client socket on stop: %s", exc)
        self._conns.clear()
        self._conn_meta.clear()

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # -- metrics -------------------------------------------------------------

    def _response_code(self, action: dict) -> str:
        """The response code this reply will carry — DE39 for ISO 8583, or the
        header-echo error field — used for the response-code breakdown."""
        s = action.get("set") or {}
        if self.protocol == "header_echo":
            return str(s.get("error", action.get("error", "00")))
        return str(s.get("39", "00"))

    def set_chaos(self, cfg: dict | None) -> dict:
        """Swap the live global chaos block at runtime — the fault dial.

        Takes effect on the very next reply (``_handle`` re-merges the global
        block per message), no restart needed. Passing an empty/None config
        turns chaos off. A ``seed`` in the new block restarts the RNG stream so
        a dialed-in fault sequence is reproducible from this point. The config
        dict is kept in sync so save-as-simulator persists what is actually
        running.
        """
        cfg = dict(cfg or {})
        if cfg.get("seed") is not None:
            self.chaos = ChaosEngine(cfg.get("seed"))
        self.global_chaos = cfg
        self.config["chaos"] = cfg
        return cfg

    def reset_stats(self) -> None:
        """Clear the live counters and traffic log (the "clear stats" action)."""
        self.received.clear()
        self.faults = 0
        self.invalid = 0
        self.by_mti.clear()
        self.by_rc.clear()
        self.started_at = time.time()

    def peers(self) -> list[dict]:
        """Live inbound client connections for the Live Sessions view.

        One row per established socket: the remote ``peer`` (``host:port``), when
        it connected, how many frames it has sent, and how long it has been idle.
        """
        now = time.time()
        rows: list[dict] = []
        for meta in self._conn_meta.values():
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

    def stats(self) -> dict:
        """Snapshot of cumulative traffic for the metrics dashboard."""
        elapsed = max(1e-6, time.time() - self.started_at)
        total = len(self.received)
        return {
            "received": total,
            "faults": self.faults,
            "invalid": self.invalid,
            "elapsed_s": round(elapsed, 1),
            "avg_rps": round(total / elapsed, 2),
            "by_mti": dict(self.by_mti),
            "by_response_code": dict(self.by_rc),
        }

    # -- connection handling -------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        peer = writer.get_extra_info("peername")
        peer_str = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        self._conn_meta[writer] = {
            "peer": peer_str,
            "since": time.time(),
            "msgs": 0,
            "last": time.time(),
        }
        try:
            while True:
                body = await self._read_frame(reader)
                if body is None:
                    break
                parsed = self._decode(body)
                self.received.append(parsed)
                meta = self._conn_meta.get(writer)
                if meta is not None:
                    meta["msgs"] += 1
                    meta["last"] = time.time()
                # metric: count this request by MTI (iso) / command (header_echo)
                mkey = parsed.get("mti") or parsed.get("command") or "?"
                self.by_mti[mkey] = self.by_mti.get(mkey, 0) + 1

                # Dialect validation (iso8583 only). In ``reject`` mode a
                # non-conformant request is answered with a format-error reply
                # instead of running the rules; in ``warn`` mode the violations
                # are recorded on the message and normal resolution continues.
                violations = self._validate(parsed)
                if violations:
                    self.invalid += 1
                    parsed["validation"] = violations
                if violations and self.validate_mode == "reject":
                    action = self._format_error_action(parsed)
                else:
                    action = await self._resolve_async(parsed)

                # roll chaos: merge global block with this rule's override
                merged = {**self.global_chaos, **(action.get("chaos") or {})}
                outcome = self.chaos.plan(merged)
                if outcome.active:
                    self.faults += 1
                    parsed["chaos"] = outcome.summary()

                # drop: legacy respond.drop or a chaos drop -> no reply (timeout)
                if action.get("drop") or outcome.drop:
                    self.by_rc["(drop)"] = self.by_rc.get("(drop)", 0) + 1
                    continue

                # metric: count the reply by its response code
                rc = self._response_code(action)
                self.by_rc[rc] = self.by_rc.get(rc, 0) + 1

                # latency: legacy delay_ms plus any chaos latency/jitter
                delay = action.get("delay_ms", 0) + outcome.latency_ms
                if delay:
                    await asyncio.sleep(delay / 1000)

                resp = self._encode(parsed, action)
                frame = self._frame(resp)

                if outcome.malformed:
                    frame = self.chaos.corrupt(
                        frame,
                        outcome.malformed,
                        prefix_bytes=self.prefix_bytes,
                        byte_order=self.byte_order,
                    )

                if outcome.partial:
                    # send only part of the frame, then hang up mid-message so
                    # the client sees a truncated / partial read.
                    n = self.chaos.partial_len(frame, outcome.partial_bytes)
                    writer.write(frame[:n])
                    await writer.drain()
                    break

                writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Responder connection error: %s", exc)
        finally:
            self._conns.discard(writer)
            self._conn_meta.pop(writer, None)
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("responder: error closing client socket: %s", exc)

    # -- framing (mirrors TcpAdapter) ---------------------------------------

    def _frame(self, body: bytes) -> bytes:
        payload = self.tpdu_out + body
        counted = len(payload) if self.length_includes_header else len(body)
        length = counted + (self.prefix_bytes if self.length_includes_prefix else 0)
        return length.to_bytes(self.prefix_bytes, self.byte_order) + payload

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes | None:
        try:
            prefix = await reader.readexactly(self.prefix_bytes)
        except asyncio.IncompleteReadError:
            return None
        length = int.from_bytes(prefix, self.byte_order)
        core = length - (self.prefix_bytes if self.length_includes_prefix else 0)
        remaining = core if self.length_includes_header else core + self.tpdu_bytes
        if remaining <= 0:
            return b""
        data = await reader.readexactly(remaining)
        return data[self.tpdu_bytes :]

    # -- protocol decode / encode -------------------------------------------

    def _decode(self, body: bytes) -> dict:
        text = body.decode(self.encoding, errors="replace")
        if self.protocol == "header_echo":
            hb, rcb = self.header_bytes, self.resp_cmd_bytes
            return {
                "header": text[:hb],
                "command": text[hb : hb + rcb],
                "data": text[hb + rcb :],
                "raw": text,
            }
        parsed = iso8583.iso_unpack(text, self.fields)
        return {
            "mti": parsed["mti"],
            "de": {k: v.get("value") for k, v in parsed["fields"].items()},
            #: every DE bit set in the bitmap (a superset of the keys in ``de``
            #: when decoding stopped early on an unknown field) — used by
            #: dialect validation to flag DEs the format does not define.
            "de_list": parsed["de_list"],
        }

    def _encode(self, parsed: dict, action: dict) -> bytes:
        if self.protocol == "header_echo":
            header = parsed.get("header", "")
            # An action carrying a full raw response text (the shape of an HSM
            # client's answer, echoed via ``${<relay_id>.response}``) is replayed
            # verbatim under the CLIENT's header — response command + error code
            # + data byte-exact, correlation intact. Explicit command/set keys
            # take the field-wise path below instead.
            raw = action.get("raw")
            if (
                isinstance(raw, str)
                and len(raw) > self.header_bytes
                and "command" not in action
                and "set" not in action
            ):
                return f"{header}{raw[self.header_bytes:]}".encode(self.encoding)
            command = action.get("command", parsed.get("command", ""))
            # ``error_code`` is the shaped-response key — accepted so a reply of
            # ``${<relay_id>.response}`` echoes an upstream answer directly.
            error = action.get("set", {}).get(
                "error", action.get("error", action.get("error_code", "00"))
            )
            data = action.get("data", parsed.get("data", ""))
            return f"{header}{command}{error}{data}".encode(self.encoding)
        # iso8583
        req_de = parsed.get("de", {})
        values: dict[str, str] = {}
        for de in action.get("echo", []):
            if str(de) in req_de and req_de[str(de)] is not None:
                values[str(de)] = req_de[str(de)]
        # A full DE map (``fields``/``values``) is taken wholesale — this is the
        # shape of an adapter's shaped response, so a reply node whose payload is
        # ``${<relay_id>.response}`` returns the upstream's answer as-is.
        # Explicit ``set`` entries below still override individual DEs.
        full = action.get("fields") or action.get("values")
        if isinstance(full, dict):
            for de, val in full.items():
                if val is not None:
                    values[str(de)] = str(val)
        for de, val in (action.get("set") or {}).items():
            values[str(de)] = str(val)
        # generate: create field if not present (e.g., RRN if missing)
        for de, gen_spec in (action.get("generate") or {}).items():
            if str(de) not in values or not values[str(de)]:
                if gen_spec == "random_rrn":
                    values[str(de)] = f"{random.randint(100000, 999999):06d}"
                elif isinstance(gen_spec, str):
                    values[str(de)] = gen_spec
        values.setdefault("39", "00")
        mti = action.get("mti") or _next_mti(parsed.get("mti", "0200"))
        return iso8583.iso_pack(mti, values, self.fields).encode(self.encoding)

    # -- rule resolution -----------------------------------------------------

    async def _resolve_async(self, parsed: dict) -> dict:
        """Async resolution hook used by ``_handle``. The base responder is
        rules-driven and synchronous; subclasses (e.g. the participant-flow
        responder) override this to compute the reply action asynchronously."""
        return self._resolve(parsed)

    # -- dialect validation --------------------------------------------------

    def _validate(self, parsed: dict) -> list[str]:
        """Check an inbound iso8583 message against the bound dialect. Returns
        the list of violations (empty when off, non-iso, or conformant)."""
        if self.validate_mode == "off" or "de" not in parsed:
            return []
        return iso8583.iso_validate(
            parsed.get("mti", ""),
            self.fields,
            parsed.get("de", {}),
            parsed.get("de_list"),
            self.validate_presence,
        )

    def _format_error_action(self, parsed: dict) -> dict:
        """Reply used when a request fails validation in ``reject`` mode — a
        format-error response code (DE 39 = 30 by default) echoing the STAN."""
        return {"echo": ["11", "37"], "set": dict(self.validate_on_error)}

    def _resolve(self, parsed: dict) -> dict:
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})
        return dict(self.default)

    def _matches(self, when: dict, parsed: dict) -> bool:
        if self.protocol == "header_echo":
            if "command" in when and not _match_condition(
                parsed.get("command", ""), when["command"]
            ):
                return False
            return True
        if "mti" in when and parsed.get("mti") != when["mti"]:
            return False
        req_de = parsed.get("de", {})
        for de, cond in (when.get("de") or {}).items():
            if not _match_condition(str(req_de.get(str(de), "")), cond):
                return False
        return True
