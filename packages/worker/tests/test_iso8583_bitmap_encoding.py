"""``bitmap_encoding`` on a Message Format: a raw-binary bitmap under an
otherwise-ASCII dialect (a POS host). MTI, fixed fields and
LL/LLLVAR length indicators stay ASCII; only the bitmap bytes change."""

from __future__ import annotations

import pytest

from payprobe_common import iso8583 as shared

#: The 1987 table plus DE 24 (NII), which the POS dialect carries.
FIELDS = {
    **shared.ISO8583_1987,
    "24": {
        "name": "Network International Identifier",
        "len_type": "fixed",
        "length": 3,
        "type": "n",
    },
}
VALUES = {
    "2": "4111111111111111",
    "3": "000000",
    "4": "000000010000",
    "7": "1106175000",
    "11": "000123",
    "12": "175000",
    "22": "051",
    "24": "001",
    "25": "00",
    "35": "4111111111111111=29121011234567890000",
    "41": "TERM0001",
    "42": "MERCHANT0000001",
    "48": "POSDATA",
    "49": "981",
}
POS_DIALECT = {"encoding": "ascii", "bitmap_encoding": "binary", "fields": FIELDS}
EXPECTED_BITMAP = bytes.fromhex("72 30 05 80 20 C1 80 00".replace(" ", ""))

#: The whole 0200, recorded from the codec on 2026-11-06 and checked by hand:
#: "0200" · 8 binary bitmap bytes · "16" + PAN · DE 3 … DE 49, all ASCII.
EXPECTED_HEX = (
    "30323030"  # MTI 0200
    "7230058020c18000"  # primary bitmap, raw binary
    "3136"
    "34313131313131313131313131313131"  # DE 2: LL "16" + PAN
    "303030303030"  # DE 3
    "303030303030303130303030"  # DE 4
    "31313036313735303030"  # DE 7
    "303030313233"  # DE 11
    "313735303030"  # DE 12
    "303531"  # DE 22
    "303031"  # DE 24
    "3030"  # DE 25
    "3337"
    "343131313131313131313131313131313d3239313231303131323334353637383930303030"  # DE 35
    "5445524d30303031"  # DE 41
    "4d45524348414e5430303030303031"  # DE 42
    "303037"
    "504f5344415441"  # DE 48: LLL "007" + value
    "393831"  # DE 49
)


def test_definition_encoding_folds_bitmap_encoding_over_the_profile():
    assert shared.definition_encoding(POS_DIALECT) == {
        "bitmap": "binary",
        "numeric": "ascii",
        "text": "ascii",
        "binary": "hex",
        "length": "ascii",
    }
    # absent -> the definition's encoding as written (None when there is none)
    assert shared.definition_encoding({"encoding": "binary"}) == "binary"
    assert shared.definition_encoding({}) is None
    # the shorthand overrides the bitmap axis of an explicit profile / dict too
    assert (
        shared.definition_encoding({"encoding": "binary", "bitmap_encoding": "hex"})["bitmap"]
        == "hex"
    )
    assert shared.definition_encoding(
        {"encoding": {"numeric": "bcd"}, "bitmap_encoding": "BINARY"}
    ) == {
        **shared.ASCII_OPTS,
        "numeric": "bcd",
        "bitmap": "binary",
    }
    with pytest.raises(ValueError, match="bitmap_encoding cannot be 'raw'"):
        shared.definition_encoding({"bitmap_encoding": "raw"})


def test_pos_0200_bitmap_is_raw_binary_while_everything_else_stays_ascii():
    wire = shared.pack("0200", VALUES, FIELDS, shared.definition_encoding(POS_DIALECT))
    assert wire[:4] == b"0200"  # MTI ASCII
    assert wire[4:12] == EXPECTED_BITMAP  # 72 30 05 80 20 C1 80 00
    assert wire[12:14] == b"16"  # DE 2 LLVAR length indicator ASCII
    assert wire[14:30] == b"4111111111111111"
    assert b"007POSDATA" in wire  # DE 48 LLLVAR length indicator ASCII
    assert wire.hex() == EXPECTED_HEX and len(wire) == 153
    # the same field set under the plain-ASCII profile differs only in the bitmap
    ascii_wire = shared.pack("0200", VALUES, FIELDS, "ascii")
    assert ascii_wire[4:20] == b"7230058020C18000"
    assert ascii_wire[:4] + ascii_wire[20:] == wire[:4] + wire[12:]


def test_pos_0200_round_trips_and_a_secondary_bitmap_is_binary_too():
    enc = shared.definition_encoding(POS_DIALECT)
    parsed = shared.unpack(shared.pack("0200", VALUES, FIELDS, enc), FIELDS, enc)
    assert parsed["mti"] == "0200"
    assert parsed["de_list"] == [2, 3, 4, 7, 11, 12, 22, 24, 25, 35, 41, 42, 48, 49]
    assert {k: v["value"] for k, v in parsed["fields"].items()} == VALUES
    # DE 70 forces a secondary bitmap: 16 raw bytes, bit 1 set in the primary
    wire = shared.pack("0800", {"11": "000001", "70": "301"}, FIELDS, enc)
    assert wire[:4] == b"0800"
    assert wire[4:12] == bytes.fromhex("8020000000000000")
    assert wire[12:20] == bytes.fromhex("0400000000000000")  # DE 70 = bit 6 of byte 65–72
    assert wire[20:] == b"000001301"


def test_track2_sentinels_and_space_padded_an_fields_validate():
    assert shared.charset_error(";5488350005706204=2912101?", "z") is None
    assert shared.charset_error("5488350005706204=2912101", "z") is None
    assert shared.charset_error("5488;350005706204", "z")  # a sentinel only at the ends
    assert shared.charset_error("MERCHANT01     ", "an") is None
    assert shared.charset_error("MERCH ANT", "an")  # padding, not embedded spaces
    assert shared.charset_error("ABC   ", "a") is None
    # the sentinels are wire bytes: counted by the LLVAR indicator
    spec = {"len_type": "llvar", "length": 37, "type": "z"}
    wire = shared.pack("0200", {"35": ";5488350005706204=2912101?"}, {"35": spec}, "ascii")
    assert wire[20:] == b"26;5488350005706204=2912101?"


def test_resolve_encoding_accepts_the_bitmap_encoding_spelling():
    assert shared.resolve_encoding({"bitmap_encoding": "binary"})["bitmap"] == "binary"
    assert shared.resolve_encoding({"bitmap_encoding": "binary"})["numeric"] == "ascii"
