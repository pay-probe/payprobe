"""TcpProxy (transparent tap) — stage 1 pass-through relay.

Topology: a backend TcpResponder (the "real" upstream) <- a TcpProxy <- a client
(a real TcpAdapter, or a raw socket for byte-exact assertions).
"""

import asyncio

import pytest

from worker.adapters.tcp import iso8583
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.proxy import TcpProxy
from worker.adapters.tcp.responder import TcpResponder

APPROVE = {
    "protocol": "iso8583",
    "default": {"echo": ["11", "37"], "set": {"39": "00", "38": "AUTH99"}},
}


def _make_frame(body: bytes, pb: int, bo: str, inc: bool) -> bytes:
    """Build a wire frame the way TcpProxy._frame does (tpdu-free path):
    length-prefix + body, where the length optionally counts its own prefix."""
    length = len(body) + (pb if inc else 0)
    return length.to_bytes(pb, bo) + body


async def _raw_echo_upstream(received: list[bytes]):
    """A dumb upstream that records every byte it receives and echoes it back
    verbatim — lets us assert the proxy is exactly pass-through."""

    async def handle(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
            received.append(data)
            writer.write(data)
            await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_tap_relays_request_and_response():
    backend = TcpResponder(dict(APPROVE))
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "tap",
        }
    )
    pport = await proxy.start()
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": pport,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await a.connect()
    try:
        res = await a.execute("send_0200", {"amount": 1000, "pan": "5111111111111111"})
        assert res.success
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"] == "AUTH99"
        # the proxy observed both directions
        assert len(proxy.received) >= 2
        assert {p.get("direction") for p in proxy.received} == {"c2u", "u2c"}
    finally:
        await a.disconnect()
        await proxy.stop()
        await backend.stop()


@pytest.mark.parametrize(
    "pb,bo,inc",
    [
        (2, "big", False),
        (2, "big", True),
        (2, "little", False),
        (1, "big", False),
        (4, "big", False),
    ],
)
async def test_tap_is_byte_exact(pb, bo, inc):
    """The golden test: bytes the upstream receives == bytes the client sent,
    and vice versa — across framing variants."""
    received: list[bytes] = []
    upstream, up_port = await _raw_echo_upstream(received)
    try:
        proxy = TcpProxy(
            {
                "kind": "proxy",
                "protocol": "iso8583",
                "host": "127.0.0.1",
                "port": 0,
                "framing": {
                    "length_prefix_bytes": pb,
                    "length_byte_order": bo,
                    "length_includes_prefix": inc,
                },
                "upstream": {"host": "127.0.0.1", "port": up_port},
                "mode": "tap",
            }
        )
        pport = await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", pport)
            frame = _make_frame(b"HELLO-WORLD-1234567890", pb, bo, inc)
            writer.write(frame)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(frame)), timeout=3)
            await asyncio.sleep(0.05)
            assert b"".join(received) == frame  # upstream got the exact bytes
            assert echoed == frame  # client got the exact bytes back
            writer.close()
        finally:
            await proxy.stop()
    finally:
        upstream.close()
        await upstream.wait_closed()


async def test_raw_mode_relays_byte_exact_without_decode():
    """``decode: false`` (raw posture): bytes pass through unchanged, but the
    proxy never parses them — every observed frame is recorded as opaque ``raw``
    and bucketed under ``(raw)``, regardless of wire protocol."""
    received: list[bytes] = []
    upstream, up_port = await _raw_echo_upstream(received)
    try:
        proxy = TcpProxy(
            {
                "kind": "proxy",
                "protocol": "iso8583",
                "host": "127.0.0.1",
                "port": 0,
                "framing": {"length_prefix_bytes": 2, "length_byte_order": "big"},
                "upstream": {"host": "127.0.0.1", "port": up_port},
                "mode": "tap",
                "decode": False,
            }
        )
        assert proxy.decode_frames is False
        pport = await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", pport)
            frame = _make_frame(b"ANY-PROTOCOL-BYTES", 2, "big", False)
            writer.write(frame)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(frame)), timeout=3)
            await asyncio.sleep(0.05)
            assert b"".join(received) == frame  # upstream got exact bytes
            assert echoed == frame  # client got exact bytes back
            # observed but NOT decoded
            assert proxy.received and all(p.get("raw") for p in proxy.received)
            assert set(proxy.by_mti) == {"(raw)"}
            writer.close()
        finally:
            await proxy.stop()
    finally:
        upstream.close()
        await upstream.wait_closed()


def test_multi_upstream_round_robin_balances_members():
    """A relay to a fleet (``upstreams``) balances inbound connections across
    members round-robin; a single upstream is returned unchanged."""
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstreams": [
                {"host": "10.0.0.1", "port": 8000},
                {"host": "10.0.0.2", "port": 8001},
            ],
            "selection": {"policy": "round_robin"},
        }
    )
    picks = [proxy._pick_upstream() for _ in range(4)]
    assert picks == [
        ("10.0.0.1", 8000),
        ("10.0.0.2", 8001),
        ("10.0.0.1", 8000),
        ("10.0.0.2", 8001),
    ]
    single = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "10.0.0.9", "port": 9000},
        }
    )
    assert single._pick_upstream() == ("10.0.0.9", 9000)


def test_relay_trace_pairs_request_and_reply_header_echo():
    """A header-echo (HSM) relay emits one correlated Network-Trace record per
    transaction — request command in, reply code out, keyed by the echoed header.
    Raw/undecoded frames produce no record."""
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "header_echo",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": 1500},
        }
    )
    # request (client→upstream) then reply (upstream→client), same header "HDR1".
    # Shapes mirror the real header_echo _decode, which ALSO carries the raw frame
    # text under "raw" — the trace must not mistake that for a no-decode frame.
    proxy._record_relay_trace(
        {"header": "HDR1", "command": "NC", "data": "", "raw": "HDR1NC"}, "c2u"
    )
    # real header_echo reply: no "error" key — the 2-char code leads "data"
    proxy._record_relay_trace(
        {"header": "HDR1", "command": "ND", "data": "00FIRMWARE", "raw": "HDR1ND00FIRMWARE"}, "u2c"
    )
    rows = proxy.traces()
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "hsm" and r["correlation_id"] == "HDR1"
    assert r["mti"] == "NC" and r["response_code"] == "00"
    assert proxy.traces("HDR1") and proxy.traces("nope") == []

    # raw / undecoded frames are skipped (no correlation)
    proxy._record_relay_trace({"raw": True, "bytes": 12, "direction": "c2u"}, "c2u")
    assert len(proxy.traces()) == 1


def test_relay_trace_respects_capture_gate():
    """When capture is paused (``_capture`` False), the proxy still relays but
    records nothing; resuming starts collecting again."""
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "header_echo",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": 1500},
        }
    )
    proxy._capture = False  # paused (the default-stopped state at launch)
    proxy._record_relay_trace(
        {"header": "0001", "command": "NC", "data": "", "raw": "0001NC"}, "c2u"
    )
    proxy._record_relay_trace(
        {"header": "0001", "command": "ND", "data": "00", "raw": "0001ND00"}, "u2c"
    )
    assert proxy.traces() == []  # nothing recorded while paused

    proxy._capture = True  # resume
    proxy._record_relay_trace(
        {"header": "0002", "command": "A0", "data": "", "raw": "0002A0"}, "c2u"
    )
    proxy._record_relay_trace(
        {"header": "0002", "command": "ND", "data": "00", "raw": "0002ND00"}, "u2c"
    )
    assert len(proxy.traces()) == 1  # records after resume


async def test_proxy_forwards_undecodable_frame():
    """A frame that isn't valid ISO 8583 is still relayed — tap never gates the
    wire on parseability."""
    received: list[bytes] = []
    upstream, up_port = await _raw_echo_upstream(received)
    try:
        proxy = TcpProxy(
            {
                "kind": "proxy",
                "protocol": "iso8583",
                "host": "127.0.0.1",
                "port": 0,
                "upstream": {"host": "127.0.0.1", "port": up_port},
                "mode": "tap",
            }
        )
        pport = await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", pport)
            frame = _make_frame(b"\x00\x01garbage-not-iso", 2, "big", False)
            writer.write(frame)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(frame)), timeout=3)
            assert echoed == frame
            assert any(p.get("undecoded") for p in proxy.received)
            writer.close()
        finally:
            await proxy.stop()
    finally:
        upstream.close()
        await upstream.wait_closed()


async def test_tap_captures_and_redacts_relayed_traffic():
    backend = TcpResponder(dict(APPROVE))
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "tap",
        }
    )
    pport = await proxy.start()
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": pport,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await a.connect()
    try:
        await a.execute("send_0200", {"amount": 1000, "pan": "4111111111111111"})
        rows = proxy.capture_rows()
        assert {r["direction"] for r in rows} == {"c2u", "u2c"}
        req = next(r for r in rows if r["direction"] == "c2u")
        assert req["values"]["2"] == "411111******1111"  # PAN masked in capture
        # the captured session synthesises a replayable scenario
        draft = proxy.capture_scenario_draft("from-tap")
        assert len(draft["steps"]) == 1
        assert draft["steps"][0]["action"] == "send_message"
    finally:
        await a.disconnect()
        await proxy.stop()
        await backend.stop()


async def test_stub_answers_matched_and_forwards_unmatched():
    backend = TcpResponder(dict(APPROVE))  # upstream approves (DE39=00)
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "stub",
            "rules": [
                {
                    "when": {"mti": "0200", "de": {"4": {"gte": "000000100000"}}},
                    "respond": {"echo": ["11"], "set": {"39": "61"}},
                }
            ],
        }
    )
    pport = await proxy.start()
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": pport,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await a.connect()
    try:
        high = await a.execute("send_0200", {"amount": 200000})
        assert high.response_payload["response_code"] == "61"  # answered by stub
        low = await a.execute("send_0200", {"amount": 1000})
        assert low.response_payload["response_code"] == "00"  # forwarded upstream
        assert len(backend.received) == 1  # only the unmatched request hit upstream
    finally:
        await a.disconnect()
        await proxy.stop()
        await backend.stop()


async def test_intercept_mutates_response():
    backend = TcpResponder(dict(APPROVE))  # approves (DE39=00)
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "intercept",
            # rewrite any approval coming back from the host into a decline
            "rules": [{"when": {"de": {"39": {"eq": "00"}}}, "respond": {"set": {"39": "05"}}}],
        }
    )
    pport = await proxy.start()
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": pport,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await a.connect()
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.response_payload["response_code"] == "05"  # mutated on the way back
        assert len(backend.received) == 1  # request still reached the real host
    finally:
        await a.disconnect()
        await proxy.stop()
        await backend.stop()


async def test_intercept_drops_matched_request():
    backend = TcpResponder(dict(APPROVE))
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "intercept",
            "rules": [{"when": {"mti": "0200"}, "respond": {"drop": True}}],
        }
    )
    pport = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        body = iso8583.iso_pack(
            "0200", {"11": "000001", "4": "000000001000"}, iso8583.DEFAULT_FIELDS
        ).encode("ascii")
        writer.write(_make_frame(body, 2, "big", False))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert len(backend.received) == 0  # dropped — never forwarded upstream
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readexactly(2), timeout=0.4)  # no reply
        writer.close()
    finally:
        await proxy.stop()
        await backend.stop()


async def test_upstream_down_closes_client():
    # a port nothing listens on (bind, capture, release)
    tmp = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = tmp.sockets[0].getsockname()[1]
    tmp.close()
    await tmp.wait_closed()

    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": dead_port, "connect_timeout_sec": 2},
            "mode": "tap",
        }
    )
    pport = await proxy.start()
    try:
        reader, _writer = await asyncio.open_connection("127.0.0.1", pport)
        data = await asyncio.wait_for(reader.read(100), timeout=3)
        assert data == b""  # proxy dropped the client because upstream was down
    finally:
        await proxy.stop()


async def test_teardown_closes_both_legs():
    backend = TcpResponder(dict(APPROVE))
    up_port = await backend.start()
    proxy = TcpProxy(
        {
            "kind": "proxy",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 0,
            "upstream": {"host": "127.0.0.1", "port": up_port},
            "mode": "tap",
        }
    )
    pport = await proxy.start()
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        await asyncio.sleep(0.05)
        assert len(proxy._up_conns) == 1  # upstream leg opened on client connect
        writer.close()
        await asyncio.sleep(0.1)
        assert len(proxy._up_conns) == 0  # client close tore down the upstream too
        assert proxy._upstream_meta == {}
    finally:
        await proxy.stop()
        await backend.stop()
