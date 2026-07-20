"""HttpFlowResponder — a participant flow that listens for HTTP requests and
computes its JSON reply by walking a flow graph (trigger -> if -> reply), sharing
the same correlated-trace machinery as the TCP FlowResponder via FlowTraceMixin.
"""

from worker.adapters.http.flow_responder import HttpFlowResponder

# Approves a POST /payments under 1000, declines at/above it. The reply node's
# payload IS the HTTP action: {status, json}.
PAY_FLOW = {
    "id": "pay-api",
    "name": "Pay API",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {
            "id": "chk",
            "kind": "if",
            "config": {"left": "${request.body.amount}", "operator": "lt", "right": 1000},
        },
        {"id": "ok", "kind": "reply", "payload": {"status": 201, "json": {"decision": "APPROVE"}}},
        {"id": "no", "kind": "reply", "payload": {"status": 402, "json": {"decision": "DECLINE"}}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "chk"},
        {"source": "chk", "source_port": "true", "target": "ok"},
        {"source": "chk", "source_port": "false", "target": "no"},
    ],
}


def _req(amount, cid="cor-1"):
    return {
        "method": "POST",
        "path": "/payments",
        "query": {},
        "headers": {"x-correlation-id": cid},
        "body": {"amount": amount},
        "raw": "",
    }


async def test_http_flow_reply_and_trace_shape():
    r = HttpFlowResponder({"protocol": "http"}, PAY_FLOW, run_id="pay-api")
    r._flow_name = "Pay API"

    action = await r._resolve_async(_req(500, cid="cor-1"))
    assert action["status"] == 201 and action["json"]["decision"] == "APPROVE"

    hops = r.traces("cor-1")
    assert len(hops) == 1
    h = hops[0]
    assert h["kind"] == "http"  # tagged as an HTTP hop
    assert h["mti"] == "POST /payments"  # label is method+path
    assert h["response_code"] == "201"  # summary is the HTTP status
    assert h["request"]["method"] == "POST"
    assert h["request"]["body"] == {"amount": 500}
    assert h["flow_name"] == "Pay API"
    assert h["calls"] == []


async def test_http_flow_declines_over_limit():
    r = HttpFlowResponder({"protocol": "http"}, PAY_FLOW, run_id="pay-api")
    action = await r._resolve_async(_req(2500, cid="cor-2"))
    assert action["status"] == 402 and action["json"]["decision"] == "DECLINE"
    assert r.traces("cor-2")[0]["response_code"] == "402"


async def test_http_correlation_id_minted_when_absent():
    r = HttpFlowResponder({"protocol": "http"}, PAY_FLOW, run_id="pay-api")
    req = _req(100)
    req["headers"] = {}  # no correlation header
    await r._resolve_async(req)
    hops = r.traces()
    assert len(hops) == 1 and hops[0]["correlation_id"]  # a cid was minted


async def test_capture_gate_suppresses_recording():
    r = HttpFlowResponder({"protocol": "http"}, PAY_FLOW, run_id="pay-api")
    r._capture = False  # paused
    await r._resolve_async(_req(100, cid="paused"))
    assert r.traces() == []  # handled, but not recorded
    r._capture = True
    await r._resolve_async(_req(100, cid="live"))
    assert len(r.traces("live")) == 1
