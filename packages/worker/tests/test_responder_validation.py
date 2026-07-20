"""Dialect validation: iso_validate codec helper + TcpResponder validate modes."""

from worker.adapters.tcp import iso8583
from worker.adapters.tcp.responder import TcpResponder

FIELDS = iso8583.DEFAULT_FIELDS
PRESENCE = {
    "0200": {"mandatory": ["2", "3", "4", "11"]},
    "default": {"mandatory": ["11"]},
}

# A well-formed 0200 carrying the mandatory core DEs.
GOOD = {"2": "4111111111111111", "3": "000000", "4": "000000010000", "11": "000001"}


def _pack(values, mti="0200"):
    return iso8583.iso_pack(mti, values, FIELDS)


# -- iso_validate --------------------------------------------------------------


def test_conformant_message_has_no_violations():
    assert iso8583.iso_validate("0200", FIELDS, GOOD, presence=PRESENCE) == []


def test_missing_mandatory_de_flagged():
    values = {k: v for k, v in GOOD.items() if k != "4"}
    issues = iso8583.iso_validate("0200", FIELDS, values, presence=PRESENCE)
    assert any("DE 4" in i and "mandatory" in i for i in issues)


def test_unknown_de_flagged_via_de_list():
    # DE 99 is in the bitmap but not in the format table.
    issues = iso8583.iso_validate("0200", FIELDS, GOOD, de_list=[2, 3, 4, 11, 99])
    assert any("DE 99" in i and "not defined" in i for i in issues)


def test_fixed_length_mismatch_flagged():
    bad = {**GOOD, "11": "12"}  # STAN must be 6 long
    issues = iso8583.iso_validate("0200", FIELDS, bad, presence=PRESENCE)
    assert any("DE 11" in i and "length" in i for i in issues)


def test_charset_flagged_when_type_present():
    fields = {**FIELDS, "4": {**FIELDS["4"], "type": "n"}}
    bad = {**GOOD, "4": "0000000ABCDE"}  # numeric field with letters
    issues = iso8583.iso_validate("0200", fields, bad, presence=PRESENCE)
    assert any("DE 4" in i and "type" in i for i in issues)


# -- responder integration -----------------------------------------------------


def _responder(mode):
    return TcpResponder(
        {
            "protocol": "iso8583",
            "validate": {"mode": mode, "presence": PRESENCE, "on_error": {"39": "30"}},
        }
    )


def test_warn_mode_records_but_still_resolves():
    r = _responder("warn")
    parsed = r._decode(_pack({k: v for k, v in GOOD.items() if k != "4"}).encode())
    violations = r._validate(parsed)
    assert violations  # DE 4 missing
    # warn never short-circuits: the normal default action is used
    assert r.validate_mode == "warn"


def test_reject_mode_emits_format_error_action():
    r = _responder("reject")
    parsed = r._decode(_pack({k: v for k, v in GOOD.items() if k != "4"}).encode())
    assert r._validate(parsed)
    action = r._format_error_action(parsed)
    assert action["set"]["39"] == "30"


def test_off_mode_skips_validation():
    r = _responder("off")
    parsed = r._decode(_pack({"11": "000001"}).encode())  # missing 2/3/4
    assert r._validate(parsed) == []


def test_conformant_request_passes_in_reject_mode():
    r = _responder("reject")
    parsed = r._decode(_pack(GOOD).encode())
    assert r._validate(parsed) == []
