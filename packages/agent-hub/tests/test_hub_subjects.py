"""Heartbeat subjects: how a run report finds the agent verdicts attached to it."""

import json
import time

from agent_hub.llm import FakeLLMBackend
from agent_hub.main import _infer_subject
from hub_testkit import FakeBackend, agent_spec, wire_fakes


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_infer_subject_from_json_input_only():
    assert _infer_subject('{"run_id": "1dba87d7-7b22", "status": "failed"}') == "run:1dba87d7-7b22"
    assert _infer_subject('  {"run_id": "r/1"}') == "run:r/1"
    assert _infer_subject('{"run_id": ""}') is None
    assert _infer_subject('{"run_id": 7}') is None
    assert _infer_subject("look at run 1dba87d7") is None  # prose is not a subject
    assert _infer_subject("[1, 2]") is None
    assert _infer_subject('{"run_id": "' + "x" * 200 + '"}') is None  # too long to index


def test_wake_records_explicit_or_inferred_subject_and_lists_by_it(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="ok"))
    inferred = client.post(
        "/agents/failure-triage/wake",
        json={"input": json.dumps({"run_id": "run-42", "status": "failed"})},
    ).json()
    explicit = client.post(
        "/agents/observer/wake", json={"input": "please look", "subject": "run:run-42"}
    ).json()
    untargeted = client.post("/agents/observer/wake", json={"input": "general look"}).json()
    for hb in (inferred, explicit, untargeted):
        _wait_done(client, hb["id"])
    assert client.get(f"/heartbeats/{inferred['id']}").json()["subject"] == "run:run-42"
    assert client.get(f"/heartbeats/{explicit['id']}").json()["subject"] == "run:run-42"
    assert client.get(f"/heartbeats/{untargeted['id']}").json()["subject"] is None

    by_run = client.get("/heartbeats?subject=run:run-42").json()
    assert {h["id"] for h in by_run} == {inferred["id"], explicit["id"]}
    assert all(h["subject"] == "run:run-42" for h in by_run)
    assert client.get("/heartbeats?subject=run:other").json() == []
    # a bad subject shape is refused at the wake, not stored
    r = client.post("/agents/observer/wake", json={"input": "x", "subject": "not a subject"})
    assert r.status_code == 422


def test_event_wakes_carry_the_run_subject(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="[]"))
    r = client.post("/events", json={"event": "run.failed", "subject": {"run_id": "run-9"}})
    assert r.status_code == 202
    for w in r.json()["woken"]:
        hb = _wait_done(client, w["heartbeat_id"])
        assert hb["subject"] == "run:run-9", hb["agent"]
    listed = client.get("/heartbeats?subject=run:run-9").json()
    assert {h["agent"] for h in listed} == {"observer", "failure-triage"}


def test_workflow_agent_tasks_are_attached_to_their_run(client):
    r = client.post("/agents", json={"name": "obs2", "spec": agent_spec(role="Obs2")})
    assert r.status_code == 201, r.text
    assert client.post("/agents/obs2/versions/1/publish").status_code == 200
    wf = {
        "nodes": [{"id": "look", "type": "agent_task", "agent": "obs2"}],
        "edges": [{"from": "look", "to": "end"}],
    }
    r = client.post("/workflows", json={"name": "w-subj", "spec": wf})
    assert r.status_code == 201, r.text
    assert client.post("/workflows/w-subj/versions/1/publish").status_code == 200
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="ok"))
    run = client.post("/workflows/w-subj/run", json={"inputs": {}}).json()
    t0 = time.time()
    while time.time() - t0 < 5 and client.get(f"/runs/{run['id']}").json()["status"] == "running":
        time.sleep(0.02)
    hbs = client.get(f"/heartbeats?subject=wfrun:{run['id']}").json()
    assert [h["agent"] for h in hbs] == ["obs2"]
