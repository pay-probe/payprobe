"""If the engine raises before emitting its own terminal event, the orchestrator
must still publish run.completed so live watchers (the portal) don't hang on
"Waiting for step results…"."""
import pytest

from orchestrator.api import main as m
from worker.engine import InMemoryStreamBackbone, RUN_COMPLETED
from orchestrator.api.run_store import RunStore


@pytest.fixture()
def app_module(monkeypatch):
    m.backbone = InMemoryStreamBackbone()
    m.run_store = RunStore(":memory:")
    m.RUNS.clear()
    return m


async def test_engine_crash_still_emits_run_completed(app_module, monkeypatch):
    class BoomEngine:
        def __init__(self, env, sink):
            pass

        async def run_scenario_batch(self, scenarios, run_id, debug_hook=None):
            raise RuntimeError("warmup exploded")

    monkeypatch.setattr(app_module, "WorkerEngine", BoomEngine)

    rec = app_module.RunRecord("r-boom", {"mode": "mock"}, [{"id": "sc", "steps": []}])
    app_module.run_store.create("r-boom", "mock", 1)
    app_module.RUNS["r-boom"] = rec

    await app_module._run_engine(rec)

    assert rec.status == "error"
    # the stream got a terminal event so subscribers complete instead of hanging
    entries = app_module.backbone._events.get("r-boom", [])
    types = [payload.get("type") for _id, payload in entries]
    assert RUN_COMPLETED in types
    # and the registry/run store reflects the error
    assert app_module.run_store.get("r-boom")["status"] == "error"
