"""Hub-wide quotas (ADR-0010 phase 5): one ledger for every agent's tokens,
one cap on concurrent heartbeats, and the load rate a heartbeat may start by
itself. All enforced in machinery: the wake path and the tool layer."""

import json
import threading
import time

from hub_testkit import FakeBackend, wire_fakes
from payprobe_common import agent_toolkit as tk

from agent_hub import quotas as q
from agent_hub.llm import FakeLLMBackend
from agent_hub.models import AgentSpec
from agent_hub.quotas import Quotas
from agent_hub.runner import scope_for
from agent_hub.seed import SEEDS

# -- env parsing ---------------------------------------------------------------


def test_defaults_and_env_override(monkeypatch):
    for k in ("AGENT_HUB_DAILY_TOKENS", "AGENT_HUB_MAX_CONCURRENT", "AGENT_LOAD_APPROVAL_TPS"):
        monkeypatch.delenv(k, raising=False)
    d = Quotas.from_env()
    assert (d.daily_tokens, d.max_concurrent, d.load_approval_tps) == (5_000_000, 4, 100)
    monkeypatch.setenv("AGENT_HUB_DAILY_TOKENS", "0")
    monkeypatch.setenv("AGENT_HUB_MAX_CONCURRENT", "2")
    monkeypatch.setenv("AGENT_LOAD_APPROVAL_TPS", "not a number")
    e = Quotas.from_env()
    assert e.daily_tokens == 0 and e.as_dict()["daily_tokens"] is None  # 0 = off
    assert e.max_concurrent == 2
    assert e.load_approval_tps == 100  # garbage falls back to the default
    assert e.tokens_refusal(10**9) is None  # off never refuses
    assert "2" in (e.concurrency_refusal(2) or "")
    assert e.concurrency_refusal(1) is None


# -- load cap in the tool layer -----------------------------------------------


class LoadBackend(FakeBackend):
    def start_load_run(self, spec):
        self.calls.append(("start_load_run", spec.get("target_tps")))
        return {"run_id": "load-1", "status": "running"}


def _scope(cap):
    return tk.ToolScope(
        allow=frozenset({"start_load_run"}), mode="full", projects=("*",),
        environments=("*",), load_tps_cap=cap,
    )


def test_requested_tps_takes_the_peak_of_every_rate_knob():
    assert tk.requested_tps({}) == 0
    assert tk.requested_tps({"target_tps": 50}) == 50
    assert tk.requested_tps({"target_tps": 50, "extra": {"spike_tps": 400}}) == 400
    assert tk.requested_tps({"target_tps": "75"}) == 75
    assert tk.requested_tps({"target_tps": True}) == 0  # a bool is not a rate


def test_heavy_load_is_refused_by_the_tool_layer_and_light_load_passes():
    be = LoadBackend()
    ctx = tk.ToolContext(backend=be)
    heavy = {"scenario_ids": ["s"], "environment_name": "mock", "target_tps": 500}
    out = tk.scoped_dispatch(ctx, _scope(100), "start_load_run", heavy)
    assert out["ok"] is False and out["guardrail"] is True
    assert "500 tps exceeds" in out["error"] and "approved workflow step" in out["error"]
    assert be.calls == []  # nothing fired
    light = {**heavy, "target_tps": 80}
    out = tk.scoped_dispatch(ctx, _scope(100), "start_load_run", light)
    assert out["ok"] is True and ("start_load_run", 80) in be.calls
    # a ramp whose end rate is heavy is heavy
    ramp = {**light, "extra": {"end_tps": 1000}}
    assert tk.scoped_dispatch(ctx, _scope(100), "start_load_run", ramp)["ok"] is False
    # no cap (the engine's approved tool node) → anything goes
    assert tk.scoped_dispatch(ctx, _scope(None), "start_load_run", heavy)["ok"] is True


def test_runner_scope_carries_the_env_cap(monkeypatch):
    spec = AgentSpec.model_validate(SEEDS["plan-executor"])
    monkeypatch.setenv("AGENT_LOAD_APPROVAL_TPS", "25")
    assert scope_for(spec).load_tps_cap == 25
    monkeypatch.setenv("AGENT_LOAD_APPROVAL_TPS", "0")
    assert scope_for(spec).load_tps_cap is None
    assert q.load_approval_tps() is None


# -- wake path ----------------------------------------------------------------


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_hub_wide_token_ceiling_refuses_every_agent(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend([], final="ok"))
    hb = _wait_done(client, client.post("/agents/observer/wake", json={}).json()["id"])
    spent = hb["tokens_in"] + hb["tokens_out"]
    assert spent > 0
    client.app.state.quotas = Quotas(daily_tokens=spent)  # the ledger is now full
    r = client.post("/agents/reviewer/wake", json={"input": "x"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["status"] == "budget_exceeded"
    assert "hub-wide daily token ceiling" in row["error"] and "AGENT_HUB_DAILY_TOKENS" in row["error"]
    assert row["agent"] == "reviewer"  # a different agent: the ceiling is shared
    health = client.get("/health").json()["quotas"]
    assert health["daily_tokens"] == spent and health["tokens_today"] >= spent
    client.app.state.quotas = Quotas(daily_tokens=0)  # off again
    r = client.post("/agents/reviewer/wake", json={"input": "x"})
    assert r.status_code == 202


class Gate(FakeLLMBackend):
    """Blocks the first completion until released, so a heartbeat stays running."""

    def __init__(self, release: threading.Event, **kw):
        super().__init__([], **kw)
        self._release = release

    def complete(self, convo, tools):
        self._release.wait(5)
        return super().complete(convo, tools)


def test_concurrency_cap_refuses_and_records_while_another_runs(client):
    release = threading.Event()
    gate = Gate(release, final="ok")
    wire_fakes(
        client.app, FakeBackend(),
        lambda spec: gate if "observer" in spec.role.lower() else FakeLLMBackend([], final="ok"),
    )
    client.app.state.quotas = Quotas(max_concurrent=1)
    try:
        first = client.post("/agents/observer/wake", json={})
        assert first.status_code == 202 and first.json()["status"] == "running"
        assert client.get("/health").json()["quotas"]["running"] == 1
        second = client.post("/agents/reviewer/wake", json={"input": "x"})
        assert second.status_code == 200, second.text
        row = second.json()
        assert row["status"] == "quota_exceeded"
        assert "AGENT_HUB_MAX_CONCURRENT=1" in row["error"]
        # the refusal is a recorded heartbeat, visible in history
        assert row["id"] in {h["id"] for h in client.get("/heartbeats?agent=reviewer").json()}
        # the same agent waking again coalesces instead (no new heartbeat, no refusal)
        again = client.post("/agents/observer/wake", json={})
        assert again.status_code == 200 and again.json().get("coalesced") is True
    finally:
        release.set()
    _wait_done(client, first.json()["id"])
    client.app.state.quotas = Quotas(max_concurrent=0)
    assert client.post("/agents/reviewer/wake", json={"input": "x"}).status_code == 202


def test_health_reports_the_quotas_in_force(client):
    client.app.state.quotas = Quotas.from_env()
    quotas = client.get("/health").json()["quotas"]
    assert set(quotas) == {"daily_tokens", "max_concurrent", "load_approval_tps",
                           "tokens_today", "running"}


# -- seed defaults (ADR action item 6) ----------------------------------------


def test_every_builtin_carries_a_daily_token_budget():
    for name, raw in SEEDS.items():
        spec = AgentSpec.model_validate(raw)
        assert spec.budget.daily_tokens, f"{name} has no daily budget"
        assert spec.budget.hard_stop is True
    # the scheduled reader spends the most; the executor the least
    assert SEEDS["observer"]["budget"]["daily_tokens"] > SEEDS["failure-triage"]["budget"]["daily_tokens"]
    assert SEEDS["plan-executor"]["budget"]["daily_tokens"] < SEEDS["config"]["budget"]["daily_tokens"]
    # every seed fits under the hub-wide default with room for the rest
    assert max(s["budget"]["daily_tokens"] for s in SEEDS.values()) < q.DEFAULT_DAILY_TOKENS


def test_quota_refusals_alert_like_budget_stops():
    from agent_hub.alerts import ALERT_STATUSES, events_for

    assert "quota_exceeded" in ALERT_STATUSES
    hb = {"id": "h", "agent": "a", "status": "quota_exceeded", "error": "x", "result": None}
    assert [e for e, _ in events_for(hb, "advisor")] == ["heartbeat.quota_exceeded"]
    assert json.dumps(events_for(hb, "advisor")[0][1], default=str)  # serialisable payload
