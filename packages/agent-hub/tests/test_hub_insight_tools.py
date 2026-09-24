"""The insight service and the orchestrator's run history as first-class agent
tools (ADR-0010 phase 4 leftover): read tools wrapped as untrusted, one
execute-tier training pass that only plan/full agents may reach."""

import time

from hub_testkit import FakeBackend, wire_fakes
from payprobe_common import agent_toolkit as tk
from payprobe_common.rest_backend import RestBackend

from agent_hub.llm import FakeLLMBackend
from agent_hub.models import AgentSpec
from agent_hub.seed import SEEDS

NEW_READS = {
    "insight_status", "get_scenario_prediction", "list_insight_categories",
    "run_trend", "run_flakiness",
}


def test_rest_backend_urls_are_clean_and_hit_the_right_service():
    seen: list[tuple[str, str, object]] = []

    def fake_request(method, url, body=None, **kw):
        seen.append((method, url, body))
        return {}

    be = RestBackend(fake_request, "http://s", "http://r", "http://i")
    be.insight_status()
    be.get_scenario_prediction("scn 1", "mock env")
    be.list_insight_categories()
    be.train_insights()
    be.run_trend(7, "nightly · issuer")
    be.run_flakiness(14, None, 2)
    urls = [u for _, u, _ in seen]
    assert urls[0] == "http://i/status"
    assert urls[1] == "http://i/insights/predictions/scn%201?environment=mock%20env"
    assert urls[2] == "http://i/insights/categories"
    assert seen[3][:2] == ("POST", "http://i/train") and seen[3][2] == {}
    assert urls[4] == "http://r/runs/trend?days=7&label=nightly%20%C2%B7%20issuer"
    assert urls[5] == "http://r/runs/flakiness?days=14&min_runs=2"
    for u in urls:
        assert " " not in u


def test_new_reads_are_untrusted_and_registered_as_read_tier():
    assert NEW_READS <= tk.UNTRUSTED_RESULT_TOOLS
    assert "train_insights" in tk.UNTRUSTED_RESULT_TOOLS
    for name in NEW_READS:
        assert tk.REGISTRY[name].tier == "read", name
    assert tk.REGISTRY["train_insights"].tier == "execute"


def _scope(mode, tools):
    return tk.ToolScope(allow=frozenset(tools), mode=mode, projects=("*",), environments=("*",))


def test_reads_dispatch_wrapped_and_a_missing_prediction_is_a_clean_error():
    be = FakeBackend()
    ctx = tk.ToolContext(backend=be)
    out = tk.scoped_dispatch(ctx, _scope("advisor", NEW_READS), "run_flakiness",
                             {"days": 7, "min_runs": 2})
    assert out["ok"] and out["result"]["kind"] == "untrusted"
    assert out["result"]["source"] == "run_flakiness"
    assert ("run_flakiness", 7, None, 2) in be.calls
    out = tk.scoped_dispatch(ctx, _scope("advisor", NEW_READS), "get_scenario_prediction",
                             {"scenario_id": "scn-1"})
    assert out["ok"] and out["result"]["data"]["p_fail_next"] == 0.4

    class NoHistory(FakeBackend):
        def get_scenario_prediction(self, scenario_id, environment=None):
            return None

    out = tk.scoped_dispatch(tk.ToolContext(backend=NoHistory()), _scope("advisor", NEW_READS),
                             "get_scenario_prediction", {"scenario_id": "new"})
    assert out["ok"] is False and "no recorded outcomes" in out["error"]


def test_training_is_execute_tier_advisor_refused_plan_proposed_full_runs():
    be = FakeBackend()
    ctx = tk.ToolContext(backend=be)
    allow = {"train_insights"}
    advisor = tk.scoped_dispatch(ctx, _scope("advisor", allow), "train_insights", {})
    assert advisor["ok"] is False and advisor["guardrail"] is True
    plan = tk.scoped_dispatch(ctx, _scope("plan", allow), "train_insights", {})
    assert plan["ok"] is False and "describe the change in your plan" in plan["error"]
    assert ("train_insights",) not in be.calls  # nothing ran yet
    full = tk.scoped_dispatch(ctx, _scope("full", allow), "train_insights", {})
    assert full["ok"] is True and full["result"]["data"]["trained"] is True
    assert ("train_insights",) in be.calls
    assert ctx.journal.dump() == []  # execute tier: nothing to restore


def test_seeds_carry_the_tools_and_still_validate():
    for name, raw in SEEDS.items():
        spec = AgentSpec.model_validate(raw)
        if spec.mode == "advisor":
            assert "train_insights" not in spec.tools, name  # advisors cannot train
    observer = AgentSpec.model_validate(SEEDS["observer"])
    assert NEW_READS <= set(observer.tools)
    assert "run_trend" in observer.instructions and "insight_status" in observer.instructions
    assert "train_insights" in SEEDS["certification-planner"]["tools"]
    assert "train_insights" in SEEDS["plan-executor"]["tools"]


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_observer_heartbeat_uses_the_history_tools(client):
    be = FakeBackend()
    llm = FakeLLMBackend(
        [{"tool_calls": [
            {"name": "run_trend", "args": {"days": 7}},
            {"name": "run_flakiness", "args": {}},
            {"name": "insight_status", "args": {}},
        ]}],
        final='[{"severity": "info", "subject": "scn-1", "headline": "flaky"}]',
    )
    wire_fakes(client.app, be, lambda spec: llm)
    r = client.post("/agents/observer/wake", json={})
    assert r.status_code == 202, r.text
    hb = _wait_done(client, r.json()["id"])
    assert hb["status"] == "done", hb["error"]
    tools = [s["tool"] for s in hb["steps"] if s["kind"] == "tool"]
    assert tools == ["run_trend", "run_flakiness", "insight_status"]
    assert all(s["ok"] for s in hb["steps"] if s["kind"] == "tool")
    assert ("run_trend", 7, None) in be.calls
