"""Starting a load run must fail loudly, not silently.

When the bus/control infra is unreachable (e.g. Redis down), the coordinator's
``start`` raises *after* the run record is created. The endpoint must turn that
into a structured 502 (so the portal can show the real reason instead of a bare
"Failed to start load run") and roll the orphan record out of "running"."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


async def test_start_failure_returns_502_and_rolls_back(store, monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(m.load_coordinator, "start", boom)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)

    assert exc.value.status_code == 502
    assert "could not start load run" in exc.value.detail
    assert "Connection refused" in exc.value.detail

    # the run record exists but is not left dangling in "running"
    runs = store.list()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


async def test_resolve_failure_returns_502_not_naked_500(store, monkeypatch):
    """An unexpected error while resolving the env/scenario (e.g. scenario-
    service unreachable) must become a structured 502, never a plain-text 500."""
    async def boom(*args, **kwargs):
        raise RuntimeError("scenario-service exploded")

    monkeypatch.setattr(m, "_resolve_load_transaction", boom)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)

    assert exc.value.status_code == 502
    assert "could not resolve transaction" in exc.value.detail
    assert "scenario-service exploded" in exc.value.detail
    # nothing was created — we failed before the run record
    assert store.list() == []


async def test_resolve_httpexception_passes_through(store, monkeypatch):
    """A clean error from resolution (e.g. 404 unknown scenario) is preserved
    as-is, not rewrapped as a 502."""
    async def not_found(*args, **kwargs):
        raise HTTPException(404, "scenario 'ghost' not found")

    monkeypatch.setattr(m, "_resolve_load_transaction", not_found)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)

    assert exc.value.status_code == 404
    assert exc.value.detail == "scenario 'ghost' not found"


async def test_short_fleet_surfaces_provisioner_notice():
    """When the provisioner brings up fewer workers than requested, the reason
    (docker error / crashed-worker logs) must land on run.notice — previously it
    only went to the orchestrator log and the portal showed "1/4 workers" with
    no explanation."""
    from worker.engine.load import LoadProfile
    from orchestrator.api.load_coordinator import LoadRun

    class ShortFleet:
        available = True

        async def scale(self, run_id, desired):
            return {"kind": "docker", "available": True, "requested": desired,
                    "running": 1,
                    "notice": "3 worker(s) exited on startup — cannot connect "
                              "to redis://redis:6379"}

    coord = m.load_coordinator
    orig = coord.provisioner
    coord.provisioner = ShortFleet()
    try:
        run = LoadRun("r-short", LoadProfile(type="steady", duration_s=5,
                                             workers=4, target_tps=100))
        await coord._provision(run)
        assert run.notice is not None
        assert "1/4" in run.notice
        assert "exited on startup" in run.notice
    finally:
        coord.provisioner = orig


async def test_full_fleet_leaves_notice_empty():
    from worker.engine.load import LoadProfile
    from orchestrator.api.load_coordinator import LoadRun

    class FullFleet:
        available = True

        async def scale(self, run_id, desired):
            return {"kind": "docker", "available": True,
                    "requested": desired, "running": desired}

    coord = m.load_coordinator
    orig = coord.provisioner
    coord.provisioner = FullFleet()
    try:
        run = LoadRun("r-full", LoadProfile(type="steady", duration_s=5,
                                            workers=4, target_tps=100))
        await coord._provision(run)
        assert run.notice is None
    finally:
        coord.provisioner = orig
