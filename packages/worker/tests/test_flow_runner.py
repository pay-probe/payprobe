"""FlowRunner — a participant flow is triggered by a message and produces a reply,
reusing the shared GraphExecutor core (trigger → logic → reply)."""

import pytest

from worker.engine import FlowRunner, NullSink, PASSED


def _flow():
    """A flow that approves 0200s and declines everything else."""
    return {
        "id": "issuer-stub",
        "name": "issuer stub",
        "nodes": [
            {"id": "t", "kind": "trigger", "config": {"connection": "issuer_listen"}},
            {
                "id": "chk",
                "kind": "if",
                "config": {"left": "${request.mti}", "operator": "eq", "right": "0200"},
            },
            {"id": "approve", "kind": "reply", "payload": {"mti": "0210", "field39": "00"}},
            {"id": "decline", "kind": "reply", "payload": {"mti": "0210", "field39": "12"}},
        ],
        "edges": [
            {"source": "t", "source_port": "out", "target": "chk"},
            {"source": "chk", "source_port": "true", "target": "approve"},
            {"source": "chk", "source_port": "false", "target": "decline"},
        ],
    }


@pytest.mark.asyncio
async def test_flow_approves_matching_message():
    runner = FlowRunner(NullSink(), run_id="flow-1")
    res = await runner.run_flow(_flow(), {"mti": "0200", "pan": "411111"})
    assert res.status == PASSED
    assert res.reply == {"mti": "0210", "field39": "00"}


@pytest.mark.asyncio
async def test_flow_declines_other_message():
    runner = FlowRunner(NullSink(), run_id="flow-2")
    res = await runner.run_flow(_flow(), {"mti": "0800"})
    assert res.reply == {"mti": "0210", "field39": "12"}


@pytest.mark.asyncio
async def test_flow_resolves_request_fields_into_reply():
    """A reply can echo fields from the incoming request via ${request.*}."""
    flow = {
        "id": "echo",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "r", "kind": "reply", "payload": {"mti": "0210", "stan": "${request.stan}"}},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "r"}],
    }
    runner = FlowRunner(NullSink(), run_id="flow-3")
    res = await runner.run_flow(flow, {"stan": "000123"})
    assert res.reply == {"mti": "0210", "stan": "000123"}


@pytest.mark.asyncio
async def test_flow_with_no_reply_node_returns_none():
    flow = {
        "id": "noreply",
        "nodes": [{"id": "t", "kind": "trigger"}],
        "edges": [],
    }
    runner = FlowRunner(NullSink(), run_id="flow-4")
    res = await runner.run_flow(flow, {"mti": "0200"})
    assert res.reply is None
