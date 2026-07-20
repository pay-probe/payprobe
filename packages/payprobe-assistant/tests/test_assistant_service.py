"""Loop + endpoints: scripted provider (no network) over the fake backend."""
import os

os.environ.setdefault("ASSIST_LLM_API_KEY", "sk-test")
os.environ.setdefault("ASSIST_LLM_MODEL", "gpt-4o")

import pytest
from fastapi.testclient import TestClient

from assistant_service import loop
from assistant_service.loop import iter_agent, run_agent
from assistant_service.main import app
from assistant_service.tools import ChangeJournal, ToolContext

# the `backend` fixture (fake REST surface) is defined in conftest.py

LLM = {"provider": "openai", "enabled": True, "api_key": "k",
       "model": "gpt-4o", "base_url": "http://x"}


def _scripted(*turns):
    seq = iter(turns)
    return lambda convo, tools, llm: next(seq)


def test_loop_read_then_answer(backend):
    ctx = ToolContext(journal=ChangeJournal())
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "list_connections", "args": {}}]},
        {"text": "No connections yet.", "tool_calls": []},
    )
    out = run_agent([{"role": "user", "content": "what do I have?"}], ctx, LLM,
                    caller=caller)
    assert out["reply"] == "No connections yet."
    assert out["tool_calls"][0]["tool"] == "list_connections"


def test_loop_write_journals(backend):
    ctx = ToolContext(journal=ChangeJournal())
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "sw", "config": {"port": 5000}}}]},
        {"text": "done", "tool_calls": []},
    )
    out = run_agent([{"role": "user", "content": "add sw"}], ctx, LLM, caller=caller)
    assert "sw" in backend.connections
    assert out["journal"][0]["resource"] == "connection"


def test_iter_agent_emits_tool_then_final(backend):
    ctx = ToolContext(journal=ChangeJournal())
    caller = _scripted(
        {"text": "", "tool_calls": [{"id": "1", "name": "list_connections", "args": {}}]},
        {"text": "done", "tool_calls": []},
    )
    events = list(iter_agent([{"role": "user", "content": "x"}], ctx, LLM, caller=caller))
    pairs = [(e["type"], e.get("phase")) for e in events]
    assert ("tool", "start") in pairs and ("tool", "end") in pairs
    assert events[-1]["type"] == "final" and events[-1]["reply"] == "done"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["service"] == "payprobe-assistant"


def test_chat_then_revert(client, backend, monkeypatch):
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "alpha", "config": {"port": 5001}}}]},
        {"text": "Added alpha.", "tool_calls": []},
    ])
    monkeypatch.setattr(loop, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    r = client.post("/agent/chat",
                    json={"messages": [{"role": "user", "content": "add alpha"}]})
    body = r.json()
    assert body["reply"] == "Added alpha."
    sid = body["session_id"]
    assert "alpha" in backend.connections

    rev = client.post("/agent/revert", json={"session_id": sid})
    assert rev.json()["reverted"] == 1
    assert "alpha" not in backend.connections


def test_chat_stream_emits_sse_events(client, backend, monkeypatch):
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "beta", "config": {"port": 5002}}}]},
        {"text": "Added beta.", "tool_calls": []},
    ])
    monkeypatch.setattr(loop, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    r = client.post("/agent/chat/stream",
                    json={"messages": [{"role": "user", "content": "add beta"}]})
    assert r.status_code == 200
    body = r.text
    assert '"type": "tool"' in body and '"type": "final"' in body
    assert '"session_id"' in body
    assert "beta" in backend.connections


def test_title_returns_cleaned_llm_text(client, monkeypatch):
    from assistant_service import explain

    monkeypatch.setattr(explain, "completion",
                        lambda system, user, llm: '  "Add TCP switch connection."  ')
    r = client.post("/agent/title", json={"messages": [
        {"role": "user", "content": "add a tcp connection called switch"},
        {"role": "assistant", "content": "Done — created 'switch'."},
    ]})
    assert r.json()["title"] == "Add TCP switch connection."


def test_title_provider_error_falls_back_empty(client, monkeypatch):
    from assistant_service import explain

    def boom(system, user, llm):
        raise RuntimeError("HTTP 502 upstream")

    monkeypatch.setattr(explain, "completion", boom)
    r = client.post("/agent/title", json={"messages": [
        {"role": "user", "content": "hello"}]})
    body = r.json()
    assert body["title"] == "" and "error" in body


def test_title_empty_messages(client):
    r = client.post("/agent/title", json={"messages": []})
    assert r.json()["title"] == ""


# -- plan mode: propose (read-only) then apply ---------------------------------

PLAN_BLOCK = ('Here is what I would do.\n```json\n{"plan": [{"tool": '
              '"upsert_connection", "args": {"name": "pl1", "config": '
              '{"port": 7001}}, "summary": "create pl1"}]}\n```')


def test_extract_plan_splits_reply():
    clean, plan = loop.extract_plan(PLAN_BLOCK)
    assert clean == "Here is what I would do."
    assert plan == [{"tool": "upsert_connection",
                     "args": {"name": "pl1", "config": {"port": 7001}},
                     "summary": "create pl1"}]


def test_extract_plan_tolerates_no_plan():
    assert loop.extract_plan("just prose") == ("just prose", [])
    bad = "```json\n{not json}\n```"
    assert loop.extract_plan(bad) == (bad, [])


def test_plan_mode_blocks_writes_and_returns_plan(client, backend, monkeypatch):
    turns = iter([
        # the model tries a write anyway — the dispatch gate must refuse it
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "sneaky", "config": {"port": 1}}}]},
        {"text": PLAN_BLOCK, "tool_calls": []},
    ])
    monkeypatch.setattr(loop, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    r = client.post("/agent/chat", json={
        "messages": [{"role": "user", "content": "add pl1"}], "mode": "plan"})
    body = r.json()
    assert "sneaky" not in backend.connections          # write blocked
    assert body["tool_calls"][0]["ok"] is False
    assert body["tool_calls"][0]["guardrail"] is True
    assert body["journal"] == []                        # nothing to revert
    assert body["reply"] == "Here is what I would do."
    assert body["plan"][0]["tool"] == "upsert_connection"


def test_apply_executes_journalled(client, backend):
    r = client.post("/agent/apply", json={"actions": [
        {"tool": "upsert_connection",
         "args": {"name": "pl2", "config": {"port": 7002}}}]})
    body = r.json()
    assert backend.connections["pl2"]["port"] == 7002
    assert body["results"][0]["ok"] is True
    assert body["journal"][0]["resource"] == "connection"
    # applied changes are revertible like any other
    rev = client.post("/agent/revert",
                      json={"session_id": body["session_id"]})
    assert rev.json()["reverted"] == 1
    assert "pl2" not in backend.connections


def test_apply_refuses_read_tools(client, backend):
    r = client.post("/agent/apply", json={"actions": [
        {"tool": "list_connections", "args": {}}]})
    body = r.json()
    assert body["results"][0]["ok"] is False
    assert body["journal"] == []


# -- direct read-tool dispatch (slash commands / mention catalog) ---------------

def test_agent_tool_read_ok(client, backend):
    backend.connections["t1"] = {"port": 9}
    r = client.post("/agent/tool", json={"tool": "list_connections"})
    body = r.json()
    assert body["ok"] is True
    assert any(c["name"] == "t1" for c in body["result"])


def test_agent_tool_refuses_writes(client, backend):
    r = client.post("/agent/tool", json={
        "tool": "upsert_connection",
        "args": {"name": "nope", "config": {"port": 1}}})
    assert r.json()["ok"] is False
    assert "nope" not in backend.connections


# -- server-side chat history ----------------------------------------------------

def test_chats_crud(client):
    doc = {"title": "Add switch", "mode": "full", "turns": [
        {"role": "user", "content": "hi"}], "updatedAt": 123}
    assert client.put("/agent/chats/c1", json={"id": "c1", **doc}).json()["ok"]
    client.put("/agent/chats/c2", json={"id": "c2", "title": "Two"})
    chats = client.get("/agent/chats").json()["chats"]
    assert [c["id"] for c in chats] == ["c2", "c1"]     # newest first
    assert chats[1]["title"] == "Add switch"

    client.delete("/agent/chats/c1")
    assert [c["id"] for c in client.get("/agent/chats").json()["chats"]] == ["c2"]
    client.delete("/agent/chats")
    assert client.get("/agent/chats").json()["chats"] == []


# -- per-change revert + before/after snapshots --------------------------------

def _chat_write(client, backend, monkeypatch, name, port, session_id=None):
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": name, "config": {"port": port}}}]},
        {"text": f"Added {name}.", "tool_calls": []},
    ])
    monkeypatch.setattr(loop, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))
    body = {"messages": [{"role": "user", "content": f"add {name}"}]}
    if session_id:
        body["session_id"] = session_id
    return client.post("/agent/chat", json=body).json()


def test_journal_records_capture_after(client, backend, monkeypatch):
    out = _chat_write(client, backend, monkeypatch, "gamma", 6001)
    sid = out["session_id"]
    changes = client.get(f"/agent/session/{sid}/changes").json()["changes"]
    assert len(changes) == 1
    rec = changes[0]
    assert rec["before"] is None                 # didn't exist
    assert rec["after"]["port"] == 6001          # snapshot of the write


def test_revert_one_undoes_single_change(client, backend, monkeypatch):
    out = _chat_write(client, backend, monkeypatch, "delta1", 6002)
    sid = out["session_id"]
    out2 = _chat_write(client, backend, monkeypatch, "delta2", 6003, session_id=sid)
    assert out2["session_id"] == sid
    assert "delta1" in backend.connections and "delta2" in backend.connections

    changes = client.get(f"/agent/session/{sid}/changes").json()["changes"]
    seq1 = changes[0]["seq"]
    r = client.post("/agent/revert-one", json={"session_id": sid, "seq": seq1})
    assert r.json()["reverted"] == 1
    assert "delta1" not in backend.connections
    assert "delta2" in backend.connections       # untouched
    remaining = client.get(f"/agent/session/{sid}/changes").json()["changes"]
    assert [c["seq"] for c in remaining] != [c["seq"] for c in changes]
    assert len(remaining) == 1


def test_revert_one_blocked_by_newer_same_key(client, backend, monkeypatch):
    out = _chat_write(client, backend, monkeypatch, "eps", 6004)
    sid = out["session_id"]
    _chat_write(client, backend, monkeypatch, "eps", 6005, session_id=sid)
    changes = client.get(f"/agent/session/{sid}/changes").json()["changes"]
    older = min(c["seq"] for c in changes)
    r = client.post("/agent/revert-one", json={"session_id": sid, "seq": older})
    body = r.json()
    assert body["reverted"] == 0 and "newer change" in body["error"]
    assert backend.connections["eps"]["port"] == 6005  # untouched


def test_revert_one_unknown_seq(client, backend, monkeypatch):
    out = _chat_write(client, backend, monkeypatch, "zeta", 6006)
    r = client.post("/agent/revert-one",
                    json={"session_id": out["session_id"], "seq": 999})
    assert r.json()["reverted"] == 0


# -- provider streaming (token-level deltas) ----------------------------------

def _openai_sse(*chunks):
    return iter([f"data: {__import__('json').dumps(c)}" for c in chunks]
                + ["data: [DONE]"])


def test_stream_openai_text_deltas(monkeypatch):
    from assistant_service import rest

    monkeypatch.setattr(rest, "post_stream", lambda url, h, b, timeout=120: _openai_sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ))
    events = list(loop._stream_openai([{"role": "user", "content": "x"}], [], LLM))
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["Hel", "lo"]
    result = events[-1]["result"]
    assert result["text"] == "Hello" and result["tool_calls"] == []


def test_stream_openai_tool_call_assembly(monkeypatch):
    from assistant_service import rest

    monkeypatch.setattr(rest, "post_stream", lambda url, h, b, timeout=120: _openai_sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1",
             "function": {"name": "list_connections", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"a"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ': 1}'}}]}}]},
    ))
    events = list(loop._stream_openai([{"role": "user", "content": "x"}], [], LLM))
    result = events[-1]["result"]
    assert result["tool_calls"] == [
        {"id": "c1", "name": "list_connections", "args": {"a": 1}}]


def test_stream_anthropic_text_and_tool(monkeypatch):
    import json as _json

    from assistant_service import rest

    frames = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Check"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ing…"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t1",
                           "name": "list_tables"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "message_stop"},
    ]
    monkeypatch.setattr(
        rest, "post_stream",
        lambda url, h, b, timeout=120: iter(
            [f"data: {_json.dumps(f)}" for f in frames]))
    llm = {**LLM, "provider": "anthropic"}
    events = list(loop._stream_anthropic([{"role": "user", "content": "x"}], [], llm))
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["Check", "ing…"]
    result = events[-1]["result"]
    assert result["text"] == "Checking…"
    assert result["tool_calls"] == [
        {"id": "t1", "name": "list_tables", "args": {}}]


def test_iter_agent_stream_emits_deltas_then_final(backend, monkeypatch):
    from assistant_service import rest

    monkeypatch.setattr(rest, "post_stream", lambda url, h, b, timeout=120: _openai_sse(
        {"choices": [{"delta": {"content": "All "}}]},
        {"choices": [{"delta": {"content": "done."}}]},
    ))
    ctx = ToolContext(journal=ChangeJournal())
    events = list(iter_agent([{"role": "user", "content": "x"}], ctx, LLM,
                             stream=True))
    kinds = [e["type"] for e in events]
    assert kinds == ["delta", "delta", "final"]
    assert events[-1]["reply"] == "All done."


def test_stream_falls_back_when_sse_unavailable(backend, monkeypatch):
    from assistant_service import rest

    def no_sse(url, h, b, timeout=120):
        raise RuntimeError("HTTP 400: stream unsupported")

    monkeypatch.setattr(rest, "post_stream", no_sse)
    monkeypatch.setattr(loop, "provider_tool_call",
                        lambda convo, tools, llm: {"text": "plain", "tool_calls": []})
    ctx = ToolContext(journal=ChangeJournal())
    events = list(iter_agent([{"role": "user", "content": "x"}], ctx, LLM,
                             stream=True))
    assert [e["type"] for e in events] == ["final"]
    assert events[-1]["reply"] == "plain"
