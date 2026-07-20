"""HSMAdapter — a payShield 10K host-command *client*.

The sending counterpart to :class:`~worker.adapters.hsm.payshield.PayShieldSimulator`.
A scenario step names a high-level action (``generate_cvv``, ``verify_pin``,
``generate_mac`` …) and supplies parameters; the adapter assembles the payShield
command string, frames it (length prefix + header + command + data), sends it,
and parses the reply into a structured payload — so flows can drive a real
payShield *or* the bundled simulator without hand-building wire strings.

**Pipelined correlation.** Each command is tagged with an incrementing
**header** (the payShield message header — a host-chosen correlation id the HSM
echoes back verbatim). A single background reader loop demultiplexes replies to
the right caller by that header, so many commands can be *in flight at once*
over one reused connection without waiting for each reply in turn. Sequential
``await``s behave exactly as before; concurrent ``execute`` calls now overlap.

**Connection pool.** Set ``connections`` (alias ``pool_size``) > 1 to open
several sockets; commands round-robin across them — useful for load/throughput,
where each socket also pipelines.

Config::

    {
      "host": "127.0.0.1", "port": 1500,
      "header_bytes": 4,           # width of the correlation header (default 4)
      "connections": 1,            # sockets to open (round-robin); >1 for load
      "length_prefix_bytes": 2,    # framing (matches the simulator defaults)
      "length_byte_order": "big",
      "encoding": "ascii",
      "response_timeout_sec": 5
    }

``response_payload`` carries ``response_code``, ``error_code``, ``ok`` (error is
``00``), the raw ``data`` tail, and any action-specific parsed field (e.g.
``cvv``). Point assertions at ``error_code`` / ``ok`` / ``cvv`` etc.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..base.base_adapter import BaseAdapter, StepResult

log = logging.getLogger(__name__)


def _kv(payload: dict, *names: str, default: str = "") -> str:
    for n in names:
        if payload.get(n) not in (None, ""):
            return str(payload[n])
    return default


# Each builder returns ``(command_code, data_string)`` for a named action,
# using the same wire layout the simulator parses.
def _b_nc(p: dict) -> tuple[str, str]:
    return "NC", _kv(p, "lmk_type")


def _b_no(p: dict) -> tuple[str, str]:
    return "NO", _kv(p, "mode", default="00")


def _b_generate_key(p: dict) -> tuple[str, str]:
    mode = _kv(p, "mode", default="0")
    keytype = _kv(p, "key_type", default="002")
    scheme = _kv(p, "scheme", default="U")
    return "A0", f"{mode}{keytype}{scheme}"


def _b_key_check(p: dict) -> tuple[str, str]:
    # 2-digit key type code + length flag (1) + key token
    code2 = _kv(p, "key_type_code", default="00")
    length_flag = _kv(p, "length_flag", default="1")
    return "BU", f"{code2}{length_flag}{_kv(p, 'key')}"


def _b_generate_cvv(p: dict) -> tuple[str, str]:
    return "CW", (
        f"{_kv(p, 'cvk')}{_kv(p, 'pan')};{_kv(p, 'expiry')}{_kv(p, 'service_code', default='000')}"
    )


def _b_verify_cvv(p: dict) -> tuple[str, str]:
    return "CY", (
        f"{_kv(p, 'cvk')}{_kv(p, 'cvv')}{_kv(p, 'pan')};"
        f"{_kv(p, 'expiry')}{_kv(p, 'service_code', default='000')}"
    )


def _b_translate_pin_tpk_zpk(p: dict) -> tuple[str, str]:
    return "CA", (
        f"{_kv(p, 'src_key', 'tpk')}{_kv(p, 'dst_key', 'zpk')}"
        f"{_kv(p, 'max_pin_len', default='12')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'src_format', default='01')}{_kv(p, 'dst_format', default='01')}"
        f"{_kv(p, 'pan')}"
    )


def _b_translate_pin_zpk_zpk(p: dict) -> tuple[str, str]:
    return "CC", (
        f"{_kv(p, 'src_key', 'src_zpk')}{_kv(p, 'dst_key', 'dst_zpk')}"
        f"{_kv(p, 'max_pin_len', default='12')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'src_format', default='01')}{_kv(p, 'dst_format', default='01')}"
        f"{_kv(p, 'pan')}"
    )


def _b_verify_pin_pvv(p: dict) -> tuple[str, str]:
    return "EC", (
        f"{_kv(p, 'zpk')}{_kv(p, 'pvk')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'format', default='01')}{_kv(p, 'pan')}{_kv(p, 'pvki', default='1')}"
        f"{_kv(p, 'pvv')}"
    )


def _b_generate_pin_offset(p: dict) -> tuple[str, str]:
    return "DE", (
        f"{_kv(p, 'tpk')}{_kv(p, 'pvk')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'format', default='01')}{_kv(p, 'pan')}"
        f"{_kv(p, 'dec_table', default='0123456789012345')}{_kv(p, 'validation_data')}"
    )


def _b_verify_pin_ibm(p: dict) -> tuple[str, str]:
    return "BA", (
        f"{_kv(p, 'tpk', 'pin_key')}{_kv(p, 'pvk')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'format', default='01')}{_kv(p, 'pan')}"
        f"{_kv(p, 'dec_table', default='0123456789012345')}{_kv(p, 'validation_data')}"
        f"{_kv(p, 'offset')}"
    )


def _b_verify_pin_ibm_interchange(p: dict) -> tuple[str, str]:
    return "BE", (
        f"{_kv(p, 'zpk', 'pin_key')}{_kv(p, 'pvk')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'format', default='01')}{_kv(p, 'pan')}"
        f"{_kv(p, 'dec_table', default='0123456789012345')}{_kv(p, 'validation_data')}"
        f"{_kv(p, 'offset')}"
    )


def _b_generate_pvv(p: dict) -> tuple[str, str]:
    return "JA", (
        f"{_kv(p, 'zpk')}{_kv(p, 'pvk')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'format', default='01')}{_kv(p, 'pan')}{_kv(p, 'pvki', default='1')}"
    )


def _b_dukpt_translate_pin(p: dict) -> tuple[str, str]:
    return "G0", (
        f"{_kv(p, 'bdk')}{_kv(p, 'zpk')}{_kv(p, 'ksn')}{_kv(p, 'pin_block')}"
        f"{_kv(p, 'src_format', default='01')}{_kv(p, 'dst_format', default='01')}"
        f"{_kv(p, 'pan')}"
    )


def _b_generate_mac(p: dict) -> tuple[str, str]:
    return "M6", f"{_kv(p, 'key')}{_kv(p, 'message', 'data')}"


def _b_verify_mac(p: dict) -> tuple[str, str]:
    return "M8", f"{_kv(p, 'key')}{_kv(p, 'message', 'data')}{_kv(p, 'mac')}"


def _b_import_key(p: dict) -> tuple[str, str]:
    return "A6", (
        f"{_kv(p, 'key_type', default='001')}{_kv(p, 'zmk')}{_kv(p, 'key')}"
        f"{_kv(p, 'scheme', default='U')}"
    )


def _b_export_key(p: dict) -> tuple[str, str]:
    return "K8", (
        f"{_kv(p, 'key_type', default='001')}{_kv(p, 'key')}{_kv(p, 'kek')}"
        f"{_kv(p, 'scheme', default='X')}"
    )


def _b_verify_arqc(p: dict) -> tuple[str, str]:
    txn = _kv(p, "txn_data")
    txnlen = f"{len(txn) // 2:02X}"
    mode = _kv(p, "mode", default="0")
    data = (
        f"{mode}{_kv(p, 'scheme', default='0')}{_kv(p, 'mk_ac')}"
        f"{_kv(p, 'pan')}{_kv(p, 'psn', default='00')}{_kv(p, 'atc')}"
        f"{txnlen}{txn};{_kv(p, 'arqc')}{_kv(p, 'arc')}"
    )
    return "KQ", data


_BUILDERS = {
    "nc": _b_nc,
    "diagnostics": _b_nc,
    "hsm_status": _b_no,
    "generate_key": _b_generate_key,
    "key_check": _b_key_check,
    "generate_cvv": _b_generate_cvv,
    "verify_cvv": _b_verify_cvv,
    "translate_pin": _b_translate_pin_tpk_zpk,
    "translate_pin_tpk_zpk": _b_translate_pin_tpk_zpk,
    "translate_pin_zpk_zpk": _b_translate_pin_zpk_zpk,
    "verify_pin": _b_verify_pin_pvv,
    "verify_pin_pvv": _b_verify_pin_pvv,
    "generate_pin_offset": _b_generate_pin_offset,
    "verify_pin_ibm": _b_verify_pin_ibm,
    "verify_pin_ibm_interchange": _b_verify_pin_ibm_interchange,
    "generate_pvv": _b_generate_pvv,
    "dukpt_translate_pin": _b_dukpt_translate_pin,
    "generate_mac": _b_generate_mac,
    "verify_mac": _b_verify_mac,
    "import_key": _b_import_key,
    "export_key": _b_export_key,
    "verify_arqc": _b_verify_arqc,
}


class _HsmConnection:
    """One pipelined TCP connection to an HSM.

    Each :meth:`send` tags its command with a fresh incrementing header and
    registers a future under that header; a single :meth:`_read_loop` reads
    replies and resolves the matching future — so many commands can be in flight
    at once and replies are demultiplexed by their echoed correlation header.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        header_bytes: int,
        prefix_bytes: int,
        byte_order: str,
        encoding: str,
        timeout: float,
    ) -> None:
        self.host, self.port = host, port
        self.header_bytes = header_bytes
        self.prefix_bytes = prefix_bytes
        self.byte_order = byte_order
        self.encoding = encoding
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._modulo = 10**header_bytes
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    def _next_header(self) -> str:
        """An incrementing, currently-unused correlation header."""
        for _ in range(self._modulo):
            self._seq = (self._seq + 1) % self._modulo
            header = str(self._seq).zfill(self.header_bytes)
            if header not in self._pending:
                return header
        raise RuntimeError("no free correlation header — too many commands in flight")

    async def send(self, command: str, data: str) -> dict:
        if self._writer is None:
            await self.connect()
        header = self._next_header()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[header] = fut
        body = f"{header}{command}{data}".encode(self.encoding)
        frame = len(body).to_bytes(self.prefix_bytes, self.byte_order) + body
        try:
            async with self._write_lock:  # keep frames from interleaving on the wire
                self._writer.write(frame)
                await self._writer.drain()
            return await asyncio.wait_for(fut, timeout=self.timeout)
        finally:
            self._pending.pop(header, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                prefix = await self._reader.readexactly(self.prefix_bytes)
                n = int.from_bytes(prefix, self.byte_order)
                raw = await self._reader.readexactly(n)
                text = raw.decode(self.encoding, errors="replace")
                header = text[: self.header_bytes]
                fut = self._pending.get(header)
                if fut is not None and not fut.done():
                    fut.set_result(self._parse(text))
                # a reply with no waiting caller (timed-out/late) is dropped.
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._fail_all(ConnectionError("hsm connection closed"))

    def _parse(self, text: str) -> dict:
        hb = self.header_bytes
        return {
            "header": text[:hb],
            "response_code": text[hb : hb + 2],
            "error_code": text[hb + 2 : hb + 4],
            "ok": text[hb + 2 : hb + 4] == "00",
            "data": text[hb + 4 :],
            "raw": text,
        }

    def _fail_all(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("hsm client: error closing socket: %s", exc)
        self._fail_all(ConnectionError("hsm connection closed"))
        self._reader = self._writer = None


class HSMAdapter(BaseAdapter):
    """payShield host-command client: pipelined, optionally pooled connections."""

    def __init__(self, config: dict, pool_size: int = 100):
        super().__init__(config, pool_size)
        self.host = config.get("host", "127.0.0.1")
        self.port = int(config.get("port", 1500))
        self.header_bytes = int(config.get("header_bytes", len(str(config.get("header", "HDR1")))))
        self.prefix_bytes = int(config.get("length_prefix_bytes", 2))
        self.byte_order = config.get("length_byte_order", "big")
        self.encoding = config.get("encoding", "ascii")
        self.timeout = float(config.get("response_timeout_sec", 5))
        #: number of sockets to open; commands round-robin across them.
        self.connections = max(1, int(config.get("connections", config.get("pool_size", 1))))
        self._conns: list[_HsmConnection] = []
        self._rr = 0

    # -- lifecycle -----------------------------------------------------------

    def _make_conn(self) -> _HsmConnection:
        return _HsmConnection(
            self.host,
            self.port,
            header_bytes=self.header_bytes,
            prefix_bytes=self.prefix_bytes,
            byte_order=self.byte_order,
            encoding=self.encoding,
            timeout=self.timeout,
        )

    async def connect(self) -> None:
        self._conns = [self._make_conn() for _ in range(self.connections)]
        await asyncio.gather(*(c.connect() for c in self._conns))

    async def disconnect(self) -> None:
        await asyncio.gather(*(c.close() for c in self._conns), return_exceptions=True)
        self._conns = []

    def _pick(self) -> _HsmConnection:
        """Round-robin the next connection (lazily connecting the first one)."""
        if not self._conns:
            self._conns = [self._make_conn()]
        conn = self._conns[self._rr % len(self._conns)]
        self._rr += 1
        return conn

    async def health_check(self) -> bool:
        try:
            parsed = await self._pick().send("NC", "")
            return parsed.get("error_code") == "00"
        except Exception:  # noqa: BLE001 — never raise from a health check
            return False

    # -- execution -----------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        payload = payload or {}
        start = time.monotonic()
        builder = _BUILDERS.get(action)
        try:
            if builder is not None:
                command, data = builder(payload)
            else:  # raw escape hatch: pass command + data straight through
                raw_cmd = payload.get("command")
                if raw_cmd is None and len(action) != 2:
                    # A command-less payload under a non-command action (e.g. a
                    # relay forwarding a sample that didn't decode to a payShield
                    # command) would silently truncate the action name onto the
                    # wire ("relay" → "re") and bounce as HSM error 30. Name the
                    # real problem instead.
                    raise ValueError(
                        f"{action}: payload has no 'command' — the inbound "
                        "request didn't decode to a payShield command. For a "
                        "flow test-fire, use a sample like "
                        '{"command": "NC", "data": ""}.'
                    )
                command = str(raw_cmd if raw_cmd is not None else action)[:2]
                data = str(payload.get("data", ""))
            parsed = await self._pick().send(command, data)
            parsed.update(_parse_action_result(action, parsed))
            return StepResult(
                success=parsed.get("error_code") == "00",
                request_payload={"action": action, "command": command, "data": data},
                response_payload=parsed,
                duration_ms=int((time.monotonic() - start) * 1000),
                raw_log=parsed.get("raw", ""),
                error=(
                    None
                    if parsed.get("error_code") == "00"
                    else f"HSM error {parsed.get('error_code')}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                success=False,
                request_payload={"action": action, "payload": payload},
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )


def _parse_action_result(action: str, parsed: dict) -> dict:
    """Pull the salient value out of an action's response data for assertions."""
    data = parsed.get("data", "")
    if action in ("generate_cvv",):
        return {"cvv": data[:3]}
    if action in ("nc", "diagnostics"):
        return {"lmk_check": data[:16], "firmware": data[16:25]}
    if action in ("generate_key", "import_key"):
        # leading key token (scheme tag + hex) then a 6-hex KCV at the end
        return {"key": data[:-6] if len(data) > 6 else data, "kcv": data[-6:]}
    if action == "key_check":
        return {"kcv": data[:6]}
    if action in (
        "translate_pin",
        "translate_pin_tpk_zpk",
        "translate_pin_zpk_zpk",
        "dukpt_translate_pin",
    ):
        return {"pin_length": data[:2], "pin_block": data[2:18]}
    if action == "verify_arqc":
        return {"arpc": data} if data else {}
    return {}
