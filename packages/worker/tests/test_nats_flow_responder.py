"""NatsFlowResponder (ADR-0006): a participant flow that listens on NATS subjects
and computes its reply by walking a flow graph (trigger -> if -> reply), sharing
the correlated-trace machinery via FlowTraceMixin. Hops are tagged 'nats' and
carry subject / queue group / inbox instead of MTI / DE 39.
"""

import pytest

# nats-py is an optional dep (ADR-0006): its absence skips this suite.
pytest.importorskip("nats")

from worker.adapters.nats.flow_responder import NatsFlowResponder

# Approves an amount under 1000, declines at/above it. The reply node's payload
# IS the NATS action: {json: {...}}.
PAY_FLOW = {
    "id": "pay-nats",
    "name": "Pay NATS",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {
            "id": "chk",
            "kind": "if",
            "config": {"left": "${request.body.amount}", "operator": "lt", "right": 1000},
        },
        {"id": "ok", "kind": "reply", "payload": {"json": {"decision": "APPROVE"}}},
        {"id": "no", "kind": "reply", "payload": {"json": {"decision": "DECLINE"}}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "chk"},
        {"source": "chk", "source_port": "true", "target": "ok"},
        {"source": "chk", "source_port": "false", "target": "no"},
    ],
}


def _msg(amount, cid="cor-1", subject="pay.auth", reply="_INBOX.a"):
    body = {"amount": amount}
    if cid is not None:
        body["correlation_id"] = cid
    return {"subject": subject, "reply": reply, "body": body, "raw": b""}


async def test_nats_flow_reply_and_trace_shape():
    r = NatsFlowResponder(
        {"protocol": "nats", "subject": "pay.auth", "queue_group": "issuers"},
        PAY_FLOW,
        run_id="pay-nats",
    )
    r._flow_name = "Pay NATS"

    action = await r._resolve_async(_msg(500, cid="cor-1"))
    assert action["json"]["decision"] == "APPROVE"

    hops = r.traces("cor-1")
    assert len(hops) == 1
    h = hops[0]
    assert h["kind"] == "nats"  # tagged as a NATS hop
    assert h["mti"] == "pay.auth"  # label is the subject
    assert h["request"]["subject"] == "pay.auth"
    assert h["request"]["queue_group"] == "issuers"
    assert h["request"]["inbox"] == "_INBOX.a"
    assert h["request"]["body"]["amount"] == 500
    assert h["flow_name"] == "Pay NATS"


async def test_nats_flow_declines_over_limit():
    r = NatsFlowResponder({"protocol": "nats", "subject": "pay.auth"}, PAY_FLOW, run_id="pay-nats")
    action = await r._resolve_async(_msg(2500, cid="cor-2"))
    assert action["json"]["decision"] == "DECLINE"


async def test_nats_correlation_id_minted_when_absent():
    r = NatsFlowResponder({"protocol": "nats", "subject": "pay.auth"}, PAY_FLOW, run_id="pay-nats")
    await r._resolve_async(_msg(100, cid=None))  # no correlation_id in body
    hops = r.traces()
    assert len(hops) == 1 and hops[0]["correlation_id"]  # one was minted


async def test_custom_correlation_field():
    r = NatsFlowResponder(
        {"protocol": "nats", "subject": "pay.auth", "correlation": {"field": "txn_id"}},
        PAY_FLOW,
        run_id="pay-nats",
    )
    msg = {
        "subject": "pay.auth",
        "reply": "_INBOX.b",
        "body": {"amount": 10, "txn_id": "T-42"},
        "raw": b"",
    }
    await r._resolve_async(msg)
    assert r.traces("T-42")  # correlated by the configured body field
