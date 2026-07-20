"""Shared simulated state: a state node mutates ${state.*}, persisted per instance."""

from worker.engine import FlowRunner, NullSink
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.flow_responder import FlowResponder

COUNTER_FLOW = {
    "id": "counter",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "s", "kind": "state", "config": {"incr": {"count": 1}}},
        {"id": "r", "kind": "reply", "payload": {"n": "${state.count}"}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "s"},
        {"source": "s", "source_port": "out", "target": "r"},
    ],
}


async def test_state_persists_across_calls_via_shared_dict():
    state: dict = {}
    runner = FlowRunner(NullSink(), run_id="x")
    r1 = await runner.run_flow(COUNTER_FLOW, {}, state=state)
    r2 = await runner.run_flow(COUNTER_FLOW, {}, state=state)
    r3 = await runner.run_flow(COUNTER_FLOW, {}, state=state)
    assert [r1.reply["n"], r2.reply["n"], r3.reply["n"]] == [1, 2, 3]


async def test_state_set_and_append():
    runner = FlowRunner(NullSink(), run_id="x")
    flow = {
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {
                "id": "s",
                "kind": "state",
                "config": {
                    "set": {"last_pan": "${request.pan}"},
                    "append": {"seen": "${request.pan}"},
                },
            },
            {
                "id": "r",
                "kind": "reply",
                "payload": {"last": "${state.last_pan}", "seen": "${state.seen}"},
            },
        ],
        "edges": [
            {"source": "t", "source_port": "out", "target": "s"},
            {"source": "s", "source_port": "out", "target": "r"},
        ],
    }
    st: dict = {}
    await runner.run_flow(flow, {"pan": "4111"}, state=st)
    res = await runner.run_flow(flow, {"pan": "5222"}, state=st)
    assert res.reply["last"] == "5222"
    assert res.reply["seen"] == ["4111", "5222"]


# A live stateful issuer: counts approvals in its shared state across messages.
STATEFUL_ISSUER = {
    "id": "issuer",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "s", "kind": "state", "config": {"incr": {"approvals": 1}}},
        {"id": "r", "kind": "reply", "payload": {"echo": ["11"], "set": {"39": "00"}}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "s"},
        {"source": "s", "source_port": "out", "target": "r"},
    ],
}


async def test_live_responder_state_persists_across_connections():
    resp = FlowResponder({"protocol": "iso8583"}, STATEFUL_ISSUER)
    port = await resp.start()
    try:
        for _ in range(3):
            a = TcpAdapter(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "sign_on": {"enabled": False},
                    "reconnect": {"enabled": False},
                    "response_timeout_sec": 2,
                }
            )
            await a.connect()
            res = await a.execute("send_0200", {"amount": 100})
            assert res.response_payload["response_code"] == "00"
            await a.disconnect()
        # the shared state survived across three separate connections
        assert resp._state["approvals"] == 3
    finally:
        await resp.stop()
