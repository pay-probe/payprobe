"""ADR-0013 phases 0-1: message authentication on DE 64 / 128.

Vectors first (frozen from the RFC-tested primitives in crypto_tools on
2026-09-25), then the contract (field, coverage, splice, compare), then the
live path: adapter and simulator MAC each other's messages, a tampered byte or
a wrong key is detected, ``warn`` records and ``reject`` refuses.
"""

from __future__ import annotations

import pytest

from payprobe_common import iso8583 as shared
from payprobe_common.iso8583 import mac
from worker.adapters.tcp import iso8583
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.responder import TcpResponder

F = shared.ISO8583_1987
MAK = "0123456789ABCDEFFEDCBA9876543210"  # double-length 3DES key (test material)
CMAC_KEY = "2B7E151628AED2A6ABF7158809CF4F3C"  # RFC 4493 key
VALUES = {"2": "4111111111111111", "4": "000000001000", "11": "000001"}


def _spec(**over):
    return mac.resolve_mac_spec({"field": 64, "key": MAK, **over})


def _algo(spec):
    return iso8583.mac_algorithm(spec)


# -- phase 0: frozen vectors -----------------------------------------------------


def test_retail_mac_de64_ascii_vector_is_frozen():
    spec = _spec()
    wire = mac.pack_with_mac("0200", VALUES, F, "ascii", spec, _algo(spec))
    # recorded 2026-09-25 from crypto_tools.retail_mac over the 60 ASCII bytes before DE 64
    assert wire[-16:] == b"4C2F521C5A330CC2"
    assert (
        wire[:-16]
        == b"0200" + b"5020000000000001" + b"164111111111111111" + b"000000001000" + b"000001"
    )


def test_mac_differs_between_wire_profiles_for_the_same_message():
    spec = _spec()
    a = mac.pack_with_mac("0200", VALUES, F, "ascii", spec, _algo(spec))
    b = mac.pack_with_mac("0200", VALUES, F, "binary", spec, _algo(spec))
    assert a[-16:].decode() != b[-8:].hex().upper()  # coverage bytes differ, so must the MAC
    assert mac.unpack_and_verify(b, F, "binary", spec, _algo(spec))["mac"]["ok"] is True


def test_truncated_mac_and_cmac_fit_the_64_bit_field():
    s4 = _spec(length=4)
    w = mac.pack_with_mac("0200", VALUES, F, "binary", s4, _algo(s4))
    assert w[-4:] == b"\x00" * 4  # 4 MAC bytes, zero-filled to the 8-byte field
    assert mac.unpack_and_verify(w, F, "binary", s4, _algo(s4))["mac"]["ok"] is True
    cm = mac.resolve_mac_spec({"field": 128, "algorithm": "aes_cmac", "key": CMAC_KEY})
    w = mac.pack_with_mac("0400", {"11": "000002", "90": "0" * 42}, F, "binary", cm, _algo(cm))
    assert mac.unpack_and_verify(w, F, "binary", cm, _algo(cm))["mac"]["ok"] is True
    with pytest.raises(mac.MacSpecError, match="length"):
        mac.resolve_mac_spec({"field": 128, "algorithm": "aes_cmac", "length": 16, "key": CMAC_KEY})


# -- the contract -------------------------------------------------------------------


def test_spec_validation_refuses_nonsense_and_unresolved_keys():
    assert mac.resolve_mac_spec(None) is None and mac.resolve_mac_spec({}) is None
    for bad in (
        {"field": 63, "key": MAK},
        {"algorithm": "hmac", "key": MAK},
        {"on_failure": "ignore", "key": MAK},
        {"field": 64},
    ):
        with pytest.raises(mac.MacSpecError):
            mac.resolve_mac_spec(bad)
    with pytest.raises(ValueError, match="unresolved"):
        iso8583.mac_spec_from_config({"mac": {"field": 64, "key": "${key.SWITCH_MAK}"}})


def test_mac_field_must_be_last_on_the_wire():
    spec = _spec()
    with pytest.raises(mac.MacSpecError, match="last field"):
        mac.pack_with_mac("0400", {"11": "000001", "90": "0" * 42}, F, "ascii", spec, _algo(spec))


def test_tamper_wrong_key_and_absence_are_reported_not_raised():
    spec = _spec()
    wire = mac.pack_with_mac("0200", VALUES, F, "ascii", spec, _algo(spec))
    tampered = bytearray(wire)
    tampered[25] ^= 0x01  # inside the PAN
    v = mac.unpack_and_verify(bytes(tampered), F, "ascii", spec, _algo(spec))["mac"]
    assert v == {"present": True, "ok": False, "field": 64, "error": "DE 64: MAC mismatch"}
    other = _spec(key="FEDCBA98765432100123456789ABCDEF")
    assert mac.unpack_and_verify(wire, F, "ascii", other, _algo(other))["mac"]["ok"] is False
    plain = shared.pack("0200", VALUES, F, "ascii")
    v = mac.unpack_and_verify(plain, F, "ascii", spec, _algo(spec))["mac"]
    assert v["present"] is False and v["ok"] is None and "no MAC present" in v["error"]


# -- the live path -----------------------------------------------------------------


async def _sim(**cfg):
    r = TcpResponder(
        {
            "protocol": "iso8583",
            "fields": F,
            "framing": {"length_prefix_bytes": 2},
            "default": {"echo": ["11"], "set": {"39": "00"}},
            **cfg,
        }
    )
    port = await r.start()
    return r, port


async def _client(port, **cfg):
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "fields": F,
            "framing": {"length_prefix_bytes": 2},
            "sign_on": {"enabled": False},
            "response_timeout_sec": 2,
            "reconnect": {"enabled": False},
            **cfg,
        }
    )
    await a.connect()
    return a


@pytest.mark.parametrize("encoding", ["ascii", "binary"])
async def test_adapter_and_simulator_authenticate_each_other(encoding):
    block = {"field": 64, "key": MAK, "on_failure": "reject"}
    r, port = await _sim(mac=block, encoding=encoding)
    a = await _client(port, mac=block, encoding=encoding)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4111111111111111", "4": "000000001000"}}
        )
        assert res.success, res.error
        assert res.response_payload["mac_verified"] is True
        assert res.response_payload["response_code"] == "00"
        assert r.received[-1]["mac"]["ok"] is True and r.invalid == 0
    finally:
        await a.disconnect()
        await r.stop()


async def test_simulator_rejects_unauthenticated_request_with_format_error():
    r, port = await _sim(mac={"field": 64, "key": MAK, "on_failure": "reject"})
    a = await _client(port)  # no MAC on the client
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4111111111111111", "4": "000000001000"}}
        )
        assert res.success  # a reply came back …
        assert res.response_payload["response_code"] == "30"  # … the format-error reply
        assert r.invalid == 1
        assert any("no MAC present" in v for v in r.received[-1]["validation"])
    finally:
        await a.disconnect()
        await r.stop()


async def test_simulator_warn_mode_records_a_bad_mac_but_still_answers():
    r, port = await _sim(mac={"field": 64, "key": MAK, "on_failure": "warn"})
    a = await _client(port, mac={"field": 64, "key": "FEDCBA98765432100123456789ABCDEF"})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4111111111111111", "4": "000000001000"}}
        )
        assert res.response_payload["response_code"] == "00"
        assert r.received[-1]["validation"] == ["DE 64: MAC mismatch"]
        assert r.invalid == 1
        # the client, verifying the reply under ITS wrong key, sees the mismatch too
        assert res.response_payload["mac_verified"] is False
        assert res.success  # warn on the client side as well: reported, not failed
    finally:
        await a.disconnect()
        await r.stop()


async def test_adapter_reject_fails_the_step_when_the_host_reply_is_unauthenticated():
    r, port = await _sim()  # host never MACs
    a = await _client(port, mac={"field": 64, "key": MAK, "on_failure": "reject"})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4111111111111111", "4": "000000001000"}}
        )
        assert not res.success
        assert "no MAC present" in (res.error or "")
        assert res.response_payload["response_code"] == "00"  # the reply is still visible
    finally:
        await a.disconnect()
        await r.stop()


async def test_de128_cmac_on_a_reversal_over_binary_wire():
    block = {"field": 128, "algorithm": "aes_cmac", "key": CMAC_KEY, "on_failure": "reject"}
    r, port = await _sim(
        mac=block, encoding="binary", default={"echo": ["11", "90"], "set": {"39": "00"}}
    )
    a = await _client(port, mac=block, encoding="binary")
    try:
        res = await a.execute("send_0400", {"values": {"4": "000000001000", "90": "0" * 42}})
        assert res.success, res.error
        assert (
            res.response_payload["mti"] == "0410" and res.response_payload["mac_verified"] is True
        )
        assert r.received[-1]["de"]["90"] == "0" * 42
    finally:
        await a.disconnect()
        await r.stop()
