"""Phase 4 wake sources: platform events via POST /events and schedule triggers."""

import json
import time
from datetime import UTC, datetime, timedelta

from agent_hub.llm import FakeLLMBackend
from agent_hub.models import AgentSpec
from agent_hub.triggers import agents_for_event, schedule_due
from hub_testkit import FakeBackend, agent_spec, wire_fakes


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


# -- pure ----------------------------------------------------------------------------


def test_agents_for_event_matches_declared_event_triggers():
    specs = [
        ("a", 1, agent_spec(triggers=[{"kind": "event", "event": "run.failed"}])),
        ("b", 2, agent_spec(triggers=[{"kind": "manual"}])),
        (
            "c",
            1,
            agent_spec(
                triggers=[
                    {"kind": "event", "event": "gate.failed"},
                    {"kind": "event", "event": "run.failed"},
                ]
            ),
        ),
    ]
    assert [(n, v) for n, v, _ in agents_for_event(specs, "run.failed")] == [("a", 1), ("c", 1)]
    assert agents_for_event(specs, "storm.finished") == []


def test_schedule_due_interval_and_daily():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    every = AgentSpec.model_validate(
        agent_spec(triggers=[{"kind": "schedule", "interval_sec": 900}])
    )
    assert schedule_due(every, None, now)
    assert schedule_due(every, now - timedelta(seconds=900), now)
    assert not schedule_due(every, now - timedelta(seconds=899), now)
    daily = AgentSpec.model_validate(
        agent_spec(triggers=[{"kind": "schedule", "daily_at": "11:30"}])
    )
    assert schedule_due(daily, None, now)  # past today's slot, never woken
    assert schedule_due(daily, now - timedelta(hours=2), now)  # last wake was before the slot
    assert not schedule_due(daily, now - timedelta(minutes=10), now)  # already woken after it
    assert not schedule_due(daily, None, now.replace(hour=9))  # slot not reached yet
    manual = AgentSpec.model_validate(agent_spec())
    assert not schedule_due(manual, None, now)


# -- through the app -----------------------------------------------------------------


def test_run_failed_event_wakes_observer_and_failure_triage(client):
    seen: list[str] = []

    def factory(spec):
        seen.append(spec.role)
        return FakeLLMBackend(final=json.dumps([{"severity": "info", "headline": "looked"}]))

    wire_fakes(client.app, FakeBackend(), factory)
    r = client.post(
        "/events",
        json={
            "event": "run.failed",
            "subject": {"run_id": "run-1", "status": "failed", "error": "issuer timeout"},
        },
    )
    assert r.status_code == 202, r.text
    woken = r.json()["woken"]
    assert {w["agent"] for w in woken} == {"observer", "failure-triage"}  # both declare run.failed
    for w in woken:
        assert w["status"] == "running" and not w["coalesced"]
        hb = _wait_done(client, w["heartbeat_id"])
        assert hb["status"] == "done" and hb["wake"] == "event"
        assert hb["invoked_by"] == "event:run.failed"
        payload = json.loads(hb["input"])
        assert payload["event"] == "run.failed" and payload["run_id"] == "run-1"
        assert payload["at"]
    listed = client.get("/heartbeats?agent=failure-triage").json()
    assert listed[0]["wake"] == "event"


def test_unknown_event_wakes_nobody_and_paused_is_recorded(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend())
    r = client.post("/events", json={"event": "storm.finished", "subject": {}})
    assert r.status_code == 202 and r.json()["woken"] == []
    assert client.post("/events", json={"event": "Bad Event!", "subject": {}}).status_code == 422
    client.put("/pause", json={"paused": True})
    r = client.post("/events", json={"event": "run.failed", "subject": {"run_id": "r"}})
    assert r.status_code == 202
    assert {w["status"] for w in r.json()["woken"]} == {"paused"}  # refused, recorded, not run
    client.put("/pause", json={"paused": False})


def test_schedule_tick_wakes_due_agents_once_per_interval(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="ok"))
    r = client.post(
        "/agents",
        json={
            "name": "cron",
            "spec": agent_spec(triggers=[{"kind": "schedule", "interval_sec": 3600}]),
        },
    )
    assert r.status_code == 201, r.text
    assert client.post("/agents/cron/versions/1/publish").status_code == 200
    wakes = client.app.state.wakes

    first = client.portal.call(wakes.tick_schedules)
    names = [w["agent"] for w in first]
    assert "cron" in names and "observer" in names  # observer's seed has a 900 s schedule
    for w in first:
        _wait_done(client, w["heartbeat_id"])
    hb = client.get("/heartbeats?agent=cron").json()[0]
    assert hb["wake"] == "schedule" and hb["invoked_by"] == "scheduler"

    second = client.portal.call(wakes.tick_schedules)
    assert second == []  # nothing is due again within the interval

    client.put("/pause", json={"paused": True})
    far = datetime.now(UTC) + timedelta(days=1)
    assert client.portal.call(wakes.tick_schedules, far) == []  # paused: no refusal spam
    client.put("/pause", json={"paused": False})
    later = client.portal.call(wakes.tick_schedules, far)
    assert "cron" in [w["agent"] for w in later]
    for w in later:
        _wait_done(client, w["heartbeat_id"])
    assert wakes.stats()["scheduled"] >= 3
