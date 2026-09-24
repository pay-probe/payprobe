"""ADR-0011 phase 4: binary ISO 8583 on a live TCP socket.

The falsifiable milestone: the flows ``test_visa_simulator.py`` covers in ASCII
pass over a live socket with ``VisaSimulator`` under the ``binary`` profile, and
the bytes that crossed the wire decode with the shared codec (the same one the
Inspector's ``iso8583_analyze`` uses) to exactly the values the sender packed.
Everything here is byte comparison and status codes; nothing is judged by eye.
"""

from __future__ import annotations

import asyncio

import pytest

from payprobe_common import iso8583 as shared
from worker.adapters.scheme.visa import VISA_FIELDS, VisaSimulator
from worker.adapters.tcp import framing
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.responder import TcpResponder

BINARY_FRAMING = {"length_prefix_bytes": 2, "length_encoding": "bcd"}


async def _adapter(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "fields": VISA_FIELDS,
        "encoding": "binary",
        "framing": dict(BINARY_FRAMING),
        "sign_on": {"enabled": False},
        "response_timeout_sec": 2,
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    a = TcpAdapter(cfg)
    await a.connect()
    return a


async def _visa(visa_cfg=None, **overrides):
    cfg = {
        "protocol": "visa",
        "fields": VISA_FIELDS,
        "encoding": "binary",
        "framing": dict(BINARY_FRAMING),
    }
    if visa_cfg is not None:
        cfg["visa"] = visa_cfg
    cfg.update(overrides)
    r = VisaSimulator(cfg)
    port = await r.start()
    return r, port


async def _raw_exchange(port: int, frame: bytes) -> bytes:
    """Send one framed message over a plain socket and return the reply body bytes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(frame)
        await writer.drain()
        prefix = await asyncio.wait_for(reader.readexactly(2), 2)
        n = framing.decode_length(prefix, "big", "bcd")
        return await asyncio.wait_for(reader.readexactly(n), 2)
    finally:
        writer.close()


# -- the VISA flow set, now under the binary profile ---------------------------


async def test_visa_stand_in_approves_over_binary_wire():
    r, port = await _visa({"stand_in": True})
    a = await _adapter(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"]
        # the simulator decoded the request under the same profile: 16-digit PAN intact
        assert r.received[-1]["de"]["2"] == "5555550000001111"
        assert r.received[-1]["de"]["4"] == "000000001000"
    finally:
        await a.disconnect()
        await r.stop()


async def test_visa_amount_limit_declines_61_over_binary_wire():
    r, port = await _visa({"decline_over": "000000100000"})
    a = await _adapter(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000100000"}}
        )
        assert res.response_payload["response_code"] == "61"
    finally:
        await a.disconnect()
        await r.stop()


async def test_visa_blocked_bin_declines_05_over_binary_wire():
    r, port = await _visa({"block_bins": ["400000"], "block_response": "05"})
    a = await _adapter(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4000001234567899", "4": "000000001000"}}
        )
        assert res.response_payload["response_code"] == "05"
    finally:
        await a.disconnect()
        await r.stop()


async def test_visa_network_management_and_reversal_over_binary_wire():
    r, port = await _visa({"stand_in": True})
    a = await _adapter(port)
    try:
        echo = await a.execute("send_0800", {"values": {"70": "301"}})
        assert echo.success and echo.response_payload["mti"] == "0810"
        rev = await a.execute(
            "send_0400",
            {
                "values": {
                    "2": "5555550000001111",
                    "4": "000000001000",
                    "37": "RRN000000001",
                    "90": "020000012309241314150000000000000000000000",
                }
            },
        )
        assert rev.success and rev.response_payload["mti"] == "0410"
    finally:
        await a.disconnect()
        await r.stop()


# -- what actually crossed the wire ---------------------------------------------


async def test_wire_bytes_are_binary_and_decode_byte_identically():
    r, port = await _visa({"stand_in": True})
    try:
        values = {
            "2": "5555550000001111",
            "3": "000000",
            "4": "000000001000",
            "7": "0924131415",
            "11": "000042",
            "41": "TERM0001",
            "49": "840",
            "52": "0123456789ABCDEF",
        }
        body = shared.pack("0200", values, VISA_FIELDS, "binary")
        # binary MTI + 8-byte bitmap: nothing ASCII-looking at the front
        assert body[:2] == bytes.fromhex("0200") and body[2:10] != b"0200".ljust(8)
        assert b"5555550000001111" not in body  # the PAN is packed, not spelled out
        reply = await _raw_exchange(port, framing.encode_length(len(body), 2, "big", "bcd") + body)
        assert reply[:2] == bytes.fromhex("0210")
        decoded = shared.unpack(reply, VISA_FIELDS, "binary")
        got = {k: v["value"] for k, v in decoded["fields"].items()}
        assert got["11"] == values["11"] and got["39"] == "00"
        assert decoded["trailing"] is None and "truncated" not in decoded
        # and the simulator saw exactly what we packed
        assert r.received[-1]["de"] == {**r.received[-1]["de"], **values}
    finally:
        await r.stop()


async def test_ascii_client_against_binary_host_fails_loudly_not_silently():
    r, port = await _visa({"stand_in": True})
    a = await _adapter(port, encoding="ascii", framing={"length_prefix_bytes": 2})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        assert not res.success
    finally:
        await a.disconnect()
        await r.stop()


# -- profile plumbing on the live path ------------------------------------------


async def test_legacy_framing_encoding_cp037_folds_to_ebcdic_text_on_both_ends():
    r = TcpResponder(
        {
            "protocol": "iso8583",
            "fields": VISA_FIELDS,
            "framing": {"length_prefix_bytes": 2, "encoding": "cp037"},
            "default": {"echo": ["11", "41"], "set": {"39": "00"}},
        }
    )
    assert r.wire_encoding == {"text": "ebcdic"}
    port = await r.start()
    a = await _adapter(port, encoding={"text": "ebcdic"}, framing={"length_prefix_bytes": 2})
    try:
        res = await a.execute("send_0200", {"values": {"4": "000000001000", "41": "TERM ABC"}})
        assert res.success, res.error
        assert res.response_payload["fields"]["41"] == "TERM ABC"
        assert r.received[-1]["de"]["41"] == "TERM ABC"
    finally:
        await a.disconnect()
        await r.stop()


async def test_disagreeing_encoding_keys_are_refused_before_anything_binds():
    # the responder refuses at construction (before it listens) …
    with pytest.raises(ValueError, match="disagree"):
        TcpResponder(
            {"protocol": "iso8583", "encoding": "binary", "framing": {"encoding": "ascii"}}
        )
    # … the adapter builds its protocol on connect, so it refuses there (before dialling)
    a = TcpAdapter(
        {"host": "127.0.0.1", "port": 1, "encoding": "binary", "framing": {"encoding": "ascii"}}
    )
    with pytest.raises(ValueError, match="disagree"):
        await a.connect()


async def test_chaos_bad_mti_corrupts_the_bcd_mti_only():
    r, port = await _visa(
        {"stand_in": True}, chaos={"malformed_pct": 100, "malformed_mode": "bad_mti"}
    )
    a = await _adapter(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        # the reply still correlates (STAN intact) but carries the bogus MTI
        assert res.response_payload["mti"] == "9999"
        assert res.response_payload["response_code"] == "00"
    finally:
        await a.disconnect()
        await r.stop()


async def test_text_fields_keep_their_spaces_on_the_wire():
    """The historical wire path stripped every space out of a decoded message; a
    DE 43 with embedded spaces was mangled. The bytes codec does not."""
    r = TcpResponder(
        {
            "protocol": "iso8583",
            "fields": VISA_FIELDS,
            "framing": {"length_prefix_bytes": 2},
            "default": {"echo": ["11", "43"], "set": {"39": "00"}},
        }
    )
    port = await r.start()
    a = await _adapter(port, encoding="ascii", framing={"length_prefix_bytes": 2})
    name = "ACME STORE 42            LONDON       GB"
    try:
        res = await a.execute("send_0200", {"values": {"4": "000000001000", "43": name}})
        assert res.success, res.error
        assert r.received[-1]["de"]["43"] == name
        assert res.response_payload["fields"]["43"] == name
    finally:
        await a.disconnect()
        await r.stop()
