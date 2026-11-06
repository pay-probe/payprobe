"""Tests for the universal TcpIso8583Adapter against a mock ISO 8583 server.

Run from the packages/ directory:
    cd packages && python -m pytest worker/tests/test_tcp_adapter.py -v
"""

import asyncio

import pytest

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
    if fr.get("length_encoding") == "ascii":
        return f"{length:0{pb}d}".encode("ascii") + payload
    return length.to_bytes(pb, bo) + payload


async def _read_frame(reader: asyncio.StreamReader, fr: dict) -> str:
    pb = fr["length_prefix_bytes"]
    bo = fr["byte_order"]
    tpdu_bytes = fr.get("tpdu_bytes", 0)
    prefix = await reader.readexactly(pb)
    length = (
        int(prefix.decode("ascii"))
        if fr.get("length_encoding") == "ascii"
        else int.from_bytes(prefix, bo)
    )
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


async def test_ascii_length_prefix_round_trips_against_a_digits_framed_host():
    """framing.length_encoding = ascii: a 4-byte prefix reads ``0043``."""
    fr = {**_DEFAULT_FR, "length_prefix_bytes": 4, "length_encoding": "ascii"}
    server = MockIsoServer(fr=fr)
    port = await server.start()
    adapter = await _make_adapter(
        port, framing={"length_prefix_bytes": 4, "length_encoding": "ascii"}
    )
    try:
        result = await adapter.execute("send_0200", {"amount": 250})
        assert result.success, result.error
        assert result.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_length_encoding_mismatch_fails_loudly_not_silently():
    """A binary-framed adapter against a digits-framed host reads a prefix
    that is not what it expects; the failure names the mismatch."""
    from worker.adapters.tcp import framing

    with pytest.raises(ValueError, match="not decimal digits"):
        framing.decode_length((43).to_bytes(2, "big"), "big", "ascii")
    with pytest.raises(ValueError, match="length_encoding"):
        framing.normalise_length_encoding("hex")  # bcd is valid since ADR-0011


# -- health-check modes, wire journal, dry run ------------------------------
#
# Background (2026-11): against a real POS host the probe's 0800 echo went
# unanswered for 30 s and *Test connection* said only "connected, but health
# check failed"; on another port the host hung up ~10 ms after the first frame
# and nothing recorded which bytes had gone out. These tests pin the three
# health-check modes, the reasons they report, and the byte-level journal.


class SlammingHost:
    """A host that reads whatever arrives, then hangs up ``after_ms`` later —
    what a switch does with a frame it cannot parse ("invalid format")."""

    def __init__(self, after_ms: int = 10):
        self.after_ms = after_ms
        self.received: list[bytes] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=2)
            self.received.append(data)
        except TimeoutError:
            pass
        await asyncio.sleep(self.after_ms / 1000)
        writer.close()


class SilentHost:
    """Accepts the connection, reads nothing, answers nothing, never closes
    until the test stops it."""

    async def start(self) -> int:
        self._stop = asyncio.Event()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._stop.set()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        await self._stop.wait()
        writer.close()


_NO_RECONNECT = {"reconnect": {"enabled": False}}


async def test_health_probe_mode_sends_the_default_echo_and_reports_the_reply():
    """Default mode (today's behaviour): the protocol's 0800 echo, healthy when
    the host answers; ``last_health`` carries the exact bytes both ways."""
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        assert await adapter.health_check() is True
        h = adapter.last_health
        assert h["mode"] == "probe" and h["healthy"] is True and h["error"] is None
        assert h["sent"]["source"] == "iso8583 default probe"
        assert bytes.fromhex(h["sent"]["hex"])[2:6] == b"0800"  # 2-byte MLI then the MTI
        assert bytes.fromhex(h["reply"]["hex"])[2:6] == b"0810"
        assert [e["dir"] for e in h["wire"]] == ["open", "tx", "rx"]
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_health_probe_mode_names_the_unanswered_probe_and_the_way_out():
    host = SilentHost()
    port = await host.start()
    adapter = await _make_adapter(port, response_timeout_sec=0.2, **_NO_RECONNECT)
    try:
        assert await adapter.health_check() is False
        err = adapter.last_health["error"]
        assert "no reply within 0.2s" in err
        assert "health_check.mode" in err and "'connect'" in err and "'message'" in err
        assert adapter.last_health["sent"]["bytes"] > 0  # it did send the echo
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_health_connect_mode_sends_nothing_and_passes_on_an_open_socket():
    """``sign_on.enabled: false`` + ``health_check.mode: connect``: not one
    byte goes out; a socket still open after ``settle_ms`` is healthy."""
    host = SilentHost()
    port = await host.start()
    adapter = await _make_adapter(
        port, health_check={"mode": "connect", "settle_ms": 50}, **_NO_RECONNECT
    )
    try:
        assert await adapter.health_check() is True
        h = adapter.last_health
        assert h["mode"] == "connect" and h["sent"] is None
        assert [e["dir"] for e in adapter.wire_events()] == ["open"]  # nothing sent
        assert "still open after 50 ms" in h["note"]
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_health_connect_mode_fails_when_the_host_hangs_up_within_settle():
    host = SlammingHost(after_ms=10)
    port = await host.start()
    adapter = await _make_adapter(
        port, health_check={"mode": "connect", "settle_ms": 150}, **_NO_RECONNECT
    )
    try:
        # a sign-on-free connect sends nothing, so poke the host once so it
        # hangs up the way it would on an unparseable frame
        adapter._writer.write(b"junk")
        await adapter._writer.drain()
        assert await adapter.health_check() is False
        err = adapter.last_health["error"]
        # "connection closed by the host" when it dropped during settle, or
        # "not connected" when the reader saw the close first — either way the
        # timing hint travels with the reason
        assert "closed" in err or "not connected" in err
        assert "peer closed the connection before anything was sent" in err
        assert adapter.wire_events()[-1]["dir"] == "close"
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_health_message_mode_sends_the_configured_message():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(
        port,
        health_check={
            "mode": "message",
            "action": "send_0800",
            "payload": {"mti": "0800", "values": {"70": "001"}},
        },
    )
    try:
        assert await adapter.health_check() is True
        sent = server.received[-1]
        assert sent["mti"] == "0800" and sent["fields"]["70"]["value"] == "001"
        assert adapter.last_health["sent"]["source"] == "health_check message"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_health_message_mode_without_a_message_is_refused_at_connect():
    server = MockIsoServer()
    port = await server.start()
    try:
        with pytest.raises(ValueError, match="health_check.mode 'message' needs"):
            await _make_adapter(port, health_check={"mode": "message"})
        with pytest.raises(ValueError, match="health_check.mode must be one of"):
            await _make_adapter(port, health_check={"mode": "ping"})
    finally:
        await server.stop()


async def test_configured_sign_on_message_doubles_as_the_health_probe():
    """One sign-on message (0800 / DE70 = 001) serves sign-on *and* the probe
    — the probe never silently falls back to the default 301 echo."""
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(
        port,
        sign_on={"enabled": True, "payload": {"mti": "0800", "values": {"70": "001"}}},
    )
    try:
        assert server.received[-1]["fields"]["70"]["value"] == "001"  # sign-on at connect
        assert await adapter.health_check() is True
        assert server.received[-1]["fields"]["70"]["value"] == "001"  # the probe too
        assert adapter.last_health["sent"]["source"] == "sign_on message"
    finally:
        await adapter.disconnect()
        await server.stop()


async def test_wire_journal_records_the_exact_bytes_and_the_close_timing():
    """A host that hangs up ~10 ms after the first frame: the journal holds the
    frame as sent, the close, and how long after the send it came; the step
    error carries the same timing."""
    host = SlammingHost(after_ms=10)
    port = await host.start()
    adapter = await _make_adapter(
        port,
        framing={"length_prefix_bytes": 4, "length_encoding": "chardec"},
        mhdr="CDb4",
        response_timeout_sec=2,
        **_NO_RECONNECT,
    )
    try:
        res = await adapter.execute("send_0800", {"values": {"70": "301"}})
        assert res.success is False
        assert "peer closed the connection" in (res.error or "")
        assert "ms after the last send" in res.error
        events = adapter.wire_events()
        assert [e["dir"] for e in events] == ["open", "tx", "close"]
        tx, close = events[1], events[2]
        # the exact bytes the host saw are the bytes the journal recorded
        assert host.received[0].hex().startswith(tx["hex"])
        assert host.received[0][:8] == b"0069CDb4"  # 4-digit ASCII length, then mhdr
        assert tx["bytes"] == len(host.received[0]) == 73
        assert "MTI 0800" in tx["note"]
        assert close["note"].startswith("peer closed:")
        assert "ms after the last send, 0 bytes read since" in close["note"]
        assert "0 of 4 expected bytes arrived" in close["note"]
        assert 0 <= close["t_ms"] - tx["t_ms"] < 1000
        assert adapter.last_health == {}  # no health check ran
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_raw_log_now_carries_the_received_frame_too():
    server = MockIsoServer()
    port = await server.start()
    adapter = await _make_adapter(port)
    try:
        res = await adapter.execute("send_0200", {"amount": 100})
        assert res.success
        lines = res.raw_log.splitlines()
        assert lines[0].startswith("[iso8583] → sent") and lines[1].startswith("  TX ")
        assert lines[2].startswith("[iso8583] ← recv ") and "MTI 0210" in lines[2]
        assert lines[3].startswith("  RX ")
        assert bytes.fromhex(lines[3].split()[1])[2:6] == b"0210"
    finally:
        await adapter.disconnect()
        await server.stop()


def test_preview_frame_is_a_dry_run_that_needs_no_socket():
    adapter = TcpAdapter(
        {
            "host": "203.0.113.9",  # TEST-NET: never dialled
            "port": 1,
            "protocol": "iso8583",
            "framing": {"length_prefix_bytes": 4, "length_encoding": "chardec"},
            "mhdr": "CDb4",
            "sign_on": {"enabled": False},
        }
    )
    out = adapter.preview_frame(
        "send_0800",
        {"values": {"70": "301", "11": "000001", "7": "1106123000", "12": "123000", "13": "1106"}},
    )
    frame = bytes.fromhex(out["hex"])
    assert frame[:8] == b"0069CDb4" and frame[8:12] == b"0800"
    assert out["total_bytes"] == 73 == 4 + 4 + out["body_bytes"]
    assert out["declared_length"] == 69 == 4 + out["body_bytes"]  # header counted
    assert out["prefix_hex"] == "30303639" and out["header_hex"] == "43446234"
    assert out["framing"] == "prefix=4 ascii · header=43446234/4in (counted)"
    assert out["request"]["mti"] == "0800" and out["health_check_mode"] == "probe"
    # with nothing given it previews the health-check message (what *Test
    # connection* sends): the default 0800 echo here
    assert adapter.preview_frame()["request"]["values"]["70"] == "301"


def test_preview_frame_refuses_a_framing_typo_instead_of_binding_the_default():
    adapter = TcpAdapter(
        {"host": "h", "port": 1, "framing": {"length_encoding": "hex"}, "sign_on": {}}
    )
    with pytest.raises(ValueError, match="length_encoding must be one of binary, ascii, bcd"):
        adapter.preview_frame("send_0200", {"amount": 1})


async def test_top_level_header_key_is_called_out_not_dropped(caplog):
    """``header`` at the top level was never a TcpAdapter key; it now logs a
    warning pointing at framing.header / mhdr instead of vanishing silently."""
    server = MockIsoServer()
    port = await server.start()
    with caplog.at_level("WARNING", logger="worker.adapters.tcp.adapter"):
        adapter = await _make_adapter(port, header="CDb4")
    try:
        assert any("'header' is not a TcpAdapter key" in r.message for r in caplog.records)
        assert adapter.frame_spec.header_out == b""  # and it did not frame with it
    finally:
        await adapter.disconnect()
        await server.stop()
