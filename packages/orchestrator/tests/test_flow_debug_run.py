"""Participant-flow debug run (editor test-fire / step-through).

POST /flows/debug-run executes one flow against a sample trigger request as a
first-class run: step.result events stream live, breakpoints pause via the same
DebugSession the scenario debugger uses, and the summary lands in the run store
shaped like a one-scenario run so the editor's results plumbing just works.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import main as m
from orchestrator.api.run_store import RunStore
from worker.engine import (
    InMemoryStreamBackbone, RUN_STARTED, RUN_COMPLETED, STEP_RESULT,
    DEBUG_PAUSED,
)


@pytest.fixture()
def app_module():
    m.backbone = InMemoryStreamBackbone()
    m.run_store = RunStore(":memory:")
    m.RUNS.clear()
    return m


def _flow():
    """Approve 0200s, decline everything else (trigger → if → reply)."""
    return {
        "id": "issuer-stub",
        "name": "issuer stub",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {
                "id": "chk",
                "kind": "if",
                "config": {"left": "${request.mti}", "operator": "eq", "right": "0200"},
            },
            {"id": "approve", "kind": "reply",
             "payload": {"mti": "0210", "de": {"39": "00", "11": "${request.de.11}"}}},
            {"id": "decline", "kind": "reply",
             "payload": {"mti": "0210", "de": {"39": "12"}}},
        ],
        "edges": [
            {"source": "t", "source_port": "out", "target": "chk"},
            {"source": "chk", "source_port": "true", "target": "approve"},
            {"source": "chk", "source_port": "false", "target": "decline"},
        ],
    }


def _event_types(app_module, run_id):
    entries = app_module.backbone._events.get(run_id, [])
    return [payload.get("type") for _id, payload in entries]


# -- plain test-fire (no debug) ------------------------------------------------

async def test_flow_test_fire_runs_to_completion(app_module):
    rec = m.RunRecord("fr-1", {}, [_flow()])
    app_module.run_store.create("fr-1", "flow", 1, label="flow test: issuer stub")
    app_module.RUNS["fr-1"] = rec

    await app_module._run_flow_debug(
        rec, _flow(), {"mti": "0200", "de": {"11": "000042"}}, {}, {})

    assert rec.status == "passed"
    assert rec.summary["kind"] == "flow-debug"
    # the reply resolved ${request.de.11} from the sample request
    assert rec.summary["reply"] == {"mti": "0210", "de": {"39": "00", "11": "000042"}}
    # summary is shaped like a one-scenario run (editor results plumbing)
    sc = rec.summary["scenarios"][0]
    assert sc["scenario_id"] == "issuer-stub"
    assert [s["step_id"] for s in sc["steps"]]
    # events streamed: started, per-node step results, terminal completed
    types = _event_types(app_module, "fr-1")
    assert types[0] == RUN_STARTED
    assert STEP_RESULT in types
    assert types[-1] == RUN_COMPLETED
    # run store finished with the verdict
    assert app_module.run_store.get("fr-1")["status"] == "passed"


async def test_flow_test_fire_takes_false_branch(app_module):
    rec = m.RunRecord("fr-2", {}, [_flow()])
    app_module.run_store.create("fr-2", "flow", 1)
    app_module.RUNS["fr-2"] = rec

    await app_module._run_flow_debug(rec, _flow(), {"mti": "0800", "de": {}}, {}, {})

    assert rec.summary["reply"] == {"mti": "0210", "de": {"39": "12"}}


# -- step-through debug --------------------------------------------------------

async def test_flow_debug_pauses_at_breakpoint_and_resumes(app_module):
    rec = m.RunRecord("fr-3", {}, [_flow()])
    rec.debug = m.DebugSession(["approve"], step_mode=False)
    app_module.run_store.create("fr-3", "flow", 1)
    app_module.RUNS["fr-3"] = rec

    task = asyncio.create_task(app_module._run_flow_debug(
        rec, _flow(), {"mti": "0200", "de": {"11": "1"}}, {}, {}))
    # wait until the run parks at the breakpoint
    for _ in range(200):
        if rec.debug.paused:
            break
        await asyncio.sleep(0.01)
    assert rec.debug.paused and rec.debug.paused_node == "approve"
    types = _event_types(app_module, "fr-3")
    assert DEBUG_PAUSED in types and RUN_COMPLETED not in types

    # the paused-context snapshot must be JSON-safe (the Redis backbone
    # json.dumps events strictly) and free of engine internals like __gen__
    import json
    paused = next(p for _i, p in app_module.backbone._events["fr-3"]
                  if p.get("type") == DEBUG_PAUSED)
    json.dumps(paused)  # must not raise
    assert "__gen__" not in paused["data"]["context"]

    # continue → the run finishes
    rec.debug.resume.set()
    await task
    assert rec.status == "passed"
    assert RUN_COMPLETED in _event_types(app_module, "fr-3")


async def test_flow_debug_cancel_while_paused_emits_completed(app_module):
    """Stop while parked at a breakpoint must end the live stream (cancelled)."""
    rec = m.RunRecord("fr-4", {}, [_flow()])
    rec.debug = m.DebugSession([], step_mode=True)  # pause at every node
    app_module.run_store.create("fr-4", "flow", 1)
    app_module.RUNS["fr-4"] = rec

    task = asyncio.create_task(app_module._run_flow_debug(
        rec, _flow(), {"mti": "0200", "de": {}}, {}, {}))
    for _ in range(200):
        if rec.debug.paused:
            break
        await asyncio.sleep(0.01)
    assert rec.debug.paused

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.status == "cancelled"
    assert RUN_COMPLETED in _event_types(app_module, "fr-4")
    assert app_module.run_store.get("fr-4")["status"] == "cancelled"


# -- the endpoint ---------------------------------------------------------------

def test_debug_run_endpoint_inline_flow(app_module):
    with TestClient(m.app) as client:
        res = client.post("/flows/debug-run", json={
            "flow": _flow(),
            "request": {"mti": "0200", "de": {"11": "7"}},
            "debug": False,
        })
        assert res.status_code == 200
        run_id = res.json()["run_id"]

        # the run finishes and is retrievable like any other run
        for _ in range(200):
            detail = client.get(f"/runs/{run_id}").json()
            if detail["status"] not in ("pending", "running"):
                break
            import time
            time.sleep(0.01)
        assert detail["status"] == "passed"
        assert detail["summary"]["reply"]["de"]["39"] == "00"


def test_debug_run_endpoint_debug_step_controls(app_module):
    with TestClient(m.app) as client:
        res = client.post("/flows/debug-run", json={
            "flow": _flow(),
            "request": {"mti": "0200", "de": {"11": "9"}},
            "debug": True,          # no breakpoints ⇒ step every node
        })
        assert res.status_code == 200
        run_id = res.json()["run_id"]

        import time
        rec = m.RUNS[run_id]
        for _ in range(200):
            if rec.debug.paused:
                break
            time.sleep(0.01)
        assert rec.debug.paused and rec.debug.paused_node == "t"

        # drive it to the end with the shared debugger controls
        for _ in range(10):
            client.post(f"/runs/{run_id}/debug/continue")
            time.sleep(0.02)
            if not (rec.task and not rec.task.done()):
                break
        for _ in range(200):
            detail = client.get(f"/runs/{run_id}").json()
            if detail["status"] not in ("pending", "running"):
                break
            time.sleep(0.01)
        assert detail["status"] == "passed"


def test_debug_run_endpoint_rejects_empty(app_module):
    with TestClient(m.app) as client:
        assert client.post("/flows/debug-run", json={}).status_code == 400
        assert client.post(
            "/flows/debug-run", json={"flow": {"nodes": []}}
        ).status_code == 400
