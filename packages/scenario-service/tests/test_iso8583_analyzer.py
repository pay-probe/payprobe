"""ISO 8583 analyzer: bitmap, per-DE validation, EMV TLV, validating build."""
import pytest

from models.iso8583_analyzer import (
    DEFAULT_FIELDS, analyze_message, build_message, build_tlv, decode_mti,
    diff_messages, interpret_de, parse_tlv, resolve_encoding, validate_field,
    validate_values,
)


def test_decode_mti_breakdown():
    info = decode_mti("0210")
    assert info["version"] == "ISO 8583:1987"
    assert info["message_class"] == "Financial"
    assert info["function"] == "Request response"
    assert info["origin"] == "Acquirer"
    assert decode_mti("oops") is None


def test_interpret_common_data_elements():
    assert interpret_de(39, "00") == "Approved"
    assert interpret_de(39, "51") == "Insufficient funds"
    assert interpret_de(3, "000000") == "Purchase (goods/services)"
    assert interpret_de(22, "051") == "Chip (ICC)"
    assert interpret_de(49, "840") == "USD"
    assert interpret_de(4, "000000010000").startswith("100.00")
    assert interpret_de(2, "4111111111111111") == "411111******1111"  # masked PAN


def test_analyze_includes_mti_info_and_interpretations():
    msg = build_message("0210", {"2": "4111111111111111", "4": "000000010000",
                                 "39": "00", "49": "840"}, DEFAULT_FIELDS)["message"]
    out = analyze_message(msg, DEFAULT_FIELDS)
    assert out["mti_info"]["message_class"] == "Financial"
    by_de = {r["de"]: r for r in out["fields"]}
    assert by_de[39]["interpretation"] == "Approved"
    assert by_de[49]["interpretation"] == "USD"
    assert "411111" in by_de[2]["interpretation"]


def test_build_then_analyze_round_trip():
    values = {"2": "4111111111111111", "3": "000000", "4": "000000010000",
              "11": "000123", "39": "00", "41": "TERM0001"}
    built = build_message("0210", values, DEFAULT_FIELDS)
    assert built["errors"] == []
    out = analyze_message(built["message"], DEFAULT_FIELDS)
    assert out["mti"] == "0210"
    assert set(out["bitmap"]["present"]) == {2, 3, 4, 11, 39, 41}
    by_de = {r["de"]: r for r in out["fields"]}
    assert by_de[2]["value"] == "4111111111111111"
    assert by_de[39]["value"] == "00"
    assert all("error" not in r for r in out["fields"])


def test_secondary_bitmap_present_flag():
    # DE 70 is < 64 but include a >64 DE to force a secondary bitmap
    values = {"4": "000000010000", "70": "001", "100": "12345678901"}
    fields = {**DEFAULT_FIELDS,
              "100": {"name": "Receiving Inst ID", "len_type": "llvar",
                      "length": 11, "type": "n"}}
    built = build_message("0800", values, fields)
    assert built["errors"] == []
    out = analyze_message(built["message"], fields)
    assert out["bitmap"]["secondary"] is not None
    assert 100 in out["bitmap"]["present"]


def test_validation_flags_bad_values():
    assert validate_field("12AB", {"len_type": "fixed", "length": 4, "type": "n"}) == "must be numeric"
    assert validate_field("12", {"len_type": "fixed", "length": 4, "type": "n"}).startswith("expected 4")
    assert validate_field("ZZ", {"len_type": "fixed", "length": 2, "type": "b"}) == "must be hexadecimal"
    assert validate_field("0000", {"len_type": "fixed", "length": 4, "type": "n"}) is None


def test_build_rejects_invalid_values():
    out = build_message("0200", {"4": "12AB"}, DEFAULT_FIELDS)  # non-numeric amount
    assert out["message"] is None
    assert any("DE4" in e for e in out["errors"])
    # unknown DE + bad MTI
    out2 = build_message("02", {"999": "x"}, DEFAULT_FIELDS)
    assert "MTI must be exactly 4 digits" in out2["errors"]
    assert any("DE999" in e for e in out2["errors"])


def test_analyze_flags_unknown_de_without_crashing():
    fields = {"2": DEFAULT_FIELDS["2"]}  # table missing DE3/DE4 etc.
    msg = "0200" + "F000000000000000" + "16" + "4111111111111111"  # DE2 only set...
    # craft a message whose bitmap claims DE1.. we just ensure no crash + error surfaced
    out = analyze_message("0200" + "7000000000000000" + "06" + "000000", fields)
    assert out["errors"]  # DE2 absent value / DE3 not in table -> reported


def test_emv_tlv_parsing_de55():
    # 9F26 (ARQC, 8 bytes) + 9F36 (ATC, 2 bytes) + 95 (TVR, 5 bytes)
    de55 = "9F2608" + "1122334455667788" + "9F3602" + "001C" + "9505" + "0000000000"
    tlv = parse_tlv(de55)
    tags = {n["tag"]: n for n in tlv}
    assert tags["9F26"]["value"] == "1122334455667788"
    assert tags["9F26"]["name"].startswith("Application Cryptogram")
    assert tags["9F36"]["value"] == "001C"
    assert tags["95"]["length"] == 5


def test_analyze_decodes_de55_tlv_inline():
    de55_hex = "9F2608" + "1122334455667788" + "9F3602" + "001C"
    values = {"4": "000000010000", "55": de55_hex}
    built = build_message("0200", values, DEFAULT_FIELDS)
    out = analyze_message(built["message"], DEFAULT_FIELDS)
    de55 = next(r for r in out["fields"] if r["de"] == 55)
    assert "tlv" in de55
    assert any(n["tag"] == "9F26" for n in de55["tlv"])


def test_build_tlv_round_trips_with_parse():
    nodes = [
        {"tag": "9F26", "value": "1122334455667788"},
        {"tag": "9F36", "value": "001C"},
        {"tag": "95", "value": "0000000000"},
    ]
    hexstr = build_tlv(nodes)
    parsed = parse_tlv(hexstr)
    assert [n["tag"] for n in parsed] == ["9F26", "9F36", "95"]
    assert next(n for n in parsed if n["tag"] == "9F26")["value"] == "1122334455667788"


def test_build_tlv_constructed_and_long_length():
    # constructed template (tag 77) wrapping a child; child value > 127 bytes
    big = "AB" * 200  # 200-byte value -> long-form length
    nodes = [{"tag": "77", "children": [{"tag": "9F10", "value": big}]}]
    hexstr = build_tlv(nodes)
    parsed = parse_tlv(hexstr)
    assert parsed[0]["tag"] == "77" and "children" in parsed[0]
    child = parsed[0]["children"][0]
    assert child["tag"] == "9F10" and child["length"] == 200


_ENCODINGS = [
    "ascii",
    "binary",
    {"bitmap": "binary", "numeric": "bcd", "length": "bcd"},
    {"numeric": "bcd", "binary": "raw", "length": "bcd"},   # ASCII bitmap, BCD body
    {"text": "ebcdic"},                                      # EBCDIC alpha fields
    {"bitmap": "binary"},                                    # binary bitmap only
]


@pytest.mark.parametrize("encoding", _ENCODINGS)
def test_build_then_analyze_round_trip_across_encodings(encoding):
    values = {"2": "4111111111111111", "3": "000000", "4": "000000010000",
              "11": "000123", "37": "RRN000000001", "39": "00",
              "41": "TERM0001", "49": "840", "52": "0123456789ABCDEF"}
    built = build_message("0210", values, DEFAULT_FIELDS, encoding=encoding)
    assert built["errors"] == [], encoding
    out = analyze_message(built["message"], DEFAULT_FIELDS, encoding=encoding)
    assert out["mti"] == "0210"
    assert set(out["bitmap"]["present"]) == {int(d) for d in values}
    by_de = {r["de"]: r for r in out["fields"]}
    for de, expected in values.items():
        assert by_de[int(de)]["value"] == expected, (encoding, de)
    assert all("error" not in r for r in out["fields"]), encoding


def test_binary_message_is_distinct_from_ascii_and_shorter_on_the_wire():
    values = {"3": "000000", "4": "000000010000", "11": "000123", "49": "840"}
    ascii_msg = build_message("0200", values, DEFAULT_FIELDS)["message"]
    bin_msg = build_message("0200", values, DEFAULT_FIELDS, encoding="binary")["message"]
    assert bin_msg != ascii_msg
    # BCD packs two digits per byte → fewer wire bytes than the ASCII form
    assert len(bytes.fromhex(bin_msg)) < len(ascii_msg)


def test_binary_secondary_bitmap_round_trips():
    fields = {**DEFAULT_FIELDS,
              "100": {"name": "Receiving Inst ID", "len_type": "llvar",
                      "length": 11, "type": "n"}}
    values = {"4": "000000010000", "70": "001", "100": "12345678901"}
    built = build_message("0800", values, fields, encoding="binary")
    out = analyze_message(built["message"], fields, encoding="binary")
    assert out["bitmap"]["secondary"] is not None
    assert 100 in out["bitmap"]["present"]
    assert {r["de"]: r["value"] for r in out["fields"]}[100] == "12345678901"


def test_binary_de55_tlv_still_parses():
    de55 = "9F2608" + "1122334455667788" + "9F3602" + "001C"
    built = build_message("0200", {"4": "000000010000", "55": de55},
                          DEFAULT_FIELDS, encoding="binary")
    out = analyze_message(built["message"], DEFAULT_FIELDS, encoding="binary")
    de55_row = next(r for r in out["fields"] if r["de"] == 55)
    assert de55_row["value"] == de55
    assert any(n["tag"] == "9F26" for n in de55_row["tlv"])


def test_resolve_encoding_profiles():
    assert resolve_encoding(None)["numeric"] == "ascii"
    assert resolve_encoding("binary")["bitmap"] == "binary"
    # a partial dict overrides only the named axis
    opts = resolve_encoding({"text": "ebcdic"})
    assert opts["text"] == "ebcdic" and opts["numeric"] == "ascii"


def test_validate_field_data_element_classes():
    assert validate_field("ABCD", {"len_type": "fixed", "length": 4, "type": "a"}) is None
    assert validate_field("AB12", {"len_type": "fixed", "length": 4, "type": "a"}) == "must be alphabetic"
    assert validate_field("AB12", {"len_type": "fixed", "length": 4, "type": "an"}) is None
    assert validate_field("AB 2", {"len_type": "fixed", "length": 4, "type": "an"}) == "must be alphanumeric"
    assert validate_field("A B-2", {"len_type": "fixed", "length": 5, "type": "ans"}) is None
    # binary must be whole bytes (even number of hex digits)
    assert validate_field("ABC", {"len_type": "lllvar", "length": 999, "type": "b"}) == \
        "binary field must have an even number of hex digits"
    # track-2 data permits the '=' separator and 'D'
    assert validate_field("4111111111111111D2512", {"len_type": "llvar", "length": 37, "type": "z"}) is None


def test_diff_detects_added_removed_changed_same():
    a = build_message("0200", {"2": "4111111111111111", "4": "000000010000",
                               "11": "000001", "37": "RRN000000001"}, DEFAULT_FIELDS)["message"]
    b = build_message("0210", {"2": "4111111111111111", "4": "000000010000",
                               "11": "000002", "39": "00"}, DEFAULT_FIELDS)["message"]
    d = diff_messages(a, b, DEFAULT_FIELDS)
    assert d["mti_changed"] is True
    by_de = {r["de"]: r for r in d["fields"]}
    assert by_de[2]["change"] == "same"        # PAN unchanged
    assert by_de[11]["change"] == "changed"    # STAN changed
    assert by_de[37]["change"] == "removed"    # RRN only in A
    assert by_de[39]["change"] == "added"      # response code only in B
    assert d["summary"]["changed"] >= 1 and d["summary"]["added"] >= 1
