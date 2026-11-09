"""Link-level framing options borrowed from terminal test tools' connection
screens: generic presets, compact link-level strings, hex length prefixes, a
mandatory trailer byte and the STX/ETX/LRC envelope. The shared vocabulary is
``payprobe_common.link_framing``; the worker's ``FrameSpec`` executes it and the
responder and adapter read every frame through the one ``read_frame``."""

import asyncio

import pytest

from payprobe_common import link_framing as lf
from worker.adapters.tcp import framing
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.chaos import ChaosEngine
from worker.adapters.tcp.responder import TcpResponder

BODY = b"0200" + bytes.fromhex("2000000000000000") + b"000123"


def _spec(**fr) -> framing.FrameSpec:
    return framing.FrameSpec.from_config({"framing": fr})


async def _read(spec: framing.FrameSpec, data: bytes) -> tuple[bytes, bytes]:
    """Feed ``data`` to a StreamReader (needs a running loop) and read one frame."""
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return await spec.read_frame(r)


# -- compact link-level strings ---------------------------------------------


def test_link_level_ascii4_is_the_pos_terminal_link():
    """``CHARDEC:false:false:BIG:4:`` — 4 ASCII digits counting only the body."""
    keys = lf.parse_link_level("CHARDEC:false:false:BIG:4:")
    assert keys == {
        "mode": "length_prefix",
        "length_prefix_bytes": 4,
        "length_encoding": "ascii",
        "length_byte_order": "big",
        "length_includes_prefix": False,
        "length_includes_header": False,
    }
    spec = _spec(link_level="CHARDEC:false:false:BIG:4:")
    assert spec.frame(BODY) == b"0018" + BODY
    assert spec == _spec(
        length_prefix_bytes=4, length_encoding="ascii", length_includes_header=False
    )


def test_link_level_binary_with_dotted_pad_and_counting_flags():
    # 2-byte binary, then two zero bytes the length does not count
    spec = _spec(link_level="BCH:false:false:BIG:2:00.00")
    assert spec.frame(BODY) == (18).to_bytes(2, "big") + b"\x00\x00" + BODY
    # 4-byte binary counting itself and the 14 pad bytes
    pad = ".".join(["00"] * 14)
    spec = _spec(link_level=f"BCH:true:true:BIG:4:{pad}")
    assert spec.frame(BODY) == (4 + 14 + 18).to_bytes(4, "big") + b"\x00" * 14 + BODY
    # a 5-token string (no pad) and lower-case tokens are fine too
    assert lf.parse_link_level("bcd:false:false:big:2")["length_encoding"] == "bcd"


def test_link_level_refuses_no_length_and_malformed_tokens():
    with pytest.raises(ValueError, match="delimited"):
        lf.parse_link_level("BCH:false:true:BIG:0:")
    with pytest.raises(ValueError, match="six colon-separated"):
        lf.parse_link_level("CHARDEC:4")
    with pytest.raises(ValueError, match="true/false"):
        lf.parse_link_level("CHARDEC:maybe:false:BIG:4:")
    with pytest.raises(ValueError, match="length_encoding"):
        lf.parse_link_level("OCTAL:false:false:BIG:4:")
    with pytest.raises(ValueError, match="byte order"):
        lf.parse_link_level("BCH:false:false:MIDDLE:2:")
    assert lf.parse_link_level("STX-ETX-LRC")["mode"] == "delimited"


# -- presets -----------------------------------------------------------------


async def test_every_preset_builds_a_spec_and_round_trips_its_own_frame():
    for name in lf.FRAMING_PRESETS:
        spec = _spec(preset=name)
        body = b"0800" + b"8220000000000000" + b"000001"  # hex bitmap: no 0x03
        frame = spec.frame(body)
        _env, got = await _read(spec, frame)
        assert got == body, name
        assert spec.preset == name
        assert name in spec.describe()


def test_preset_is_a_bundle_of_defaults_explicit_keys_win():
    out = lf.expand_framing({"preset": "ascii4", "length_prefix_bytes": 6})
    assert out["length_prefix_bytes"] == 6 and out["length_encoding"] == "ascii"
    assert out["preset"] == "ascii4"
    with pytest.raises(ValueError, match="framing.preset must be one of"):
        lf.expand_framing({"preset": "visa"})
    assert _spec(preset="ascii4").frame(BODY) == b"0018" + BODY
    assert _spec(preset="binary2").with_(preset="") == _spec()  # the default framing, named


def test_describe_presets_lists_generic_names_only():
    names = {row["name"] for row in lf.describe_presets()}
    assert {"ascii4", "binary2", "binary2_pad2", "stx_etx_lrc", "ascii4_trailer"} <= names
    for row in lf.describe_presets():
        assert row["description"] and "_desc" not in row["keys"]


# -- hex length prefix --------------------------------------------------------


@pytest.mark.parametrize(
    "value,width,expected", [(43, 4, b"002B"), (255, 2, b"FF"), (0, 3, b"000")]
)
def test_hex_length_prefix_round_trip(value, width, expected):
    assert framing.encode_length(value, width, "big", "hex") == expected
    assert framing.decode_length(expected, "big", "hex") == value
    assert framing.decode_length(expected.lower(), "big", "hex") == value


def test_hex_capacity_and_mismatch():
    assert framing.length_capacity(2, "hex") == 255
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(256, 2, "big", "hex")
    with pytest.raises(ValueError, match="hexadecimal"):
        framing.decode_length(b"00\x2b", "big", "hex")
    assert framing.normalise_length_encoding("CHARHEX") == "hex"


# -- trailer -------------------------------------------------------------------


async def test_trailer_is_appended_expected_and_never_counted():
    spec = _spec(preset="ascii4_trailer")
    frame = spec.frame(BODY)
    assert frame == b"0018" + BODY + b"\x03"
    assert spec.parts(BODY)["trailer_hex"] == "03"
    env, body = await _read(spec, frame)
    assert body == BODY and env == b"0018"
    # the wrong trailer is a framing mismatch, not silently part of the body
    with pytest.raises(ValueError, match="trailer"):
        await _read(spec, b"0018" + BODY + b"\x04")
    # trailer_bytes without trailer_hex: strip, do not check
    loose = _spec(length_prefix_bytes=4, length_encoding="ascii", trailer_bytes=1)
    assert (await _read(loose, b"0018" + BODY + b"\x04"))[1] == BODY


# -- delimited STX / ETX / LRC ------------------------------------------------


def test_stx_etx_lrc_frame_bytes():
    spec = _spec(preset="stx_etx_lrc")
    body = b"0800822000000000000000000001"
    frame = spec.frame(body)
    assert frame[:1] == b"\x02" and frame[1:-2] == body and frame[-2:-1] == b"\x03"
    assert frame[-1] == framing.lrc(body + b"\x03")
    parts = spec.parts(body)
    assert parts["mode"] == "delimited" and parts["checksum"] == "lrc"
    assert spec.describe().startswith("delimited start=02 end=03")


async def test_stx_etx_lrc_reads_skips_noise_and_checks_the_lrc():
    spec = _spec(preset="stx_etx_lrc")
    body = b"0210822000000000000000000001"
    frame = spec.frame(body)
    env, got = await _read(spec, b"\x06\x06" + frame)  # ACKs before STX
    assert got == body and env == b"\x02"
    bad = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(ValueError, match="LRC mismatch"):
        await _read(spec, bad)
    plain = _spec(mode="delimited", checksum="none")
    assert plain.frame(body) == b"\x02" + body + b"\x03"
    assert (await _read(plain, plain.frame(body)))[1] == body


def test_delimited_refuses_a_body_carrying_the_end_byte():
    spec = _spec(preset="stx_etx_lrc")
    with pytest.raises(ValueError, match="end byte"):
        spec.frame(b"0200" + bytes.fromhex("0300000000000000"))  # binary bitmap with 0x03


def test_delimited_mode_rejects_length_keys_and_zero_prefix_points_at_it():
    with pytest.raises(ValueError, match="delimited"):
        _spec(length_prefix_bytes=0)
    with pytest.raises(ValueError, match="framing.mode must be one of"):
        _spec(mode="magic")
    with pytest.raises(ValueError, match="framing.checksum must be one of"):
        _spec(mode="delimited", checksum="crc32")


# -- chaos on delimited frames ---------------------------------------------------


async def test_chaos_on_delimited_frames_keeps_or_breaks_the_envelope_deliberately():
    spec = _spec(preset="stx_etx_lrc")
    body = b"0210822000000000000000000001"
    frame = spec.frame(body)
    chaos = ChaosEngine()
    stalled = chaos.corrupt(frame, "bad_length", frame_spec=spec)
    assert stalled == b"\x02" + body  # no ETX, no LRC: the reader waits forever
    for mode in ("garbage", "flip_bits", "bad_mti"):
        out = chaos.corrupt(frame, mode, frame_spec=spec)
        _env, got = await _read(spec, out)  # envelope + LRC still valid
        assert len(got) == len(body)
        if mode == "bad_mti":
            assert got[:4] == b"9999"


# -- both ends through the responder ----------------------------------------------


async def _round_trip(cfg: dict) -> None:
    responder = TcpResponder(
        {"protocol": "iso8583", "framing": cfg, "default": {"echo": ["11"], "set": {"39": "00"}}}
    )
    port = await responder.start()
    adapter = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": cfg,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 3,
        }
    )
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await responder.stop()


async def test_adapter_and_responder_agree_on_every_length_prefix_preset():
    for name in ("ascii4", "ascii4_trailer", "hex4", "binary2_pad2", "binary4_pad14_inclusive"):
        await _round_trip({"preset": name})


async def test_adapter_and_responder_agree_through_a_link_level_string():
    await _round_trip({"link_level": "CHARDEC:false:false:BIG:4:"})


async def test_adapter_and_responder_agree_on_stx_etx_lrc():
    # the default (hex-bitmap ASCII) dialect never carries 0x03 in the body
    await _round_trip({"preset": "stx_etx_lrc"})
