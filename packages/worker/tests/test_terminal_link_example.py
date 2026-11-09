"""The ``examples/terminal-link`` pack runs end to end: the POS purchase scenario
drives the ``pos_terminal_link`` connection (ascii4 framing, binary-bitmap ASCII
dialect, three-digit response code) against the pack's own host stand-in, with
the card and terminal bound from their pools and the echo assertions resolved
against the request that actually went out.

The pieces are wired the way the orchestrator wires a real run
(``_bind_connection_format`` / ``_bind_simulator_format`` /
``_attach_message_formats`` / the pool resolution): the Message Format bound
by ``message_format_id`` lands on the connection and the simulator as
``fields`` + ``encoding`` (+ ``validate.presence`` on the simulator) and on
the step as ``fields`` / ``presence``; ``card_pool`` / ``terminal_pool`` names
resolve to inline ``cards`` / ``terminals``.
"""

import json
import pathlib

from worker.adapters.tcp.responder import TcpResponder
from worker.engine import PASSED, InMemorySink, WorkerEngine

PACK = pathlib.Path(__file__).resolve().parents[3] / "examples" / "terminal-link"


def _load(name: str) -> dict:
    return json.loads((PACK / name).read_text())


def test_pack_files_are_consistent():
    fmt, conn, scenario, sim = (
        _load("message-format.json"),
        _load("connection.json"),
        _load("scenario.json"),
        _load("host-simulator.json"),
    )
    link = conn["connections"]["pos_terminal_link"]
    assert link["framing"] == {"preset": "ascii4"} and link["mode"] == "outbound"
    assert link["encoding"] == fmt["definition"]["encoding"]
    fid = link["message_format_id"]
    assert fid == sim["message_format_id"] == scenario["steps"][0]["payload"]["message_format_id"]
    assert sim["framing"] == link["framing"]
    assert fmt["definition"]["fields"]["39"]["length"] == 3  # the dialect's 3-digit code
    assert scenario["steps"][0]["target"] == "pos_terminal_link"
    # the pack names no vendor, scheme or customer; it is a generic POS-to-host link


async def test_pos_purchase_runs_against_the_host_stand_in():
    fmt, conn, scenario = (
        _load("message-format.json"),
        _load("connection.json"),
        _load("scenario.json"),
    )
    cards, terminals = _load("card-pool.json")["cards"], _load("terminal-pool.json")["terminals"]
    definition = fmt["definition"]
    sim = _load("host-simulator.json")
    sim.pop("_comment", None)
    sim["port"] = 0  # ephemeral
    # _bind_simulator_format: the bound format's table, encoding and presence rules
    sim.update(fields=definition["fields"], encoding=definition["encoding"])
    sim["validate"] = {**sim["validate"], "presence": definition["presence"]}
    host = TcpResponder(sim)
    port = await host.start()

    link = dict(conn["connections"]["pos_terminal_link"])
    link.update(host="127.0.0.1", port=port)
    link.update(fields=definition["fields"])  # _bind_connection_format
    env = {"mode": "real", "adapters": {"pos_terminal_link": link}}

    # _attach_message_formats + pool resolution
    scenario["steps"][0]["payload"]["fields"] = definition["fields"]
    scenario["steps"][0]["payload"]["presence"] = definition["presence"]
    scenario["cards"], scenario["terminals"] = cards, terminals

    try:
        engine = WorkerEngine(env, InMemorySink())
        summary = await engine.run_scenario_batch([scenario], run_id="run-pos-purchase")
    finally:
        await host.stop()

    sc = summary["scenarios"][0]
    assert sc["status"] == PASSED, json.dumps(sc, indent=1, default=str)[:3000]
    step = sc["steps"][0]
    assert step["status"] == PASSED and all(a["passed"] for a in step["assertions"])

    req, resp = step["request"]["values"], step["response"]
    # card + terminal bound from the pools, STAN / time stamps generated
    assert req["2"] == cards[0]["pan"] and req["35"] == cards[0]["track2"]
    assert req["41"] == terminals[0]["id"] and req["42"] == terminals[0]["merchant"]
    assert req["49"] == terminals[0]["currency"]
    assert len(req["12"]) == 12 and req["12"].isdigit()  # YYMMDDhhmmss, terminal clock
    assert len(req["7"]) == 10 and len(req["11"]) == 6  # auto-stamped / generated
    # the host approved in the dialect's three-digit code and echoed what we sent
    assert resp["mti"] == "0210" and resp["response_code"] == "000"
    assert resp["fields"]["2"] == req["2"] and resp["fields"]["41"] == req["41"]
    assert resp["fields"]["37"] and resp["fields"]["38"] == "A00001"
    # the echo assertions were resolved against the real request values
    echo = next(a for a in step["assertions"] if a["field"] == "fields.2")
    assert echo["expected"] == req["2"] == echo["actual"]


async def test_the_stand_in_frames_with_a_four_digit_ascii_length():
    """What actually crosses the wire: ``0043`` + ASCII MTI + an 8-byte binary bitmap."""
    sim, fmt = _load("host-simulator.json"), _load("message-format.json")
    sim.pop("_comment", None)
    host = TcpResponder(
        {
            **sim,
            "port": 0,
            "fields": fmt["definition"]["fields"],
            "encoding": fmt["definition"]["encoding"],
        }
    )
    body = host._encode(
        {
            "mti": "0200",
            "fields": {
                "2": {"value": "5413330089600010"},
                "11": {"value": "000001"},
                "41": {"value": "TERM0001"},
                "49": {"value": "981"},
            },
        },
        sim["default"],
    )
    frame = host._frame(body)
    assert frame[:4].isdigit() and int(frame[:4]) == len(body)
    assert frame[4:8] == b"0210"  # ASCII MTI
    bitmap = frame[8:16]
    assert len(bitmap) == 8 and not bitmap.isalnum()  # raw binary bitmap, not hex text


def test_generated_rrn_fills_the_fixed_width_field():
    """``generate: {"37": "random_rrn"}`` used to emit six digits into the
    12-character DE 37; the codec does not pad short fixed fields, so every
    reply that did not echo an RRN desynced from DE 38 on (measured 2026-11-09
    while wiring this pack). The generated value is sized to the bound table."""
    from payprobe_common import iso8583

    host = TcpResponder(
        {
            "port": 0,
            "default": {"generate": {"37": "random_rrn"}, "set": {"39": "00", "38": "A00001"}},
        }
    )
    body = host._encode({"mti": "0200", "fields": {}, "de": {}}, host.default)
    parsed = iso8583.unpack(body, iso8583.DEFAULT_FIELDS, "ascii")
    assert len(parsed["fields"]["37"]["value"]) == 12
    assert parsed["fields"]["38"]["value"] == "A00001" and parsed["fields"]["39"]["value"] == "00"
