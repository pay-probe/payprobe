"""Proxy capture buffer — redaction, bounding, and scenario synthesis."""

from worker.adapters.tcp.capture import (
    CaptureBuffer,
    build_scenario_draft,
    mask_pan,
    match_log_rules,
    redact_values,
)


def test_mask_pan_keeps_bin_and_last4():
    assert mask_pan("4111111111111111") == "411111******1111"
    assert mask_pan("123456") == "******"  # too short to reveal anything


def test_redact_masks_sensitive_des_only():
    out = redact_values(
        {
            "2": "4111111111111111",
            "4": "000000010000",
            "35": "4111=2512",
            "52": "ABCDEF",
            "39": "00",
        }
    )
    assert out["2"] == "411111******1111"
    assert out["4"] == "000000010000"  # amount untouched
    assert out["35"] == "*********"  # track2 fully masked
    assert out["52"] == "******"  # PIN block fully masked
    assert out["39"] == "00"


def test_capture_redacts_by_default():
    buf = CaptureBuffer()
    buf.add("c2u", {"mti": "0200", "de": {"2": "4111111111111111", "39": ""}})
    row = buf.rows()[0]
    assert row["values"]["2"] == "411111******1111"
    assert row["direction"] == "c2u" and row["seq"] == 1


def test_capture_can_keep_raw_when_opted_in():
    buf = CaptureBuffer(store_raw=True)
    buf.add("u2c", {"mti": "0210", "de": {"39": "00"}}, raw=b"\x01\x02")
    assert buf.rows()[0]["raw"] == "0102"
    # default: no raw
    buf2 = CaptureBuffer()
    buf2.add("u2c", {"mti": "0210", "de": {}}, raw=b"\x01\x02")
    assert "raw" not in buf2.rows()[0]


def test_capture_is_bounded_and_counts_drops():
    buf = CaptureBuffer(max_frames=3)
    for i in range(5):
        buf.add("c2u", {"mti": "0200", "de": {"11": str(i)}})
    rows = buf.rows()
    assert len(rows) == 3
    assert [r["values"]["11"] for r in rows] == ["2", "3", "4"]  # oldest evicted
    assert buf.dropped == 2


def test_capture_disabled_records_nothing():
    buf = CaptureBuffer(enabled=False)
    buf.add("c2u", {"mti": "0200", "de": {}})
    assert len(buf) == 0


def test_build_scenario_pairs_request_with_response():
    rows = [
        {
            "seq": 1,
            "direction": "c2u",
            "mti": "0200",
            "values": {"2": "411111******1111", "4": "1000"},
        },
        {"seq": 2, "direction": "u2c", "mti": "0210", "values": {"39": "00"}},
        {"seq": 3, "direction": "c2u", "mti": "0200", "values": {"4": "999999"}},
        {"seq": 4, "direction": "u2c", "mti": "0210", "values": {"39": "61"}},
    ]
    draft = build_scenario_draft(rows, "captured-run")
    assert draft["name"] == "captured-run"
    assert len(draft["steps"]) == 2
    s1 = draft["steps"][0]
    assert s1["action"] == "send_message"
    assert s1["target"] == "tcp_iso8583"
    assert s1["payload"]["mti"] == "0200"
    assert s1["payload"]["values"]["4"] == "1000"
    assert s1["assertions"] == [{"field": "response_code", "operator": "eq", "expected": "00"}]
    assert draft["steps"][1]["assertions"][0]["expected"] == "61"


RULES = [
    {"when": {"de": {"39": {"ne": "00"}}}, "log": {"tag": "declined", "level": "warn"}},
    {"when": {"mti": "0200", "de": {"4": {"gte": "000000100000"}}}, "log": {"tag": "high-value"}},
]


def test_match_log_rules_first_match_wins_and_ne_operator():
    decline = match_log_rules({"mti": "0210", "de": {"39": "05"}}, RULES)
    assert decline == {"tag": "declined", "level": "warn"}
    big = match_log_rules({"mti": "0200", "de": {"39": "00", "4": "000000200000"}}, RULES)
    assert big == {"tag": "high-value"}
    # DE values compare as strings, so amounts are the zero-padded wire form.
    small = {"mti": "0200", "de": {"39": "00", "4": "000000001000"}}
    assert match_log_rules(small, RULES) is None


def test_match_log_rules_direction_scope():
    rules = [{"when": {"direction": "u2c", "de": {"39": {"ne": "00"}}}, "log": {"tag": "decline"}}]
    assert match_log_rules({"direction": "u2c", "de": {"39": "05"}}, rules)
    assert match_log_rules({"direction": "c2u", "de": {"39": "05"}}, rules) is None


def test_capture_tags_matched_frames_in_all_mode():
    buf = CaptureBuffer(rules=RULES, mode="all")
    buf.add("u2c", {"mti": "0210", "de": {"39": "05"}})  # declined → tagged
    buf.add("u2c", {"mti": "0210", "de": {"39": "00"}})  # approved → kept, untagged
    rows = buf.rows()
    assert len(rows) == 2
    assert rows[0]["tag"] == "declined" and rows[0]["level"] == "warn"
    assert "tag" not in rows[1]


def test_capture_matched_mode_logs_only_matches():
    buf = CaptureBuffer(rules=RULES, mode="matched")
    buf.add("u2c", {"mti": "0210", "de": {"39": "05"}})  # matches → logged
    buf.add("u2c", {"mti": "0210", "de": {"39": "00"}})  # no match → skipped
    buf.add("c2u", {"undecoded": True})  # can't match → skipped
    rows = buf.rows()
    assert len(rows) == 1
    assert rows[0]["tag"] == "declined"
    assert buf.skipped == 2


def test_capture_note_is_stamped():
    buf = CaptureBuffer(
        rules=[
            {
                "when": {"de": {"2": {"prefix": "400000"}}},
                "log": {"tag": "watchlist", "note": "blocked BIN"},
            }
        ]
    )
    buf.add("c2u", {"mti": "0200", "de": {"2": "4000001234567890"}})
    assert buf.rows()[0]["note"] == "blocked BIN"
    assert buf.rows()[0]["tag"] == "watchlist"


def test_build_scenario_skips_undecoded_and_unpaired():
    rows = [
        {"seq": 1, "direction": "u2c", "undecoded": True},  # ignored
        {"seq": 2, "direction": "c2u", "mti": "0200", "values": {}},  # no response yet
    ]
    assert build_scenario_draft(rows, "x")["steps"] == []
