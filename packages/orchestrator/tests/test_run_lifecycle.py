"""Run-state durability + cross-replica cancel/stop routing."""
import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as main
from orchestrator.api.load_coordinator import LoadCoordinator
from worker.engine.load import InMemoryLoadBus


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


# -- durable RUN_DB default ----------------------------------------------------

def test_default_run_db_dev_is_memory(monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "dev")
    monkeypatch.delenv("RUN_DB", raising=False)
    assert main._default_run_db() == ":memory:"


def test_default_run_db_prod_is_file(monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.delenv("RUN_DB", raising=False)
    assert main._default_run_db() == "/data/runs.db"


def test_default_run_db_explicit_wins(monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("RUN_DB", "/tmp/custom.db")
    assert main._default_run_db() == "/tmp/custom.db"


# -- cancel routing ------------------------------------------------------------

def test_cancel_unknown_run_is_404(client):
    assert client.post("/runs/no-such-run/cancel").status_code == 404


def test_cancel_finished_run_reports_terminal_status(client):
    rid = "finished-run-1"
    main.run_store.create(rid, "mock", 1, label="t")
    main.run_store.finish(rid, "passed", {"status": "passed", "scenarios": []})
    r = client.post(f"/runs/{rid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "passed"


def test_cancel_routes_to_owning_replica(client, monkeypatch):
    """A run not local here is cancelled via the control channel (other replica)."""
    seen = {}

    async def fake_request_cancel(run_id):
        seen["run_id"] = run_id
        return True

    monkeypatch.setattr(main.run_control, "request_cancel", fake_request_cancel)
    main.RUNS.pop("remote-run", None)

    r = client.post("/runs/remote-run/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelling"
    assert seen["run_id"] == "remote-run"


# -- cross-replica load-run stop ----------------------------------------------

class _Store:
    def __init__(self, status): self.status = status
    def get(self, run_id): return {"status": self.status} if run_id == "L1" else None


@pytest.mark.asyncio
async def test_load_stop_signals_bus_for_remote_running_run():
    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, backbone=None, run_store=_Store("running"))
    # L1 is not in coord.runs (owned by another replica) but is live in the store
    assert await coord.stop("L1") is True
    assert await bus.is_stopped("L1") is True


@pytest.mark.asyncio
async def test_load_stop_returns_false_when_not_running():
    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, backbone=None, run_store=_Store("completed"))
    assert await coord.stop("L1") is False


@pytest.mark.asyncio
async def test_load_stop_unknown_run_false():
    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, backbone=None, run_store=_Store("running"))
    assert await coord.stop("other") is False
