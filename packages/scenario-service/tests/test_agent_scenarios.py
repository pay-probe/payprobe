"""Scenario write tools through the agent — create / update / revert.

Runs against the real (async) scenario store via TestClient, exercising the
sync-handler → async-store bridge (ctx.run_async) and persistent revert. The
model's tool calls are supplied by a monkeypatched provider (no network).
"""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api import agent as agent_mod
from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _enable_llm(client):
    client.put("/assist/config", json={"provider": "openai", "enabled": True,
                                       "api_key": "sk-test", "model": "gpt-4o"})


def _scenario(name, steps):
    return {"name": name, "description": "", "tags": ["assistant"],
            "stop_on_failure": True, "timeout_ms": 5000, "steps": steps}


def _step(sid, target="http", action="send_auth_request"):
    return {"id": sid, "target": target, "action": action,
            "payload": {}, "assertions": []}


def _provider(monkeypatch, *turns):
    it = iter(turns)
    monkeypatch.setattr(agent_mod, "provider_tool_call",
                        lambda convo, tools, llm: next(it))


def test_agent_creates_scenario_then_revert_removes_it(client, monkeypatch):
    _enable_llm(client)
    _provider(
        monkeypatch,
        {"text": "", "tool_calls": [{"id": "1", "name": "create_scenario", "args": {
            "scenario": _scenario("agent built flow", [_step("step_001")])}}]},
        {"text": "Created the scenario.", "tool_calls": []},
    )
    r = client.post("/agent/chat",
                    json={"messages": [{"role": "user", "content": "make a flow"}]})
    body = r.json()
    assert body["tool_calls"][0]["ok"] is True
    sid = body["session_id"]
    names = {s["name"] for s in client.get("/scenarios").json()}
    assert "agent built flow" in names

    rev = client.post("/agent/revert", json={"session_id": sid})
    assert rev.json()["reverted"] == 1
    names = {s["name"] for s in client.get("/scenarios").json()}
    assert "agent built flow" not in names


def test_agent_rejects_invalid_scenario(client, monkeypatch):
    _enable_llm(client)
    _provider(
        monkeypatch,
        {"text": "", "tool_calls": [{"id": "1", "name": "create_scenario", "args": {
            "scenario": _scenario("bad", [_step("dup"), _step("dup")])}}]},
        {"text": "Those step ids clash.", "tool_calls": []},
    )
    r = client.post("/agent/chat",
                    json={"messages": [{"role": "user", "content": "make a flow"}]})
    tc = r.json()["tool_calls"][0]
    assert tc["ok"] is False  # validation rejected it; nothing persisted


def test_agent_updates_scenario_then_revert_restores(client, monkeypatch):
    _enable_llm(client)
    # seed a one-step scenario via the normal API
    created = client.post("/scenarios", json={
        "scenario": _scenario("editable", [_step("step_001")])}).json()
    sid = created["id"]

    _provider(
        monkeypatch,
        {"text": "", "tool_calls": [{"id": "1", "name": "update_scenario", "args": {
            "scenario_id": sid,
            "scenario": _scenario("editable", [_step("step_001"), _step("step_002")])}}]},
        {"text": "Added a step.", "tool_calls": []},
    )
    r = client.post("/agent/chat",
                    json={"messages": [{"role": "user", "content": "add a step"}]})
    session = r.json()["session_id"]
    assert len(client.get(f"/scenarios/{sid}").json()["steps"]) == 2

    client.post("/agent/revert", json={"session_id": session})
    assert len(client.get(f"/scenarios/{sid}").json()["steps"]) == 1  # restored
