"""General assistant agent loop — provider-neutral loop, translators, endpoint.

No network: model turns are supplied by a scripted fake caller (or monkeypatched
provider call), so we test the loop wiring, tool dispatch, the change journal,
the iteration cap, the provider translators, and the LLM-not-configured guard.
"""
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api import agent as agent_mod
from api.agent import (
    _parse_anthropic,
    _parse_openai,
    _to_anthropic_messages,
    _to_openai_messages,
    iter_agent,
    llm_ready,
    not_configured_reply,
    run_agent,
)
from api.agent_tools import ChangeJournal, ToolContext
from api.catalog_store import CatalogStore
from api.connection_store import ConnectionStore
from api.flow_store import StarterFlowStore
from api.format_store import FormatStore
from api.main import app
from api.table_store import TableStore
from api.variable_store import VariableStore


def _ctx() -> ToolContext:
    stores = SimpleNamespace(
        connections=ConnectionStore(":memory:"),
        catalog=CatalogStore(":memory:"),
        flows=StarterFlowStore(":memory:"),
        formats=FormatStore(":memory:"),
        tables=TableStore(":memory:"),
        variables=VariableStore(":memory:"),
    )
    return ToolContext(stores=stores, journal=ChangeJournal())


def _scripted(*turns):
    """A caller that returns each scripted turn in order."""
    seq = iter(turns)

    def caller(convo, tools, llm):
        return next(seq)

    return caller


LLM = {"provider": "openai", "enabled": True, "api_key": "sk-x",
       "model": "gpt-4o", "base_url": "http://x"}


# -- loop ---------------------------------------------------------------------

def test_loop_runs_read_tool_then_answers():
    ctx = _ctx()
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "list_connections", "args": {}}]},
        {"text": "You have no connections yet.", "tool_calls": []},
    )
    out = run_agent([{"role": "user", "content": "what connections do I have?"}],
                    ctx, LLM, caller=caller)
    assert out["reply"] == "You have no connections yet."
    assert out["iterations"] == 2 and out["stopped"] is False
    assert len(out["tool_calls"]) == 1 and out["tool_calls"][0]["tool"] == "list_connections"


def test_loop_write_creates_and_journals():
    ctx = _ctx()
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "switch", "config": {"host": "10.0.0.1", "port": 5000}}}]},
        {"text": "Created the connection 'switch'.", "tool_calls": []},
    )
    out = run_agent([{"role": "user", "content": "add a switch at 10.0.0.1:5000"}],
                    ctx, LLM, caller=caller)
    assert ctx.stores.connections.get("switch") is not None
    assert out["journal"] and out["journal"][0]["resource"] == "connection"
    assert out["tool_calls"][0]["ok"] is True


def test_loop_stops_at_iteration_cap():
    ctx = _ctx()

    def always_tool(convo, tools, llm):
        return {"text": "", "tool_calls": [
            {"id": "x", "name": "list_connections", "args": {}}]}

    out = run_agent([{"role": "user", "content": "loop"}], ctx, LLM,
                    caller=always_tool, max_iterations=3)
    assert out["stopped"] is True and out["iterations"] == 3


def test_advisor_mode_hides_write_tools():
    ctx = _ctx()
    captured = {}

    def caller(convo, tools, llm):
        captured["names"] = {t["name"] for t in tools}
        return {"text": "ok", "tool_calls": []}

    run_agent([{"role": "user", "content": "hi"}], ctx, LLM,
              caller=caller, tiers=("read",))
    assert "list_connections" in captured["names"]
    assert "upsert_connection" not in captured["names"]


# -- translators --------------------------------------------------------------

def test_openai_message_translation():
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "list_connections", "args": {}}]},
        {"role": "tool", "tool_call_id": "1", "name": "list_connections", "content": "[]"},
    ]
    msgs = _to_openai_messages(convo)
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "list_connections"
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "1"


def test_anthropic_splits_system_and_tool_result():
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "thinking", "tool_calls": [
            {"id": "1", "name": "list_tables", "args": {}}]},
        {"role": "tool", "tool_call_id": "1", "name": "list_tables", "content": "[]"},
    ]
    system, msgs = _to_anthropic_messages(convo)
    assert system == "sys"
    assert msgs[1]["content"][-1]["type"] == "tool_use"
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_parse_openai_and_anthropic_tool_calls():
    o = _parse_openai({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "a", "function": {"name": "get_format", "arguments": '{"fid":"x"}'}}]}}]})
    assert o["tool_calls"][0]["name"] == "get_format"
    assert o["tool_calls"][0]["args"] == {"fid": "x"}

    a = _parse_anthropic({"content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "b", "name": "list_tables", "input": {}}]})
    assert a["text"] == "ok" and a["tool_calls"][0]["name"] == "list_tables"


# -- guards / endpoint --------------------------------------------------------

def test_llm_ready_and_not_configured():
    assert llm_ready(LLM) is True
    assert llm_ready({"enabled": False}) is False
    assert not_configured_reply()["needs_config"] is True


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_iter_agent_emits_tool_then_final():
    ctx = _ctx()
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "list_connections", "args": {}}]},
        {"text": "done", "tool_calls": []},
    )
    events = list(iter_agent([{"role": "user", "content": "x"}], ctx, LLM, caller=caller))
    pairs = [(e["type"], e.get("phase")) for e in events]
    assert ("tool", "start") in pairs and ("tool", "end") in pairs
    assert events[-1]["type"] == "final" and events[-1]["reply"] == "done"
    # tool args are not leaked into the stream
    assert all("args" not in e for e in events if e["type"] == "tool")


def test_chat_stream_emits_sse_events(client, monkeypatch):
    client.put("/assist/config", json={"provider": "openai", "enabled": True,
                                       "api_key": "sk-test", "model": "gpt-4o"})
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "zeta", "config": {"port": 5005}}}]},
        {"text": "Added zeta.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_mod, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    r = client.post("/agent/chat/stream",
                    json={"messages": [{"role": "user", "content": "add zeta"}]})
    assert r.status_code == 200
    body = r.text
    assert '"type": "tool"' in body and '"type": "final"' in body
    assert '"session_id"' in body
    assert client.get("/connections/zeta").status_code == 200


def test_chat_endpoint_needs_config_by_default(client):
    # no provider configured in a fresh in-memory app
    r = client.post("/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json().get("needs_config") is True


def test_chat_endpoint_full_round_with_fake_provider(client, monkeypatch):
    client.put("/assist/config", json={"provider": "openai", "enabled": True,
                                       "api_key": "sk-test", "model": "gpt-4o"})
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "acq", "config": {"host": "h", "port": 6000}}}]},
        {"text": "Added connection 'acq'.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_mod, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    r = client.post("/agent/chat",
                    json={"messages": [{"role": "user", "content": "add acq h:6000"}]})
    body = r.json()
    assert r.status_code == 200
    assert body["reply"] == "Added connection 'acq'."
    assert body["journal"][0]["key"] == "acq"
    assert client.get("/connections/acq").status_code == 200
