"""Provisioning pre-run + cyclic-soak (kind='load') schedule."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402
from orchestrator.api.schedule_store import ScheduleStore  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


def _mix_ok(monkeypatch):
    async def fake_mix(req):
        return {"name": "e"}, [{"id": "pay", "steps": []}], [1.0], "lbl"
    monkeypatch.setattr(m, "_resolve_load_mix", fake_mix)


def _start_ok(monkeypatch):
    async def fake_start(*a, **k):
        return None
    monkeypatch.setattr(m.load_coordinator, "start", fake_start)


# -- provisioning ------------------------------------------------------------

async def test_provisioning_runs_then_starts_load(store, monkeypatch):
    _mix_ok(monkeypatch)
    _start_ok(monkeypatch)
    ran = {}

    async def fake_resolve_prov(req):
        return {"id": "onboard", "steps": []}

    async def fake_run_prov(env, sc):
        ran["did"] = True
        return {"status": "passed"}

    monkeypatch.setattr(m, "_resolve_provision_scenario", fake_resolve_prov)
    monkeypatch.setattr(m, "_run_provisioning", fake_run_prov)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=10,
                           provision_scenario_id="onboard")
    out = await m.create_load_run(req)
    assert ran.get("did") is True
    assert out["status"] == "running"


async def test_provisioning_failure_aborts_load(store, monkeypatch):
    _mix_ok(monkeypatch)
    started = {"called": False}

    async def fake_start(*a, **k):
        started["called"] = True

    async def fake_resolve_prov(req):
        return {"id": "onboard", "steps": []}

    async def fake_run_prov(env, sc):
        return {"status": "failed"}

    monkeypatch.setattr(m.load_coordinator, "start", fake_start)
    monkeypatch.setattr(m, "_resolve_provision_scenario", fake_resolve_prov)
    monkeypatch.setattr(m, "_run_provisioning", fake_run_prov)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=10,
                           provision_scenario_id="onboard")
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)
    assert exc.value.status_code == 424
    assert started["called"] is False          # load never started
    assert store.list() == []                   # no run record created


async def test_no_provisioning_is_a_noop(store, monkeypatch):
    _mix_ok(monkeypatch)
    _start_ok(monkeypatch)

    async def must_not_run(env, sc):  # pragma: no cover
        raise AssertionError("provisioning should not run")

    monkeypatch.setattr(m, "_run_provisioning", must_not_run)
    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=10)
    out = await m.create_load_run(req)
    assert out["status"] == "running"


# -- cyclic soak (kind='load' schedule) --------------------------------------

async def test_load_schedule_triggers_create_load_run(monkeypatch):
    captured = {}

    async def fake_create_load_run(req):
        captured["type"] = req.type
        captured["label"] = req.label
        return {"run_id": "load-1", "status": "running"}

    monkeypatch.setattr(m, "create_load_run", fake_create_load_run)
    sched = {
        "id": "soak-cycle", "kind": "load", "label": "nightly soak",
        "request": {"type": "soak", "connections": 100, "duration_s": 600,
                    "scenario_ids": ["hb"]},
    }
    run_id = await m._trigger_schedule(sched)
    assert run_id == "load-1"
    assert captured["type"] == "soak"
    assert captured["label"] == "nightly soak"


def test_create_load_schedule_validation():
    store = ScheduleStore(":memory:")
    doc = store.create({"label": "cyc", "kind": "load", "interval_sec": 3600,
                        "request": {"type": "soak", "connections": 10}})
    assert doc["kind"] == "load"
    assert store.get(doc["id"])["kind"] == "load"
