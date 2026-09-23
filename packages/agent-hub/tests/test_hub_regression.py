"""The regression post-check (ADR-0010 phase 5): the platform's run history
overrides the model's `regression` claim.

Real case, 2026-09-23: failure-triage judged the first ever run of a scenario
a regression because "similar failures" existed. The history said first_run."""

import json
import time

from hub_testkit import FakeBackend, wire_fakes

from agent_hub import postcheck
from agent_hub.llm import FakeLLMBackend

EVIDENCE_FIRST_RUN = {
    "run_id": "r1",
    "verdict": "first_run",
    "regression": False,
    "regressed": [],
    "never_passed": [],
    "scenarios": [
        {
            "scenario_id": "scn-1", "name": "purchase", "status": "failed",
            "conclusion": "first_run", "prior_runs": 0, "prior_passed": 0,
            "prior_failed": 0, "last_passed_run_id": None, "last_passed_at": None,
            "failure_streak": 1,
        }
    ],
}
EVIDENCE_REGRESSION = {
    **EVIDENCE_FIRST_RUN,
    "run_id": "r2",
    "verdict": "regression",
    "regression": True,
    "regressed": ["purchase"],
    "scenarios": [
        {**EVIDENCE_FIRST_RUN["scenarios"][0], "conclusion": "regression",
         "prior_runs": 3, "prior_passed": 2, "prior_failed": 1,
         "last_passed_run_id": "r0", "failure_streak": 2}
    ],
}


def _triage(run_id: str, regression) -> str:
    return json.dumps({"run_id": run_id, "category": "assertion",
                       "root_cause": "RC 05", "regression": regression,
                       "next_step": "check the switch"})


def test_history_overrides_a_false_regression_claim_and_keeps_the_claim():
    be = FakeBackend()
    be.regression["r1"] = EVIDENCE_FIRST_RUN
    text, step = postcheck.regression(_triage("r1", True), be)
    data = json.loads(text)
    assert data["regression"] is False
    assert data["regression_evidence"]["verdict"] == "first_run"
    assert data["regression_evidence"]["claimed"] is True
    assert data["regression_evidence"]["scenarios"][0]["conclusion"] == "first_run"
    assert data["root_cause"] == "RC 05"  # the rest of the answer is untouched
    assert step["kind"] == "postcheck" and step["ok"] and step["changed"] is True
    assert step["claimed"] is True and step["verified"] is False


def test_history_confirms_a_true_claim_without_marking_a_change():
    be = FakeBackend()
    be.regression["r2"] = EVIDENCE_REGRESSION
    text, step = postcheck.regression(_triage("r2", True), be)
    assert json.loads(text)["regression"] is True
    assert step["changed"] is False and step["verdict"] == "regression"


def test_unknown_claim_is_replaced_by_the_evidence():
    be = FakeBackend()
    be.regression["r2"] = EVIDENCE_REGRESSION
    text, step = postcheck.regression(_triage("r2", "unknown"), be)
    assert json.loads(text)["regression"] is True
    assert step["changed"] is True and step["claimed"] == "unknown"


def test_markdown_wrapped_answer_is_checked_too():
    be = FakeBackend()
    be.regression["r1"] = EVIDENCE_FIRST_RUN
    wrapped = "Here is my triage:\n```json\n" + _triage("r1", True) + "\n```\n"
    text, step = postcheck.regression(wrapped, be)
    assert json.loads(text)["regression"] is False and step["changed"]


def test_answers_without_a_run_or_a_claim_are_left_alone():
    be = FakeBackend()
    for text in (
        None,
        "",
        "plain prose, no JSON",
        json.dumps({"findings": [{"severity": "warn"}]}),  # observer style
        json.dumps({"run_id": "r1"}),  # no claim to check
        json.dumps({"regression": True}),  # no run to check against
        json.dumps({"run_id": "", "regression": True}),
    ):
        out, step = postcheck.regression(text, be)
        assert out == text and step is None
    assert be.calls == []  # nothing was looked up


def test_unknown_run_keeps_the_claim_and_records_the_miss():
    be = FakeBackend()  # no evidence registered → backend answers None
    text, step = postcheck.regression(_triage("ghost", True), be)
    assert json.loads(text)["regression"] is True  # untouched
    assert step["ok"] is False and step["changed"] is False
    assert "not found" in step["error"]


def test_backend_failure_never_raises_and_keeps_the_answer():
    class Down(FakeBackend):
        def get_run_regression(self, run_id):
            raise ConnectionError("orchestrator unreachable")

    original = _triage("r1", True)
    text, step = postcheck.regression(original, Down())
    assert text == original
    assert step["ok"] is False and "ConnectionError" in step["error"]


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_heartbeat_persists_the_corrected_verdict_and_a_postcheck_step(client):
    be = FakeBackend()
    be.regression["r1"] = EVIDENCE_FIRST_RUN
    llm = FakeLLMBackend([], final=_triage("r1", True))
    wire_fakes(client.app, be, lambda spec: llm)
    r = client.post("/agents/failure-triage/wake", json={"input": '{"run_id": "r1"}'})
    assert r.status_code == 202, r.text
    hb = _wait_done(client, r.json()["id"])
    assert hb["status"] == "done", hb["error"]
    data = json.loads(hb["result"])
    assert data["regression"] is False
    assert data["regression_evidence"]["verdict"] == "first_run"
    last = hb["steps"][-1]
    assert last["kind"] == "postcheck" and last["tool"] == "get_run_regression"
    assert last["ok"] is True and last["changed"] is True
    assert last["n"] == len(hb["steps"])  # numbered after the model's steps
    assert ("get_run_regression", "r1") in be.calls
    assert hb["subject"] == "run:r1"


def test_seeded_failure_triage_may_call_the_evidence_tool(client):
    spec = client.get("/agents/failure-triage").json()["spec"]
    assert "get_run_regression" in spec["tools"]
    assert "get_run_regression" in spec["instructions"]
