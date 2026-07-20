"""Iso8583Protocol.encode honors a per-step DE-table (dialect) override.

A tcp_iso8583 step can pin its own dialect by snapshotting a Message Format's
field table into ``payload['fields']``; packing must then use that table (and its
length conventions / MTI), falling back to the connection's table otherwise."""

import json

from worker.adapters.tcp.protocols import Iso8583Protocol


def _proto():
    # Connection default carries only a STAN field — clearly distinct from the
    # richer dialect a step may override with.
    return Iso8583Protocol({"fields": {"11": {"name": "STAN", "len_type": "fixed", "length": 6}}})


_NT = {
    "2": {"name": "PAN", "len_type": "llvar", "length": 19, "type": "n"},
    "4": {"name": "Amt", "len_type": "fixed", "length": 12, "type": "n"},
    "11": {"name": "STAN", "len_type": "fixed", "length": 6, "type": "n"},
    "126": {"name": "Acq", "len_type": "llllvar", "length": 4000, "type": "ans"},
}


def test_encode_uses_payload_fields_override():
    p = _proto()
    msg = p.encode(
        "send_message",
        {
            "mti": "1100",
            "values": {
                "2": "4111111111111111",
                "4": "000000010000",
                "11": "000001",
                "126": "X" * 1500,
            },
            "fields": _NT,
        },
    )
    body = msg.body.decode()
    assert body[:4] == "1100"
    # DE126 packed with a 4-digit LLLLVAR length prefix from the override table —
    # only possible if encode used the step's dialect, not the connection default.
    assert ("1500" + "X" * 1500) in body


def test_encode_accepts_fields_override_as_json_string():
    p = _proto()
    msg = p.encode(
        "send_message",
        {
            "mti": "1200",
            "values": {"4": "000000010000", "11": "000002"},
            "fields": json.dumps(_NT),
        },
    )
    assert msg.body.decode()[:4] == "1200"


def test_encode_falls_back_to_connection_fields():
    p = _proto()
    msg = p.encode("send_message", {"mti": "0200", "values": {"11": "000003"}})
    assert msg.body.decode()[:4] == "0200"


def test_time_fields_sized_to_dialect():
    """DE7 auto-stamp width follows the bound dialect: 10 (1987) vs 14 (1993/NT)."""
    short = Iso8583Protocol(
        {
            "fields": {
                "7": {"name": "Txn DT", "len_type": "fixed", "length": 10, "type": "n"},
                "11": {"name": "STAN", "len_type": "fixed", "length": 6},
            }
        }
    )
    long = Iso8583Protocol(
        {
            "fields": {
                "7": {"name": "Txn DT", "len_type": "fixed", "length": 14, "type": "n"},
                "11": {"name": "STAN", "len_type": "fixed", "length": 6},
            }
        }
    )
    assert (
        len(
            short.encode("send_message", {"mti": "0200", "values": {"11": "1"}}).request["values"][
                "7"
            ]
        )
        == 10
    )
    assert (
        len(
            long.encode("send_message", {"mti": "1200", "values": {"11": "1"}}).request["values"][
                "7"
            ]
        )
        == 14
    )
