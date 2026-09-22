"""Heartbeats over HTTP: wake, poll, coalesce, cancel, revert, refusals, OBO."""

import threading
import time

import jwt
from agent_hub.llm import FakeLLMBackend
from hub_testkit import FakeBackend, agent_spec, wire_fakes


def _publish(client, name, **over):
    r = client.post("/agents", json={"name": name, "spec": agent_spec(**over)})
    assert r.status_code == 201, r.text
    assert client.post(f"/agents/{name}/versions/1/publish").status_code == 200


def _wait(client, hb_id, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_wake_runs_to_done_and_is_recorded(client):
    be = FakeBackend()
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}]}], final="one failed")
    wire_fakes(client.app, be, lambda spec: llm)
    r = client.post("/agents/observer/wake", json={"input": "anything wrong?"})
    assert r.status_code == 202, r.text
    row = r.json()
    assert row["status"] == "running" and row["agent"] == "observer" and row["version"] == 1
    hb = _wait(client, row["id"])
    assert hb["status"] == "done" and hb["result"] == "one failed"
    assert hb["tokens_in"] == 200 and hb["model"] == "fake"
    assert [s["kind"] for s in hb["steps"]] == ["llm", "tool", "llm"]
    assert hb["spec_sha256"] == client.get("/agents/observer").json()["versions"][0]["spec_sha256"]
    listed = client.get("/heartbeats?agent=observer").json()
    assert listed[0]["id"] == row["id"] and listed[0]["n_steps"] == 3


def test_wake_pins_a_version_and_404s_on_unrunnable(client):
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend())
    assert client.post("/agents/observer/wake", json={"version": 9}).status_code == 404
    assert client.post("/agents/nope/wake", json={}).status_code == 404
    r = client.post("/agents/observer/wake", json={"version": 1})
    assert r.status_code == 202 and r.json()["version"] == 1
    _wait(client, r.json()["id"])


def test_paused_and_budget_refusals_are_recorded_not_run(client):
    calls = {"n": 0}

    def factory(spec):
        calls["n"] += 1
        return FakeLLMBackend(tokens_per_turn=600)  # 750 tokens per wake

    wire_fakes(client.app, FakeBackend(), factory)
    client.put("/pause", json={"paused": True})
    r = client.post("/agents/observer/wake", json={})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert calls["n"] == 0
    client.put("/pause", json={"paused": False})

    _publish(client, "thrifty", budget={"daily_tokens": 1000, "hard_stop": True})
    # 750 synthetic tokens per wake: the second wake must be refused
    for _ in range(5):
        r = client.post("/agents/thrifty/wake", json={})
        hb = _wait(client, r.json()["id"]) if r.json()["status"] == "running" else r.json()
        if hb["status"] == "budget_exceeded":
            break
    else:
        raise AssertionError("budget never tripped")
    assert "daily token budget (1000) spent" in hb["error"]


def test_running_heartbeat_coalesces_and_cancel_stops_it(client):
    gate = threading.Event()

    class Slow(FakeLLMBackend):
        def complete(self, convo, tools):
            gate.wait(5)  # first turn blocks until released
            return super().complete(convo, tools)

    slow = Slow([{"tool_calls": [{"name": "list_runs"}]}] * 3)
    wire_fakes(client.app, FakeBackend(), lambda spec: slow)
    first = client.post("/agents/observer/wake", json={}).json()
    assert first["status"] == "running"
    again = client.post("/agents/observer/wake", json={})
    assert again.status_code == 200 and again.json()["coalesced"] is True
    assert again.json()["id"] == first["id"]

    r = client.post(f"/heartbeats/{first['id']}/cancel")
    assert r.status_code == 200 and r.json()["cancel_requested"] is True
    gate.set()
    hb = _wait(client, first["id"])
    assert hb["status"] == "cancelled"
    # a finished heartbeat cannot be cancelled again
    assert client.post(f"/heartbeats/{first['id']}/cancel").status_code == 409


def test_full_mode_write_can_be_reverted_from_the_journal(client):
    be = FakeBackend()
    llm = FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {
                        "name": "upsert_connection",
                        "args": {"name": "switch", "config": {"port": 9999}},
                    }
                ]
            }
        ]
    )
    wire_fakes(client.app, be, lambda spec: llm)
    _publish(
        client,
        "editor",
        mode="full",
        tools=["upsert_connection"],
        write_scope={"projects": ["*"], "environments": []},
    )
    hb = _wait(client, client.post("/agents/editor/wake", json={}).json()["id"])
    assert hb["status"] == "done" and be.connections["switch"]["port"] == 9999
    assert len(hb["journal"]) == 1
    r = client.post(f"/heartbeats/{hb['id']}/revert")
    assert r.status_code == 200 and r.json()["reverted"] == 1
    assert be.connections["switch"]["port"] == 9000
    assert r.json()["reverted_at"] and r.json()["journal"] == []
    assert client.post(f"/heartbeats/{hb['id']}/revert").status_code == 409  # nothing left


def test_plan_mode_heartbeat_carries_the_proposed_calls(client):
    llm = FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_scenario",
                        "args": {"scenario": {"name": "x"}, "project_id": "p1"},
                    }
                ]
            }
        ]
    )
    wire_fakes(client.app, FakeBackend(), lambda spec: llm)
    hb = _wait(
        client, client.post("/agents/scenario-author/wake", json={"input": "author x"}).json()["id"]
    )
    assert hb["status"] == "done"
    assert hb["proposed"][0]["tool"] == "create_scenario"
    assert client.get("/heartbeats?agent=scenario-author").json()[0]["n_proposed"] == 1


def test_default_llm_factory_503s_when_unconfigured(client, monkeypatch):
    for k in ("ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSIST_SETTINGS_LLM", "0")
    r = client.post("/agents/observer/wake", json={})
    assert r.status_code == 503 and "no LLM provider configured" in r.text
    assert client.get("/heartbeats?agent=observer").json() == []  # nothing recorded


def test_obo_token_carries_user_and_act_claims(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    now = int(time.time())
    user = jwt.encode(
        {"sub": "olga", "roles": ["operator"], "project_ids": ["p1"], "iat": now, "exp": now + 300},
        "k",
        algorithm="HS256",
    )
    seen = {}

    def backend_factory(token):
        seen["claims"] = jwt.decode(token, "k", algorithms=["HS256"])
        return FakeBackend()

    client.app.state.backend_factory = backend_factory
    client.app.state.llm_factory = lambda spec: FakeLLMBackend()
    r = client.post("/agents/observer/wake", json={}, headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 202, r.text
    hb = _wait_auth(client, r.json()["id"], user)
    assert hb["status"] == "done" and hb["invoked_by"] == "olga"
    c = seen["claims"]
    assert c["sub"] == "olga" and c["roles"] == ["operator"] and c["project_ids"] == ["p1"]
    assert c["act"] == {"agent": "observer", "version": 1, "heartbeat": hb["id"]}
    assert "svc" not in c
    assert c["exp"] - c["iat"] == 300 + 60  # observer wall_clock_s + 60


def test_invoke_roles_gate_wake_and_revert_needs_admin(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    now = int(time.time())
    viewer = jwt.encode(
        {"sub": "v", "roles": ["viewer"], "iat": now, "exp": now + 300}, "k", algorithm="HS256"
    )
    op = jwt.encode(
        {"sub": "o", "roles": ["operator"], "iat": now, "exp": now + 300}, "k", algorithm="HS256"
    )
    client.app.state.backend_factory = lambda token: FakeBackend()
    client.app.state.llm_factory = lambda spec: FakeLLMBackend()
    assert (
        client.post(
            "/agents/observer/wake", json={}, headers={"Authorization": f"Bearer {viewer}"}
        ).status_code
        == 403
    )
    r = client.post("/agents/observer/wake", json={}, headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 202
    hb = _wait_auth(client, r.json()["id"], op)
    assert (
        client.post(
            f"/heartbeats/{hb['id']}/revert", headers={"Authorization": f"Bearer {op}"}
        ).status_code
        == 403
    )


def _wait_auth(client, hb_id, token, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}", headers={"Authorization": f"Bearer {token}"}).json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")
