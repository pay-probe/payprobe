"""In-graph relay node — a relay wired to further nodes forwards the inbound
request to its upstream connection and exposes the answer as ``${<id>.response}``,
so a following reply can return (or reshape) the upstream's response.

(A TERMINAL relay never reaches the graph — the orchestrator launches it as a
transparent TcpProxy instead; see orchestrator ``_relay_is_terminal``.)"""

import pytest

from worker.adapters.base.base_adapter import StepResult
from worker.engine import FlowRunner, NullSink, PASSED, FAILED
from worker.engine.runner import GraphExecutor


def _relay_flow(reply_payload):
    return {
        "id": "relay-flow",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {
                "id": "fwd",
                "kind": "relay",
                "target": "upstream_conn",
                "config": {"connection": "upstream_conn"},
            },
            {"id": "r", "kind": "reply", "payload": reply_payload},
        ],
        "edges": [
            {"source": "t", "source_port": "out", "target": "fwd"},
            {"source": "fwd", "source_port": "out", "target": "r"},
        ],
    }


def _upstream(response, *, success=True):
    """Stub outbound executor recording what the relay dispatched."""
    calls: list[tuple] = []

    async def _exec(target, action, payload, assertions):
        calls.append((target, action, payload))
        return StepResult(
            success=success,
            request_payload=payload,
            response_payload=response,
            duration_ms=1,
        )

    return _exec, calls


@pytest.mark.asyncio
async def test_relay_forwards_request_and_reply_echoes_response():
    upstream_answer = {"mti": "0210", "fields": {"11": "000123", "39": "00"}}
    exec_step, calls = _upstream(upstream_answer)
    runner = FlowRunner(NullSink(), run_id="relay-1")
    res = await runner.run_flow(
        _relay_flow("${fwd.response}"),
        {"mti": "0200", "de": {"2": "4111111111111111", "11": "000123"}},
        execute_step=exec_step,
    )
    assert res.status == PASSED
    # the relay shaped the ISO flow view into an outbound payload
    assert calls == [
        (
            "upstream_conn",
            "relay",
            {"mti": "0200", "values": {"2": "4111111111111111", "11": "000123"}},
        )
    ]
    # the reply node returned the upstream's answer verbatim
    assert res.reply == upstream_answer
    # and it is addressable for reshaping too
    assert res.context["fwd"]["response"] == upstream_answer


@pytest.mark.asyncio
async def test_relay_response_can_be_reshaped_in_reply():
    exec_step, _ = _upstream({"mti": "0210", "fields": {"39": "05"}})
    runner = FlowRunner(NullSink(), run_id="relay-2")
    res = await runner.run_flow(
        _relay_flow({"mti": "${fwd.response.mti}", "set": {"39": "${fwd.response.fields.39}"}}),
        {"mti": "0200", "de": {"11": "1"}},
        execute_step=exec_step,
    )
    assert res.reply == {"mti": "0210", "set": {"39": "05"}}


@pytest.mark.asyncio
async def test_relay_without_connection_errors_and_stops():
    flow = _relay_flow("${fwd.response}")
    for n in flow["nodes"]:
        if n["kind"] == "relay":
            n.pop("target"), n.pop("config")
    exec_step, calls = _upstream({})
    runner = FlowRunner(NullSink(), run_id="relay-3")
    res = await runner.run_flow(flow, {"mti": "0200", "de": {}}, execute_step=exec_step)
    assert res.status == FAILED
    assert calls == []  # nothing dispatched
    assert res.reply is None  # responder then falls back to its default action


@pytest.mark.asyncio
async def test_relay_upstream_failure_marks_flow_failed():
    exec_step, _ = _upstream({}, success=False)
    runner = FlowRunner(NullSink(), run_id="relay-4")
    res = await runner.run_flow(
        _relay_flow("${fwd.response}"), {"mti": "0200", "de": {}}, execute_step=exec_step
    )
    assert res.status == FAILED


def test_relay_payload_shapes_host_command_view():
    """A header-echo (HSM) request forwards command+data; the upstream leg mints
    its own correlation header."""
    p = GraphExecutor._relay_payload(
        {"header": "0001", "command": "NC", "data": "", "raw": "0001NC"}
    )
    assert p == {"command": "NC", "data": ""}


def test_relay_payload_passes_unknown_shapes_through():
    assert GraphExecutor._relay_payload({"foo": 1}) == {"foo": 1}
    assert GraphExecutor._relay_payload(None) == {}


# -- end-to-end over a real socket --------------------------------------------


@pytest.mark.asyncio
async def test_relay_reply_echoes_live_upstream_over_the_wire():
    """trigger → relay → reply("${fwd.response}"): the client's answer is the
    upstream's answer, carried through the graph (not a byte-level proxy)."""
    from worker.adapters.tcp.adapter import TcpAdapter
    from worker.adapters.tcp.flow_responder import FlowResponder
    from worker.adapters.tcp.responder import TcpResponder

    upstream = TcpResponder(
        {
            "protocol": "iso8583",
            "default": {"echo": ["11", "37"], "set": {"39": "61", "38": "UP0007"}},
        }
    )
    upstream_port = await upstream.start()

    downstream = {
        "upstream_conn": {
            "adapter": "tcp",
            "host": "127.0.0.1",
            "port": upstream_port,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    }
    flow = _relay_flow("${fwd.response}")
    relay_participant = FlowResponder({"protocol": "iso8583"}, flow, downstream=downstream)
    relay_port = await relay_participant.start()

    client = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": relay_port,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await client.connect()
    try:
        res = await client.execute("send_0200", {"amount": 1000, "pan": "4111111111111111"})
        assert res.success
        # the upstream's decision came back through relay → reply
        assert res.response_payload["response_code"] == "61"
        assert res.response_payload["auth_code"] == "UP0007"
        assert res.response_payload["stan"]  # correlation survived both hops
    finally:
        await client.disconnect()
        await relay_participant.stop()
        await upstream.stop()


@pytest.mark.asyncio
async def test_hsm_relay_reply_carries_payshield_answer():
    """The user's failing chain: HSM client → header_echo relay flow → payShield
    sim. The reply node echoes ``${fwd.response}`` (an HSM-client shape with
    ``raw`` and no ``command``) — the responder must replay the upstream answer
    under the client's header so ``ok``/``kcv`` assertions see real values."""
    from worker.adapters.hsm.adapter import HSMAdapter
    from worker.adapters.hsm.payshield import PayShieldSimulator
    from worker.adapters.tcp.flow_responder import FlowResponder

    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0})
    sim_port = await sim.start()

    downstream = {
        "upstream_conn": {
            "adapter": "payshield",
            "host": "127.0.0.1",
            "port": sim_port,
            "response_timeout_sec": 2,
        }
    }
    relay_participant = FlowResponder(
        {"protocol": "header_echo", "header_bytes": 4},
        _relay_flow("${fwd.response}"),
        downstream=downstream,
    )
    relay_port = await relay_participant.start()

    client = HSMAdapter(
        {"host": "127.0.0.1", "port": relay_port, "header_bytes": 4, "response_timeout_sec": 2}
    )
    try:
        res = await client.execute("diagnostics", {})
        assert res.success, res.error
        assert res.response_payload["ok"] is True
        assert res.response_payload["error_code"] == "00"
        assert res.response_payload["response_code"] == "ND"  # NC's reply command
        assert res.response_payload["lmk_check"]  # payload survived the hop
    finally:
        await client.disconnect()
        await relay_participant.stop()
        await sim.stop()
