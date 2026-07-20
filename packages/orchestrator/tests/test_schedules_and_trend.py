"""Run trend aggregation + scheduled-regression store and endpoints."""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["DISABLE_SCHEDULER"] = "1"  # don't start the background loop in tests

from fastapi.testclient import TestClient  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402
from orchestrator.api.schedule_store import ScheduleStore, next_after  # noqa: E402


# -- trend --------------------------------------------------------------------

def test_trend_buckets_runs_by_day_with_pass_rate():
    s = RunStore(":memory:")
    s.create("r1", "mock", 2)
    s.finish("r1", "passed", {"scenarios": [{"status": "passed"}, {"status": "passed"}]})
    s.create("r2", "mock", 2)
    s.finish("r2", "failed", {"scenarios": [{"status": "passed"}, {"status": "failed"}]})

    trend = s.trend()
    assert len(trend) == 1                       # both runs are "today"
    b = trend[0]
    assert b["runs"] == 2 and b["passed"] == 1 and b["failed"] == 1
    assert b["scenarios_total"] == 4 and b["scenarios_passed"] == 3
    assert b["pass_rate"] == 0.75


def _finish(store, rid, scenarios):
    store.create(rid, "mock", len(scenarios))
    status = "passed" if all(s["status"] == "passed" for s in scenarios) else "failed"
    store.finish(rid, status, {"scenarios": scenarios})


def test_flakiness_flags_only_intermittent_scenarios():
    s = RunStore(":memory:")
    # flaky: pass, fail, pass, fail across 4 runs
    # stable_pass: always passes; stable_fail: always fails
    seqs = [
        ("flaky", ["passed", "failed", "passed", "failed"]),
        ("stable_pass", ["passed", "passed", "passed", "passed"]),
        ("stable_fail", ["failed", "failed", "failed", "failed"]),
    ]
    for i in range(4):
        _finish(s, f"r{i}", [
            {"scenario_id": sid, "name": sid, "status": outs[i]}
            for sid, outs in seqs
        ])

    flaky = s.flakiness(min_runs=3)
    names = {f["name"] for f in flaky}
    assert names == {"flaky"}                       # stable ones excluded
    f = flaky[0]
    assert f["runs"] == 4 and f["passed"] == 2 and f["failed"] == 2
    assert f["flips"] == 3                           # p->f->p->f
    assert f["score"] == 1.0                         # 3 flips / (4-1)
    assert f["last_status"] == "failed"


def test_flakiness_respects_min_runs():
    s = RunStore(":memory:")
    _finish(s, "a", [{"scenario_id": "x", "name": "x", "status": "passed"}])
    _finish(s, "b", [{"scenario_id": "x", "name": "x", "status": "failed"}])
    # only 2 observations < default min_runs(3) → not reported
    assert s.flakiness(min_runs=3) == []
    # lower the bar → now flagged
    assert [f["name"] for f in s.flakiness(min_runs=2)] == ["x"]


def test_flakiness_blocked_status_ignored():
    s = RunStore(":memory:")
    for i, st in enumerate(("passed", "blocked", "failed", "passed")):
        _finish(s, f"r{i}", [{"scenario_id": "y", "name": "y", "status": st}])
    f = s.flakiness(min_runs=3)[0]
    # blocked is dropped, leaving pass/fail/pass → 3 observations, 2 flips
    assert f["runs"] == 3 and f["flips"] == 2


# -- schedule store -----------------------------------------------------------

def test_next_after_interval_and_daily():
    base = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    assert next_after({"interval_sec": 3600}, base) == base + timedelta(hours=1)
    # daily_at earlier today -> rolls to tomorrow
    nxt = next_after({"daily_at": "06:00"}, base)
    assert nxt == base.replace(hour=6, minute=0) + timedelta(days=1)
    # daily_at later today -> today
    assert next_after({"daily_at": "18:30"}, base) == base.replace(hour=18, minute=30)


def test_create_due_and_mark_ran():
    store = ScheduleStore(":memory:")
    sched = store.create({"label": "nightly", "interval_sec": 1, "request": {}})
    assert sched["id"] == "nightly"
    # not due yet (next_run is ~1s out)
    assert store.due(datetime.now(timezone.utc)) == []
    # due in the future
    future = datetime.now(timezone.utc) + timedelta(seconds=2)
    assert [s["id"] for s in store.due(future)] == ["nightly"]
    # advancing resets the next fire time
    store.mark_ran("nightly", future)
    assert store.get("nightly")["last_run_at"] is not None
    assert store.due(future) == []  # no longer due immediately after running


def test_disabled_schedule_is_never_due():
    store = ScheduleStore(":memory:")
    store.create({"label": "off", "interval_sec": 1, "enabled": False})
    far = datetime.now(timezone.utc) + timedelta(days=1)
    assert store.due(far) == []


# -- endpoints ----------------------------------------------------------------

@pytest.fixture()
def client():
    m.schedule_store = ScheduleStore(":memory:")
    return TestClient(m.app)


def test_schedule_crud_and_validation(client):
    # validation: need a cadence
    bad = client.post("/schedules", json={"label": "x", "request": {}})
    assert bad.status_code == 400

    created = client.post("/schedules", json={
        "label": "nightly smoke", "interval_sec": 86400,
        "request": {"environment_name": "mock"}})
    assert created.status_code == 201
    sid = created.json()["id"]

    assert any(s["id"] == sid for s in client.get("/schedules").json())
    assert client.delete(f"/schedules/{sid}").status_code == 204
    assert all(s["id"] != sid for s in client.get("/schedules").json())


def test_run_now_triggers(client, monkeypatch):
    created = client.post("/schedules", json={
        "label": "now", "interval_sec": 3600, "request": {"environment_name": "mock"}})
    sid = created.json()["id"]

    async def fake_trigger(sched):
        assert sched["id"] == sid
        return "run-xyz"

    monkeypatch.setattr(m, "_trigger_schedule", fake_trigger)
    r = client.post(f"/schedules/{sid}/run")
    assert r.status_code == 200 and r.json()["run_id"] == "run-xyz"
