"""The one length-prefix encode/decode every TCP piece shares (adapter,
responder, proxy, chaos): binary integers and zero-padded ASCII digits."""

import pytest

from worker.adapters.tcp import framing
from worker.adapters.tcp.chaos import ChaosEngine


@pytest.mark.parametrize(
    "value,width,order,enc,expected",
    [
        (43, 2, "big", "binary", b"\x00\x2b"),
        (43, 2, "little", "binary", b"\x2b\x00"),
        (43, 4, "big", "ascii", b"0043"),
        (43, 6, "big", "ascii", b"000043"),
        (0, 2, "big", "ascii", b"00"),
    ],
)
def test_encode_and_decode_round_trip(value, width, order, enc, expected):
    prefix = framing.encode_length(value, width, order, enc)
    assert prefix == expected and len(prefix) == width
    assert framing.decode_length(prefix, order, enc) == value


def test_ascii_byte_order_is_irrelevant():
    assert framing.encode_length(7, 3, "little", "ascii") == b"007"
    assert framing.decode_length(b"007", "little", "ascii") == 7


def test_capacity_and_overflow():
    assert framing.length_capacity(2, "binary") == 65535
    assert framing.length_capacity(2, "ascii") == 99
    assert framing.length_capacity(4, "ascii") == 9999
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(100, 2, "big", "ascii")
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(65536, 2, "big", "binary")


def test_non_digit_ascii_prefix_names_the_mismatch():
    with pytest.raises(ValueError, match="framing.length_encoding"):
        framing.decode_length(b"\x00\x2b", "big", "ascii")


def test_normalise_defaults_to_binary_and_refuses_unknown():
    assert framing.normalise_length_encoding(None) == "binary"
    assert framing.normalise_length_encoding("ASCII") == "ascii"
    with pytest.raises(ValueError):
        framing.normalise_length_encoding("bcd")


def test_chaos_bad_length_keeps_the_ascii_prefix_digits():
    frame = b"0012" + b"0210AUTH00ABCD"
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=4, length_encoding="ascii")
    assert out[4:] == frame[4:]
    assert out[:4].isdigit() and int(out[:4]) > 12  # still digits, still inflated


def test_chaos_bad_length_near_capacity_still_inflates():
    frame = b"95" + b"x" * 95
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=2, length_encoding="ascii")
    assert out[:2] == b"99"
