"""Persistent change-journal sessions + one-click revert.

Covers the in-memory session store, JSON-round-tripped restore (proving the
journal is persistence-safe), and the /agent/chat → /agent/revert flow across
two turns with a fake provider (no network).
"""
import asyncio
import json
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api import agent as agent_mod
from api.agent_session import InMemorySessionStore
from api.agent_tools import ChangeJournal, ToolContext, dispatch, restore_journal
from api.connection_store import ConnectionStore
from api.main import app
from api.table_store import TableStore
from api.variable_store import VariableStore
from api.catalog_store import CatalogStore
from api.flow_store import StarterFlowStore
from api.format_store import FormatStore


def _stores() -> SimpleNamespace:
    return SimpleNamespace(
        connections=ConnectionStore(":memory:"),
        catalog=CatalogStore(":memory:"),
        flows=StarterFlowStore(":memory:"),
        formats=FormatStore(":memory:"),
        tables=TableStore(":memory:"),
        variables=VariableStore(":memory:"),
    )


# -- session store ------------------------------------------------------------

def test_in_memory_session_accumulates_and_clears():
    store = InMemorySessionStore()

    async def go():
        await store.append("s1", [{"seq": 1}])
        await store.append("s1", [{"seq": 2}])
        assert len(await store.get("s1")) == 2
        await store.clear("s1")
        assert await store.get("s1") == []

    asyncio.run(go())


# -- restore from serialized records (persistence-safe) -----------------------

def test_restore_journal_from_json_records():
    ctx = ToolContext(stores=_stores(), journal=ChangeJournal())
    dispatch(ctx, "upsert_connection",
             {"name": "sw", "config": {"host": "h", "port": 5000}})
    dispatch(ctx, "save_table", {"name": "t", "draft": {"rows": []}})
    assert ctx.stores.connections.get("sw") is not None
    assert ctx.stores.tables.get("t") is not None

    # serialize the journal (as it would be stored in Redis) and replay it
    records = json.loads(json.dumps(ctx.journal.dump()))
    fresh = ToolContext(stores=ctx.stores)  # restore acts on the same stores
    n = restore_journal(fresh, records)
    assert n == 2
    assert ctx.stores.connections.get("sw") is None
    assert ctx.stores.tables.get("t") is None


# -- endpoint flow ------------------------------------------------------------

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _enable_llm(client):
    client.put("/assist/config", json={"provider": "openai", "enabled": True,
                                       "api_key": "sk-test", "model": "gpt-4o"})


def test_chat_persists_session_then_revert_undoes_all(client, monkeypatch):
    _enable_llm(client)
    # 4 provider turns: (tool, final) for chat #1, then (tool, final) for chat #2
    turns = iter([
        {"text": "", "tool_calls": [{"id": "1", "name": "upsert_connection",
         "args": {"name": "alpha", "config": {"port": 5001}}}]},
        {"text": "Added alpha.", "tool_calls": []},
        {"text": "", "tool_calls": [{"id": "2", "name": "upsert_connection",
         "args": {"name": "beta", "config": {"port": 5002}}}]},
        {"text": "Added beta.", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_mod, "provider_tool_call",
                        lambda convo, tools, llm: next(turns))

    r1 = client.post("/agent/chat",
                     json={"messages": [{"role": "user", "content": "add alpha"}]})
    sid = r1.json()["session_id"]
    assert sid
    r2 = client.post("/agent/chat", json={
        "session_id": sid,
        "messages": [
            {"role": "user", "content": "add alpha"},
            {"role": "assistant", "content": "Added alpha."},
            {"role": "user", "content": "now add beta"},
        ],
    })
    assert r2.json()["session_id"] == sid
    assert client.get("/connections/alpha").status_code == 200
    assert client.get("/connections/beta").status_code == 200

    rev = client.post("/agent/revert", json={"session_id": sid})
    assert rev.status_code == 200 and rev.json()["reverted"] == 2
    assert client.get("/connections/alpha").status_code == 404
    assert client.get("/connections/beta").status_code == 404


def test_revert_unknown_session_is_noop(client):
    r = client.post("/agent/revert", json={"session_id": "nope"})
    assert r.status_code == 200 and r.json()["reverted"] == 0
