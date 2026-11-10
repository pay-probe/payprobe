"""A short value in a fixed-length field is filled the ISO 8583 way.

Before 2026-11-10 the codec wrote it short: every later field shifted and the
host received a garbled message (only the dry run's check caught it). Now ``n``
is zero-filled on the left and the text classes are space-filled on the right,
in the shared codec, so the client, the responder, the proxy and the NATS codec
all send the declared width. Values that break their class are not padded, so
they are still refused with the real reason.
"""

from __future__ import annotations

import pytest

from payprobe_common import iso8583
from payprobe_common.iso8583 import pad_fixed
from worker.adapters.tcp.protocols import Iso8583Protocol

N12 = {"len_type": "fixed", "length": 12, "type": "n"}
ANS15 = {"len_type": "fixed", "length": 15, "type": "ans"}
AN8 = {"len_type": "fixed", "length": 8, "type": "an"}
B16 = {"len_type": "fixed", "length": 16, "type": "b"}
LLVAR = {"len_type": "llvar", "length": 19, "type": "n"}


@pytest.mark.parametrize(
    ("value", "spec", "expected"),
    [
        ("10000", N12, "000000010000"),  # amount: zeros on the left
        ("MERCH01", ANS15, "MERCH01        "),  # merchant id: spaces on the right
        ("SHOP1", ANS15, "SHOP1          "),  # DE 42: 5 chars + 10 spaces = 15
        ("TERM01", AN8, "TERM01  "),
        ("000000010000", N12, "000000010000"),  # already the right width
        ("0000000002A", N12, "0000000002A"),  # breaks n: left for the check to refuse
        ("1234567890123", N12, "1234567890123"),  # too long: refused, never cut
        ("", N12, ""),  # empty: not silently a zero amount
        ("ABCD", B16, "ABCD"),  # binary has no safe fill
        ("4111", LLVAR, "4111"),  # variable fields carry their own length
    ],
)
def test_pad_fixed(value, spec, expected):
    assert pad_fixed(value, spec) == expected


def test_pack_sends_the_declared_width():
    fields = {"4": N12, "42": ANS15}
    wire = iso8583.pack("0200", {"4": "10000", "42": "MERCH01"}, fields, "ascii")
    out = iso8583.unpack(wire, fields, "ascii")
    assert out["fields"]["4"]["value"] == "000000010000"
    assert out["fields"]["42"]["value"] == "MERCH01        "


def test_check_and_correlation_use_the_padded_value():
    """The client pads before it reads the correlation key, so a STAN typed as
    "102" matches the host's "000102" echo, and the dry-run check no longer
    reports a short field it would send correctly."""
    proto = Iso8583Protocol(
        {
            "fields": {
                "4": N12,
                "11": {"len_type": "fixed", "length": 6, "type": "n"},
                "42": ANS15,
            },
            "auto_fields": False,
        }
    )
    payload = {"mti": "0200", "values": {"4": "10000", "11": "102", "42": "MERCH01"}}
    assert proto.check("send_message", payload) == []
    msg = proto.encode("send_message", payload)
    assert msg.request["values"]["11"] == "000102"
    assert msg.request["values"]["42"] == "MERCH01        "
    assert "000102" in msg.key


def test_a_value_that_breaks_its_class_is_still_refused():
    proto = Iso8583Protocol({"fields": {"4": N12}, "auto_fields": False})
    issues = proto.check("send_message", {"mti": "0200", "values": {"4": "0000000002A"}})
    assert issues == ["DE4 expected 12 chars, got 11"]
