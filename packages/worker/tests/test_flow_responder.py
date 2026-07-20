"""FlowResponder — a listening participant flow answered over a real TcpAdapter
client. Same wire as TcpResponder, but the reply is computed by a flow graph
(trigger -> if -> reply) instead of static rules."""

from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.flow_responder import FlowResponder
from worker.adapters.tcp.responder import TcpResponder


async def _adapter(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "sign_on": {"enabled": False},
        "response_timeout_sec": 2,
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    a = TcpAdapter(cfg)
    await a.connect()
    return a


# A flow that approves 0200s (echo STAN, AUTH99) and declines anything else.
ISSUER_FLOW = {
    "id": "issuer-stub",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {
            "id": "chk",
            "kind": "if",
            "config": {"left": "${request.mti}", "operator": "eq", "right": "0200"},
        },
        {
            "id": "approve",
            "kind": "reply",
            "payload": {"echo": ["11", "37"], "set": {"39": "00", "38": "AUTH99"}},
        },
        {"id": "decline", "kind": "reply", "payload": {"echo": ["11"], "set": {"39": "05"}}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "chk"},
        {"source": "chk", "source_port": "true", "target": "approve"},
        {"source": "chk", "source_port": "false", "target": "decline"},
    ],
}


async def _run(flow):
    r = FlowResponder({"protocol": "iso8583"}, flow)
    port = await r.start()
    a = await _adapter(port)
    return r, a, port


async def test_flow_responder_approves_0200_and_echoes_stan():
    r, a, _ = await _run(ISSUER_FLOW)
    try:
        res = await a.execute("send_0200", {"amount": 1000, "pan": "5111111111111111"})
        assert res.success
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"] == "AUTH99"
        assert res.response_payload["stan"]  # STAN echoed -> correlation worked
    finally:
        await a.disconnect()
        await r.stop()


async def test_flow_responder_declines_non_0200():
    # send a network/0800-style message; the flow's false branch declines (05)
    r, a, _ = await _run(ISSUER_FLOW)
    try:
        res = await a.execute("send_0800", {})
        assert res.response_payload["response_code"] == "05"
    finally:
        await a.disconnect()
        await r.stop()


# Phase 2: a "switch" flow that forwards to a downstream issuer and returns its
# response code — proving outbound calls + response mapping into the reply.
SWITCH_FLOW = {
    "id": "switch",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {
            "id": "callout",
            "kind": "action",
            "target": "issuer",
            "action": "send_0200",
            "payload": {"amount": 1000, "pan": "5111111111111111"},
        },
        {
            "id": "rep",
            "kind": "reply",
            "payload": {"echo": ["11", "37"], "set": {"39": "${callout.response.response_code}"}},
        },
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "callout"},
        {"source": "callout", "source_port": "out", "target": "rep"},
    ],
}


async def test_flow_forwards_to_downstream_and_returns_its_rc():
    # Downstream issuer: a plain responder that always answers DE39 = 07.
    issuer = TcpResponder(
        {"protocol": "iso8583", "default": {"echo": ["11", "37"], "set": {"39": "07"}}}
    )
    issuer_port = await issuer.start()

    downstream = {
        "issuer": {
            "adapter": "tcp",
            "host": "127.0.0.1",
            "port": issuer_port,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    }
    switch = FlowResponder({"protocol": "iso8583"}, SWITCH_FLOW, downstream=downstream)
    switch_port = await switch.start()
    a = await _adapter(switch_port)
    try:
        res = await a.execute("send_0200", {"amount": 500, "pan": "4000000000000002"})
        # the switch forwarded to the issuer and mapped the issuer's DE39 back
        assert res.response_payload["response_code"] == "07"
    finally:
        await a.disconnect()
        await switch.stop()
        await issuer.stop()


# -- cross-hop correlated trace ---------------------------------------------


def test_correlation_id_extracted_or_minted():
    r = FlowResponder({"protocol": "iso8583"}, {})
    # taken from the configured correlation field (DE 11 by default)
    assert r._correlation_id({"de": {"11": "000123"}}) == "000123"
    # minted when absent so the hop can still be stitched
    minted = r._correlation_id({"de": {}})
    assert minted and len(minted) == 12


def test_inject_correlation_stamps_field_without_clobbering():
    r = FlowResponder({"protocol": "iso8583"}, {})
    p = {"mti": "0200"}
    r._inject_correlation(p, "zzz999")
    assert p["de"]["11"] == "zzz999"  # stamped onto a payload with no DE
    p2 = {"de": {"11": "keep"}}
    r._inject_correlation(p2, "new")
    assert p2["de"]["11"] == "keep"  # never overwrites an existing id


async def test_resolve_records_correlated_trace():
    r = FlowResponder({"protocol": "iso8583"}, ISSUER_FLOW, run_id="issuer-stub")
    action = await r._resolve_async({"mti": "0200", "de": {"11": "654321", "37": "rrn"}})
    assert action["set"]["39"] == "00"
    hops = r.traces("654321")
    assert len(hops) == 1
    h = hops[0]
    assert h["flow_id"] == "issuer-stub"
    assert h["mti"] == "0200"
    assert h["response_code"] == "00"
    assert h["calls"] == []  # pure responder, no outbound hop
    # an unrelated correlation id sees nothing
    assert r.traces("nope") == []


async def test_resolve_records_downstream_call_and_propagates_cid():
    captured: dict = {}

    class _SR:
        success = True
        assertions: list = []
        request_payload: dict = {}
        response_payload = {"response_code": "07"}
        duration_ms = 3
        error = None
        raw_log = ""
        status = "passed"

    switch = FlowResponder({"protocol": "iso8583"}, SWITCH_FLOW, run_id="switch")

    async def fake_exec(target, action, payload, assertions):
        captured["target"] = target
        captured["payload"] = payload
        return _SR()

    switch._execute_step = fake_exec  # type: ignore[assignment]

    action = await switch._resolve_async({"mti": "0200", "de": {"11": "abc123"}})
    assert action["set"]["39"] == "07"  # issuer's RC mapped into reply
    assert captured["payload"]["de"]["11"] == "abc123"  # cid propagated downstream
    hops = switch.traces("abc123")
    assert len(hops) == 1 and hops[0]["calls"][0]["target"] == "issuer"


async def test_failed_downstream_hop_is_traced_with_error():
    # SWITCH_FLOW forwards to "issuer", but no downstream is configured -> the
    # registry can't resolve it and the call RAISES. That hop used to vanish from
    # the trace; now it's recorded with the reason, end-to-end.
    switch = FlowResponder({"protocol": "iso8583"}, SWITCH_FLOW, run_id="switch")
    await switch._resolve_async({"mti": "0200", "de": {"11": "fail99", "4": "000000000500"}})
    hops = switch.traces("fail99")
    assert len(hops) == 1
    h = hops[0]
    assert h["ok"] is False  # flow failed end-to-end
    assert h["status"] in ("failed", "error")
    assert h["request"]["de"]["4"] == "000000000500"  # inbound captured
    assert h["calls"], "the failing downstream hop must be recorded"
    call = h["calls"][0]
    assert call["target"] == "issuer" and call["ok"] is False
    assert "issuer" in call["error"]  # the WHY (unknown adapter)


async def test_successful_hop_records_payloads_and_ok():
    switch = FlowResponder({"protocol": "iso8583"}, SWITCH_FLOW, run_id="switch")

    class _SR:
        success = True
        request_payload = {"mti": "0200", "de": {"4": "000000000500"}}
        response_payload = {"response_code": "07", "mti": "0210"}
        duration_ms = 4
        error = None

    async def fake_exec(target, action, payload, assertions):
        return _SR()

    switch._execute_step = fake_exec  # type: ignore[assignment]
    await switch._resolve_async({"mti": "0200", "de": {"11": "ok7"}})
    call = switch.traces("ok7")[0]["calls"][0]
    assert call["ok"] is True and call["error"] is None
    assert call["response"]["response_code"] == "07"  # issuer's reply captured
    assert call["duration_ms"] == 4


def test_trace_ring_buffer_is_bounded():
    r = FlowResponder({"protocol": "iso8583"}, {})
    r._max_traces = 5
    for i in range(12):
        r._record_trace({"correlation_id": str(i)})
    assert len(r._traces) == 5
    assert [t["correlation_id"] for t in r._traces] == ["7", "8", "9", "10", "11"]
