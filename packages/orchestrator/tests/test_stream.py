"""Streaming layer tests: backbone resume + orchestrator WebSocket transport.

Run from the packages/ directory:
    cd packages && python -m pytest orchestrator/tests worker/tests -v
"""
import asyncio

import pytest

from worker.engine import (
    WorkerEngine, StreamSink, InMemoryStreamBackbone,
    RUN_COMPLETED, STEP_RESULT, RUN_STARTED, PHASE_UPDATE,
)

MOCK_ENV = {
    "mode": "mock",
    "adapters": {
        "terminal_sim": {"mock": True, "latency_ms": 0},
        "http": {"mock": True, "latency_ms": 0},
    },
}

SCENARIO = {
    "id": "sc-1", "name": "tap", "test_class": "e2e",
    "steps": [
        {"id": "s1", "target": "terminal_sim", "action": "tap_nfc",
         "payload": {}, "assertions": [
             {"field": "status", "operator": "eq", "expected": "tapped"}]},
        {"id": "s2", "target": "http", "action": "send_auth_request",
         "payload": {"amount": 2000}, "assertions": [
             {"field": "response_code", "operator": "eq", "expected": "00"}]},
    ],
}


# -- backbone: durability + resume -------------------------------------------

async def test_backbone_replays_full_history_from_start():
    bb = InMemoryStreamBackbone()
    await bb.append("r", {"type": STEP_RESULT, "data": {"step": "a"}})
    await bb.append("r", {"type": STEP_RESULT, "data": {"step": "b"}})
    await bb.append("r", {"type": RUN_COMPLETED, "data": {}})

    seen = [(eid, ev["type"]) async for eid, ev in bb.stream("r")]
    assert [eid for eid, _ in seen] == ["1", "2", "3"]
    assert seen[-1][1] == RUN_COMPLETED


async def test_backbone_resumes_after_last_event_id():
    """A reconnecting client passes its last id and gets only newer events."""
    bb = InMemoryStreamBackbone()
    await bb.append("r", {"type": STEP_RESULT, "data": {"step": "a"}})
    await bb.append("r", {"type": STEP_RESULT, "data": {"step": "b"}})
    await bb.append("r", {"type": RUN_COMPLETED, "data": {}})

    resumed = [ev["data"] async for _, ev in bb.stream("r", after_id="1")]
    assert resumed == [{"step": "b"}, {}]  # missed nothing, replayed nothing twice


async def test_backbone_tails_live_events():
    """Consumer subscribes before events exist, then receives them live."""
    bb = InMemoryStreamBackbone()

    async def produce():
        await asyncio.sleep(0.01)
        await bb.append("r", {"type": STEP_RESULT, "data": {}})
        await bb.append("r", {"type": RUN_COMPLETED, "data": {}})

    async def consume():
        return [ev["type"] async for _, ev in bb.stream("r")]

    _, types = await asyncio.gather(produce(), consume())
    assert types == [STEP_RESULT, RUN_COMPLETED]


# -- orchestrator: WebSocket transport ---------------------------------------
#
# We call the real endpoint coroutine (orchestrator.api.main.stream_run) with a
# fake WebSocket. This exercises the actual resume/forward/close logic against
# the durable backbone without starlette's TestClient portal threads, which
# deadlock under pytest-asyncio's managed event loop. The transport itself is
# verified separately in the manual smoke run.


class FakeWebSocket:
    """Minimal stand-in for starlette's WebSocket for endpoint unit tests."""

    def __init__(self, query: dict | None = None) -> None:
        self.query_params = query or {}
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def app_module():
    from orchestrator.api import main as m
    from orchestrator.api.run_store import RunStore
    m.backbone = InMemoryStreamBackbone()
    m.run_store = RunStore(":memory:")
    m.RUNS.clear()
    return m


async def _prefill(module, run_id: str) -> None:
    engine = WorkerEngine(MOCK_ENV, StreamSink(module.backbone))
    await engine.run_scenario_batch([SCENARIO], run_id)


async def test_ws_replays_completed_run_from_start(app_module):
    await _prefill(app_module, "run-a")

    ws = FakeWebSocket()
    await app_module.stream_run(ws, "run-a")

    types = [e["type"] for e in ws.sent]
    assert ws.accepted and ws.closed
    assert RUN_STARTED in types
    assert PHASE_UPDATE in types
    assert STEP_RESULT in types
    assert types[-1] == RUN_COMPLETED
    # every frame carries a resume id
    assert all("id" in e for e in ws.sent)


async def test_ws_resume_skips_already_seen_events(app_module):
    await _prefill(app_module, "run-b")

    ws = FakeWebSocket({"last_event_id": "2"})
    await app_module.stream_run(ws, "run-b")

    ids = [int(e["id"]) for e in ws.sent]
    assert min(ids) > 2              # nothing at/before the cursor replayed
    assert ids == sorted(ids)       # delivered in order
    assert ws.sent[-1]["type"] == RUN_COMPLETED


async def test_create_run_executes_and_persists_stream(app_module):
    """POST /runs wiring: launches the engine, events land durably."""
    resp = await app_module.create_run(
        app_module.CreateRunRequest(environment=MOCK_ENV, scenarios=[SCENARIO])
    )
    rec = app_module.RUNS[resp.run_id]
    await rec.task  # let the background run finish

    assert rec.status == "passed"
    history = app_module.backbone.history(resp.run_id)
    assert history[0][1]["type"] == RUN_STARTED
    assert history[-1][1]["type"] == RUN_COMPLETED
