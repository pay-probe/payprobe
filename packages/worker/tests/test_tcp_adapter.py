"""Tests for the universal TcpIso8583Adapter against a mock ISO 8583 server.

Run from the packages/ directory:
    cd packages && python -m pytest worker/tests/test_tcp_adapter.py -v
"""

import asyncio


from worker.adapters.tcp import iso8583
from worker.adapters.tcp.adapter import TcpAdapter

_RESP_MTI = {"0100": "0110", "0200": "0210", "0400": "0410", "0800": "0810"}


def _frame(body: str, fr: dict) -> bytes:
    """Mirror of TcpIso8583Adapter._frame for the mock server side."""
    pb = fr["length_prefix_bytes"]
    bo = fr["byte_order"]
    tpdu = bytes.fromhex(fr.get("tpdu_hex", "") or "")
    body_bytes = body.encode("ascii")
    payload = tpdu + body_bytes
    counted = len(payload) if fr["includes_header"] else len(body_bytes)
    length = counted + (pb if fr["includes_prefix"] else 0)
    return length.to_bytes(pb, bo) + payload


async def _read_frame(reader: asyncio.StreamReader, fr: dict) -> str:
    pb = fr["length_prefix_bytes"]
    bo = fr["byte_order"]
    tpdu_bytes = fr.get("tpdu_bytes", 0)
    prefix = await reader.readexactly(pb)
    length = int.from_bytes(prefix, bo)
    core = length - (pb if fr["includes_prefix"] else 0)
    remaining = core if fr["includes_header"] else core + tpdu_bytes
    data = await reader.readexactly(remaining)
    return data[tpdu_bytes:].decode("ascii")


_DEFAULT_FR = {
    "length_prefix_bytes": 2,
    "byte_order": "big",
    "includes_prefix": False,
    "includes_header": True,
    "tpdu_hex": "",
    "tpdu_bytes": 0,
}


class MockIsoServer:
    """Tiny ISO 8583 echo server: replies <reqMTI+0x10>, DE39=rc, echoes DE11."""

    def __init__(
        self, fr=None, *, reorder=False, drop=False, response_code="00", drop_after_first=False
    ):
        self.fr = fr or _DEFAULT_FR
        self.reorder = reorder
        self.drop = drop
        self.response_code = response_code
        self.drop_after_first = drop_after_first
        self._dropped_once = False
        self.received: list[dict] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def _response(self, parsed: dict) -> str:
        mti = _RESP_MTI.get(parsed["mti"], parsed["mti"])
        de = {k: v["value"] for k, v in parsed["fields"].items()}
        values = {"11": de.get("11", "000000"), "39": self.response_code}
        if "37" in de:
            values["37"] = de["37"]
        return iso8583.iso_pack(mti, values, iso8583.DEFAULT_FIELDS)

    async def _handle(self, reader, writer):
        pending = []
        try:
            while True:
                text = await _read_frame(reader, self.fr)
                parsed = iso8583.iso_unpack(text, iso8583.DEFAULT_FIELDS)
                self.received.append(parsed)
                if self.drop:
                    continue
                resp = self._response(parsed)
                if self.drop_after_first and not self._dropped_once:
                    # answer once, then sever the connection to exercise reconnect
                    writer.write(_frame(resp, self.fr))
                    await writer.drain()
                    self._dropped_once = True
                    writer.close()
                    return
                if self.reorder:
                    pending.append(resp)
                    if len(pending) >= 2:
                        for r in reversed(pending):  # answer out of order
                            writer.write(_frame(r, self.fr))
                        await writer.drain()
                        pending = []
                else:
                    writer.write(_frame(resp, self.fr))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass


async def _make_adapter(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "sign_on": {"enabled": False},
        "response_timeout_sec": 3,
    }
    cfg.update(overrides)
    adapter = TcpAdapter(cfg)
    await adapter.connect()
    return adapter


# -- codec round-trip ---------------------------------------------------------


def test_codec_round_trip():
    values = {"2": "4111111111111111", "4": "000000010000", "11": "000123", "39": "00"}
    packed = iso8583.iso_pack("0210", values, iso8583.DEFAULT_FIELDS)
    parsed = iso8583.iso_unpack(packed, iso8583.DEFAULT_FIELDS)
    assert parsed["mti"] == "0210"
    assert parsed["fields"]["2"]["value"] == "4111111111111111"
    assert parsed["fields"]["39"]["value"] == "00"


# -- basic exchange -----------------------------------------------------------


async def test_send_message_correlates_and_shapes_response():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        result = await adapter.execute("send_0200", {"amount": 10000, "pan": "4111111111111111"})
        assert result.success
        assert result.response_payload["mti"] == "0210"
        assert result.response_payload["response_code"] == "00"
        assert result.response_payload["stan"]  # auto-generated STAN echoed back
        # the server saw the amount packed into DE4 (12-digit minor units)
        assert server.received[0]["fields"]["4"]["value"] == "000000010000"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_concurrent_requests_correlate_by_stan_not_order():
    # server answers in reverse arrival order; correct STAN correlation required
    server = MockIsoServer(reorder=True)
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        r1, r2 = await asyncio.gather(
            adapter.execute("send_message", {"mti": "0200", "values": {"11": "111111"}}),
            adapter.execute("send_message", {"mti": "0200", "values": {"11": "222222"}}),
        )
        assert r1.response_payload["stan"] == "111111"
        assert r2.response_payload["stan"] == "222222"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_health_check_signs_on():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        assert await adapter.health_check() is True
        assert server.received[-1]["mti"] == "0800"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_timeout_when_no_response():
    server = MockIsoServer(drop=True)
    port = await server.start()
    adapter = await _make_adapter(port, response_timeout_sec=0.3)
    try:
        result = await adapter.execute("send_0200", {"amount": 100})
        assert result.success is False
        assert "No response" in (result.error or "")
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_response_code_passthrough_for_assertions():
    server = MockIsoServer(response_code="05")  # declined
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        result = await adapter.execute("send_0200", {"amount": 100})
        # transport succeeded; assertions decide pass/fail on the response code
        assert result.success
        assert result.response_payload["response_code"] == "05"
    finally:
        await adapter.disconnect()
        await server.stop()


# -- configurable framing -----------------------------------------------------


async def test_reconnects_after_socket_drop():
    server = MockIsoServer(drop_after_first=True)
    port = await server.start()
    adapter = await _make_adapter(port, reconnect={"enabled": True, "backoff_initial_sec": 0.05})
    try:
        r1 = await adapter.execute("send_0200", {"amount": 100})
        assert r1.success
        # server severed the link after replying; give the reader time to notice
        # and the adapter to reconnect, then the next call should succeed.
        await asyncio.sleep(0.3)
        r2 = await adapter.execute("send_0200", {"amount": 200})
        assert r2.success
        assert r2.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_events_sink_collects_uncorrelated_messages():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        # a reply whose STAN matches no pending request -> unsolicited
        packed = iso8583.iso_pack("0810", {"11": "999999", "39": "00"}, iso8583.DEFAULT_FIELDS)
        adapter._dispatch(iso8583.iso_unpack(packed, iso8583.DEFAULT_FIELDS))
        assert len(adapter.events()) == 1
        drained = adapter.drain_events()
        assert drained[0]["fields"]["11"]["value"] == "999999"
        assert adapter.events() == []  # drained
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_response_exposes_named_des_and_raw_fields():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        r = await adapter.execute(
            "send_message",
            {
                "mti": "0200",
                "values": {"11": "123456", "37": "RRN000000123", "2": "4111111111111111"},
            },
        )
        rp = r.response_payload
        assert rp["mti"] == "0210"
        assert rp["response_code"] == "00"  # DE39
        assert rp["stan"] == "123456"  # DE11 echoed
        assert rp["rrn"] == "RRN000000123"  # DE37 echoed
        assert "39" in rp["fields"]  # raw DE map for the report inspector
    finally:
        await adapter.disconnect()
        await server.stop()


class MockHeaderEchoServer:
    """HSM-style host: echoes the 4-byte header, replies <cmd+1?>/<error>/<data>."""

    def __init__(self, fr=None, *, reorder=False, error_code="00"):
        self.fr = fr or _DEFAULT_FR
        self.reorder = reorder
        self.error_code = error_code
        self.received: list[str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def _response(self, text: str) -> str:
        header = text[:4]
        command = text[4:6]
        data = text[6:]
        resp_cmd = command[0] + chr(ord(command[1]) + 1) if len(command) == 2 else "ZZ"
        return f"{header}{resp_cmd}{self.error_code}{data}"

    async def _handle(self, reader, writer):
        pending = []
        try:
            while True:
                text = await _read_frame(reader, self.fr)
                self.received.append(text)
                resp = self._response(text)
                if self.reorder:
                    pending.append(resp)
                    if len(pending) >= 2:
                        for r in reversed(pending):
                            writer.write(_frame(r, self.fr))
                        await writer.drain()
                        pending = []
                else:
                    writer.write(_frame(resp, self.fr))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass


# -- header_echo / HSM protocol ----------------------------------------------


async def test_header_echo_send_command():
    server = MockHeaderEchoServer()
    port = await server.start()
    adapter = await _make_adapter(port, protocol="header_echo")
    try:
        result = await adapter.execute("send_command", {"command": "A0", "data": "DEADBEEF"})
        assert result.success
        assert result.response_payload["response_code"] == "00"
        assert result.response_payload["data"] == "DEADBEEF"
        assert result.response_payload["header"]  # 4-byte header echoed back
        # server saw header(4) + command(2) + data
        assert server.received[0][4:6] == "A0"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_header_echo_concurrent_correlates_by_header():
    server = MockHeaderEchoServer(reorder=True)  # answers out of order
    port = await server.start()
    adapter = await _make_adapter(port, protocol="header_echo")
    try:
        r1, r2 = await asyncio.gather(
            adapter.execute("send_command", {"command": "A0", "data": "1111"}),
            adapter.execute("send_command", {"command": "A0", "data": "2222"}),
        )
        # each call gets its own reply despite reversed server ordering
        assert r1.response_payload["data"] == "1111"
        assert r2.response_payload["data"] == "2222"
        assert r1.response_payload["header"] != r2.response_payload["header"]
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_header_echo_health_check():
    server = MockHeaderEchoServer()
    port = await server.start()
    adapter = await _make_adapter(port, protocol="header_echo")
    try:
        assert await adapter.health_check() is True
        assert server.received[-1][4:6] == "NC"  # default diagnostic command
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_header_echo_error_code_passthrough():
    server = MockHeaderEchoServer(error_code="42")
    port = await server.start()
    adapter = await _make_adapter(port, protocol="header_echo")
    try:
        result = await adapter.execute("send_command", {"command": "A0"})
        assert result.success  # transport ok
        assert result.response_payload["response_code"] == "42"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_framing_with_tpdu_and_length_includes_prefix():
    fr = {
        "length_prefix_bytes": 2,
        "byte_order": "big",
        "includes_prefix": True,
        "includes_header": True,
        "tpdu_hex": "6000000000",
        "tpdu_bytes": 5,
    }
    server = MockIsoServer(fr=fr)
    port = await server.start()
    adapter = await _make_adapter(
        port,
        framing={
            "length_prefix_bytes": 2,
            "length_byte_order": "big",
            "length_includes_prefix": True,
            "length_includes_header": True,
            "tpdu_bytes": 5,
            "tpdu_outbound_hex": "6000000000",
        },
    )
    try:
        result = await adapter.execute("send_0200", {"amount": 250})
        assert result.success
        assert result.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await server.stop()
