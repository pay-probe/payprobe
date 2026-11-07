"""A run step that names ``message_format_id`` packs with that registered
dialect — its DE table, presence rules and wire encoding (``bitmap_encoding``)
— not the connection's / stock 1987 table. Acceptance shape: a POS-host 0200 (ASCII text, raw-binary bitmap, track-2 sentinels,
space-padded DE 42) framed with the connection's 4-digit ASCII length."""
import pytest

from orchestrator.api import main as m
from payprobe_common.iso8583 import ISO8583_1987
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.framing import FrameSpec
from worker.adapters.tcp.protocols import make_protocol

PAN = "5488350005706204"
POS_FIELDS = {
    **ISO8583_1987,
    "12": {"name": "Local date & time (YYMMDDhhmmss)", "len_type": "fixed", "length": 12, "type": "n"},
    "24": {"name": "NII", "len_type": "fixed", "length": 3, "type": "n"},  # not in the stock table
    "42": {"name": "Card Acceptor ID Code", "len_type": "fixed", "length": 15, "type": "ans"},
}
POS_FORMAT = {"id": "pos-dialect", "protocol": "iso8583", "definition": {
    "encoding": "ascii", "bitmap_encoding": "binary", "fields": POS_FIELDS,
    "presence": {"0200": {"mandatory": ["2", "3", "4", "11", "41", "49"]}},
}}
VALUES = {
    "2": PAN, "3": "000000", "4": "000000000200", "7": "1106175000", "11": "000005",
    "12": "261106175000", "22": "020", "24": "200", "25": "00",
    "35": f";{PAN}=2912101?", "41": "TERMID01", "42": "MERCHANT01     ",
    "48": "0360010", "49": "981",
}
assert len(VALUES["42"]) == 15

#: The frame the vendor client puts on the wire for VALUES, written out from the
#: dialect by hand (byte 0-3 MTI, 4-11 the raw bitmap, then the DEs in order):
REFERENCE_BODY = (
    b"0200"
    + bytes.fromhex("72 30 05 80 20 C1 80 00".replace(" ", ""))
    + b"16" + PAN.encode()                      # DE 2  LLVAR
    + b"000000"                                  # DE 3
    + b"000000000200"                            # DE 4
    + b"1106175000"                              # DE 7
    + b"000005"                                  # DE 11
    + b"261106175000"                            # DE 12
    + b"020"                                     # DE 22
    + b"200"                                     # DE 24
    + b"00"                                      # DE 25
    + b"26" + f";{PAN}=2912101?".encode()        # DE 35 LLVAR — sentinels counted
    + b"TERMID01"                                # DE 41
    + b"MERCHANT01     "                         # DE 42 — 15 bytes incl. padding
    + b"007" + b"0360010"                        # DE 48 LLLVAR
    + b"981"                                     # DE 49
)
REFERENCE_FRAME = f"{len(REFERENCE_BODY):04d}".encode() + REFERENCE_BODY


def _registry(monkeypatch, formats=None, connections=None):
    formats = formats if formats is not None else {"pos-dialect": POS_FORMAT}
    connections = connections if connections is not None else [{
        "name": "pos_host", "adapter": "tcp", "protocol": "iso8583",
        "host": "172.16.90.11", "port": 52000, "auto_fields": False,
        "framing": {"length_prefix_bytes": 4, "length_byte_order": "big",
                    "length_encoding": "ascii"},
        "sign_on": {"enabled": False}, "health_check": {"mode": "connect"},
    }]

    async def fake_get(url):
        if url.endswith("/connections"):
            return connections
        if "/formats/" in url:
            fid = url.rsplit("/formats/", 1)[1]
            if fid in formats:
                return formats[fid]
            raise RuntimeError(f"404 {url}")
        raise AssertionError(url)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    monkeypatch.setattr(m, "_ISO8583_FORMAT_ENCODING", True)


def _scenario(*payloads, connection="pos_host"):
    return [{"id": "sc", "steps": [
        {"id": f"s{i}", "kind": "action", "target": "tcp_iso8583", "action": "send_message",
         "config": {"connection": connection}, "payload": dict(p)}
        for i, p in enumerate(payloads)]}]


async def _resolve(env, scs):
    await m._attach_connections(env, scs)
    await m._attach_message_formats(env, scs)


async def test_step_with_message_format_id_only_packs_the_registered_dialect(monkeypatch):
    _registry(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    scs = _scenario({"message_format_id": "pos-dialect", "mti": "0200", "values": VALUES})
    await _resolve(env, scs)
    step = scs[0]["steps"][0]
    cfg = env["adapters"]["pos_host"]
    # the dialect reached the step (table + presence) …
    assert step["payload"]["fields"] == POS_FIELDS
    assert step["payload"]["presence"] == POS_FORMAT["definition"]["presence"]
    # … and its wire encoding the socket the step runs on
    assert cfg["encoding"]["bitmap"] == "binary" and cfg["encoding"]["text"] == "ascii"
    # pack exactly as the worker will: the protocol built from the resolved
    # adapter config, the step payload as attached
    proto = make_protocol(cfg)
    assert proto.check(step["action"], step["payload"]) == []
    msg = proto.encode(step["action"], step["payload"])
    body = msg.body
    assert body[:4] == b"0200"
    assert body[4:12] == bytes.fromhex("7230058020C18000")  # bits 2,3,4,7,11,12,22,24,25,35,41,42,48,49
    assert body[12:30] == b"16" + PAN.encode()
    de35 = b"26;" + PAN.encode() + b"=2912101?"
    assert de35 in body  # sentinels kept, counted in the LL prefix
    assert b"MERCHANT01     0070360010981" in body  # DE 42 is 15 bytes incl. padding, then DE 48, 49
    assert body == REFERENCE_BODY
    # the framing layer adds the connection's 4-digit ASCII length prefix
    frame = FrameSpec.from_config(cfg).frame(body)
    assert frame == REFERENCE_FRAME and frame[:4] == b"0148" and len(body) == 148
    # the dry run of the same step agrees byte for byte
    preview = TcpAdapter(cfg).preview_frame(step["action"], step["payload"])
    assert bytes.fromhex(preview["hex"]) == REFERENCE_FRAME


async def test_without_the_binding_the_stock_table_rejects_de24(monkeypatch):
    """The failure this fixes: no format resolution -> stock 1987 table."""
    _registry(monkeypatch)
    cfg = {"host": "h", "port": 1, "protocol": "iso8583", "auto_fields": False}
    proto = make_protocol(cfg)
    issues = proto.check("send_message", {"mti": "0200", "values": VALUES})
    assert "DE24 not defined in the field table" in issues
    assert "DE12 expected 6 chars, got 12" in issues
    with pytest.raises(KeyError, match="DE 24 is not defined"):
        proto.encode("send_message", {"mti": "0200", "values": VALUES})


async def test_presence_rules_and_field_classes_come_from_the_format(monkeypatch):
    _registry(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    short = {k: v for k, v in VALUES.items() if k not in ("41", "49")}
    scs = _scenario({"message_format_id": "pos-dialect", "mti": "0200",
                     "values": {**short, "35": f"{PAN}?x=2912101"}})
    await _resolve(env, scs)
    issues = make_protocol(env["adapters"]["pos_host"]).check("send_message", scs[0]["steps"][0]["payload"])
    assert "DE35 must be track 2/3 data (digits, '=', 'D'; ';' … '?' sentinels allowed)" in issues
    assert "DE41 mandatory for MTI 0200 but missing" in issues
    assert "DE49 mandatory for MTI 0200 but missing" in issues


async def test_inline_fields_on_the_step_win_over_the_format(monkeypatch):
    _registry(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    mine = {"2": POS_FIELDS["2"], "11": POS_FIELDS["11"]}
    scs = _scenario({"message_format_id": "pos-dialect", "mti": "0200", "fields": mine,
                     "values": {"2": PAN, "11": "000005"}})
    await _resolve(env, scs)
    assert scs[0]["steps"][0]["payload"]["fields"] == mine
    assert env["adapters"]["pos_host"]["encoding"]["bitmap"] == "binary"  # the socket still binds


async def test_adapter_with_a_disagreeing_encoding_is_refused(monkeypatch):
    from fastapi import HTTPException

    _registry(monkeypatch, connections=[{
        "name": "pos_host", "adapter": "tcp", "protocol": "iso8583", "host": "h", "port": 1,
        "encoding": "ascii"}])
    with pytest.raises(HTTPException) as exc:
        await _resolve({"mode": "real", "adapters": {}},
                       _scenario({"message_format_id": "pos-dialect", "mti": "0200", "values": {}}))
    assert exc.value.status_code == 400
    assert "step 's0' on adapter 'pos_host'" in exc.value.detail and "keep one" in exc.value.detail


async def test_two_formats_with_different_encodings_on_one_socket_are_refused(monkeypatch):
    from fastapi import HTTPException

    ascii_fmt = {"id": "plain", "protocol": "iso8583",
                 "definition": {"encoding": "ascii", "fields": POS_FIELDS}}
    _registry(monkeypatch, formats={"pos-dialect": POS_FORMAT, "plain": ascii_fmt})
    with pytest.raises(HTTPException) as exc:
        await _resolve({"mode": "real", "adapters": {}},
                       _scenario({"message_format_id": "pos-dialect", "mti": "0200", "values": {}},
                                 {"message_format_id": "plain", "mti": "0200", "values": {}}))
    assert exc.value.status_code == 400 and "different wire encodings" in exc.value.detail


async def test_two_steps_on_the_same_format_share_the_socket_fine(monkeypatch):
    _registry(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    await _resolve(env, _scenario({"message_format_id": "pos-dialect", "mti": "0200", "values": {}},
                                  {"message_format_id": "pos-dialect", "mti": "0800", "values": {}}))
    assert env["adapters"]["pos_host"]["encoding"]["bitmap"] == "binary"


async def test_unknown_format_on_a_step_is_a_404_not_a_silent_fallback(monkeypatch):
    from fastapi import HTTPException

    _registry(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await _resolve({"mode": "real", "adapters": {}},
                       _scenario({"message_format_id": "nope", "mti": "0200", "values": {}}))
    assert exc.value.status_code == 404 and "step 's0'" in exc.value.detail


async def test_steps_without_a_format_are_untouched(monkeypatch):
    _registry(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    scs = _scenario({"mti": "0200", "values": {"2": PAN}})
    await _resolve(env, scs)
    assert "fields" not in scs[0]["steps"][0]["payload"]
    assert "encoding" not in env["adapters"]["pos_host"]
