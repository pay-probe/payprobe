"""ADR-0011 phase 0/1 contract: the shared ISO 8583 codec reproduces, byte for
byte, what the pre-ADR worker codec (ASCII) and analyzer codec (binary profiles)
produced, and every consumer shares one field dictionary.

``fixtures/iso8583_golden.json`` was recorded on 2026-09-24 from the code as it
stood before the extraction; regenerate it only from a checkout that predates
ADR-0011, never from the codec under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from payprobe_common import iso8583 as shared
from worker.adapters.scheme import visa
from worker.adapters.tcp import iso8583

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "iso8583_golden.json").read_text())
CASES = [(c, e) for c in GOLDEN["cases"] for e in GOLDEN["encodings"]]
IDS = [f"{c['name']}-{e}" for c, e in CASES]


@pytest.mark.parametrize("case,ename", CASES, ids=IDS)
def test_pack_matches_recorded_wire_bytes(case, ename):
    fields = shared.BUILTIN_TABLES[case["format"]]
    enc = GOLDEN["encodings"][ename]
    assert (
        shared.pack(case["mti"], case["values"], fields, enc).hex().upper() == case["wire"][ename]
    )


@pytest.mark.parametrize("case,ename", CASES, ids=IDS)
def test_unpack_round_trips_recorded_wire_bytes(case, ename):
    fields = shared.BUILTIN_TABLES[case["format"]]
    enc = GOLDEN["encodings"][ename]
    out = shared.unpack(bytes.fromhex(case["wire"][ename]), fields, enc)
    assert out["mti"] == case["mti"]
    assert out["de_list"] == sorted(int(d) for d in case["values"])
    assert {k: v["value"] for k, v in out["fields"].items()} == case["values"]
    assert "truncated" not in out and out["trailing"] is None


def test_worker_str_helpers_are_the_shared_codec_under_ascii():
    # ``iso_unpack(str)`` keeps its historical strip-all-whitespace convenience, so
    # pick a golden case whose text fields carry no spaces (the wire path uses
    # the bytes codec and is covered for every case above).
    case = next(c for c in GOLDEN["cases"] if not any(" " in v for v in c["values"].values()))
    fields = shared.BUILTIN_TABLES[case["format"]]
    text = iso8583.iso_pack(case["mti"], case["values"], fields)
    assert text.encode("ascii").hex().upper() == case["wire"]["ascii"]
    parsed = iso8583.iso_unpack(text, fields)
    assert {k: v["value"] for k, v in parsed["fields"].items()} == case["values"]


def test_one_field_dictionary_everywhere():
    """The drift ADR-0011 measured (22 vs 29 vs 50 DEs, no ``type`` on the worker
    copy) cannot recur: the consumers hold the very same objects."""
    assert iso8583.DEFAULT_FIELDS is shared.ISO8583_1987
    assert visa.VISA_FIELDS is shared.VISA_BASE_I
    assert len(shared.ISO8583_1987) == 50
    assert all("type" in spec for spec in shared.ISO8583_1987.values())
    assert {"5", "6", "15", "19", "43", "53", "64"} <= set(shared.ISO8583_1987)


def test_unknown_de_is_reported_not_silently_truncated():
    wire = shared.pack("0200", {"4": "000000000100", "11": "000001"}, shared.ISO8583_1987)
    out = shared.unpack(wire, {"4": shared.ISO8583_1987["4"]})
    assert out["truncated"] is True
    assert out["error"].startswith("DE 11: not in field table")
    assert out["fields"]["4"]["value"] == "000000000100"
    assert out["fields"]["11"]["error"] == "DE not in spec"


def test_structurally_broken_message_raises_decode_error():
    with pytest.raises(shared.Iso8583DecodeError):
        shared.unpack(b"0200", shared.ISO8583_1987)
    with pytest.raises(shared.Iso8583DecodeError):
        # llvar indicator is not digits
        shared.unpack(b"0200" + b"4000000000000000" + b"XX411", shared.ISO8583_1987)
    with pytest.raises(ValueError):
        iso8583.iso_unpack("0200", shared.ISO8583_1987)


def test_per_field_override_pan_right_padded_and_track2_as_bcd():
    fields = dict(shared.ISO8583_1987)
    fields["2"] = {**fields["2"], "encoding": {"numeric": "bcd", "pad": "right"}}
    fields["35"] = {**fields["35"], "encoding": {"numeric": "bcd", "pad": "right"}}
    fields["55"] = {**fields["55"], "encoding": {"binary": "raw", "length": "binary"}}
    values = {
        "2": "411111111111111",  # 15 digits -> F nibble on the right
        "35": "4111111111111111=2712101",  # '=' -> D nibble
        "55": "9F2608AABBCCDDEEFF0011",
    }
    wire = shared.pack("0200", values, fields, "ascii")
    body = wire[4 + 16 :]
    assert body[:2] == b"15" and body[2:10].hex().upper() == "411111111111111F"
    assert body[10:12] == b"24" and body[12:24].hex().upper() == "4111111111111111D2712101"
    # DE 55: 2-byte binary LLL indicator (11 bytes) then raw TLV bytes
    assert body[24:26] == (11).to_bytes(2, "big") and body[26:] == bytes.fromhex(values["55"])
    back = {k: v["value"] for k, v in shared.unpack(wire, fields, "ascii")["fields"].items()}
    assert back == {**values, "35": "4111111111111111D2712101"}


def test_profile_typo_and_bad_axis_are_refused():
    with pytest.raises(ValueError):
        shared.resolve_encoding("bnary")
    with pytest.raises(ValueError):
        shared.resolve_encoding({"numeric": "ebcdc"})
    # foreign keys are ignored, valid axes honoured
    assert shared.resolve_encoding({"text": "ebcdic", "pad": "left"})["text"] == "ebcdic"


def test_wire_encoding_precedence_folds_legacy_key_and_refuses_disagreement():
    assert shared.wire_encoding_from_config({}) == "ascii"
    assert shared.wire_encoding_from_config({"framing": {"encoding": "ascii"}}) == "ascii"
    assert shared.wire_encoding_from_config({"framing": {"encoding": "cp037"}}) == {
        "text": "ebcdic"
    }
    assert shared.wire_encoding_from_config({"encoding": "binary"}) == "binary"
    # an agreeing pair is fine: the explicit profile is returned
    assert (
        shared.wire_encoding_from_config({"encoding": "ascii", "framing": {"encoding": "utf-8"}})
        == "ascii"
    )
    with pytest.raises(ValueError, match="disagree"):
        shared.wire_encoding_from_config({"encoding": "binary", "framing": {"encoding": "ascii"}})
    with pytest.raises(ValueError, match="not a known"):
        shared.wire_encoding_from_config({"framing": {"encoding": "koi8-r"}})
    # header_echo keeps framing.encoding as a real text codec: never folded
    assert (
        shared.wire_encoding_from_config(
            {"framing": {"encoding": "koi8-r"}}, protocol="header_echo"
        )
        == "ascii"
    )
