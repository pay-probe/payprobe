"""Participant-flow lifecycle: start a listening flow, drive it, list, stop."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402
from worker.adapters.tcp.adapter import TcpAdapter  # noqa: E402


FLOW = {
    "id": "issuer",
    "name": "issuer stub",
    "trigger": {"connection": ""},  # no connection -> ISO responder on an ephemeral port
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "chk", "kind": "if",
         "config": {"left": "${request.mti}", "operator": "eq", "right": "0200"}},
        {"id": "ok", "kind": "reply",
         "payload": {"echo": ["11", "37"], "set": {"39": "00", "38": "AUTH99"}}},
        {"id": "no", "kind": "reply",
         "payload": {"echo": ["11"], "set": {"39": "05"}}},
    ],
    "edges": [
        {"source": "t", "source_port": "out", "target": "chk"},
        {"source": "chk", "source_port": "true", "target": "ok"},
        {"source": "chk", "source_port": "false", "target": "no"},
    ],
}


async def _fake_get(url):
    if url.endswith("/participant-flows/issuer"):
        return FLOW
    raise AssertionError(f"unexpected fetch: {url}")


async def test_participant_start_drive_list_stop(monkeypatch):
    m.PARTICIPANTS.clear()
    monkeypatch.setattr(m, "_http_get_json", _fake_get)

    started = await m.start_participant(m.StartParticipant(flow_id="issuer"))
    pid, port = started["id"], started["port"]
    assert port and started["flow_id"] == "issuer"

    adapter = TcpAdapter({"host": "127.0.0.1", "port": port,
                          "sign_on": {"enabled": False},
                          "reconnect": {"enabled": False}, "response_timeout_sec": 2})
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"] == "AUTH99"
    finally:
        await adapter.disconnect()

    listed = await m.list_participants()
    assert any(p["id"] == pid and p["received"] >= 1 for p in listed)

    detail = await m.get_participant(pid)
    assert detail["received"] >= 1
    assert detail["by_mti"]  # per-MTI metrics tracked
    assert detail["log"]     # recent decoded requests

    await m.stop_participant(pid)
    assert all(p["id"] != pid for p in await m.list_participants())


async def test_participant_traces_index_and_drilldown(monkeypatch):
    class _R:
        port = 9000

        def traces(self, cid=None):
            data = [
                {"correlation_id": "aa", "flow_id": "sw", "mti": "0200", "ts": 2.0,
                 "ok": False, "calls": [
                     {"target": "auth-group", "ok": False,
                      "error": "ValueError: unknown adapter 'auth-group'"}]},
                {"correlation_id": "bb", "flow_id": "sw", "mti": "0200", "ts": 1.0,
                 "ok": True, "calls": []},
            ]
            return data if cid is None else [t for t in data if t["correlation_id"] == cid]

    monkeypatch.setattr(m, "PARTICIPANTS", {"p1": _R()})

    idx = await m.participant_traces()
    rows = idx["transactions"]
    assert [t["correlation_id"] for t in rows] == ["aa", "bb"]   # newest first
    assert rows[0]["ok"] is False and rows[0]["hops"] == 2        # flow hop + 1 call
    assert rows[1]["ok"] is True

    # filter by correlation-id / STAN substring
    f = await m.participant_traces(q="bb")
    assert [t["correlation_id"] for t in f["transactions"]] == ["bb"]

    # drill into one transaction → the failing hop's reason is visible
    det = await m.participant_trace("aa")
    assert det["hops"][0]["calls"][0]["error"] == "ValueError: unknown adapter 'auth-group'"


async def test_downstream_resolves_action_node_connections(monkeypatch):
    """A flow's outbound action-node targets are resolved into a worker-shaped
    adapters map, with connection metadata stripped."""
    flow = {
        "id": "switch",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "call", "kind": "action", "target": "issuer",
             "action": "send_0200", "payload": {}},
            {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}},
        ],
    }
    conn = {"name": "issuer", "adapter": "tcp", "protocol": "iso8583",
            "host": "10.0.0.9", "port": 7005, "mode": "outbound",
            "environment_overrides": {"prod": {"host": "x"}}}

    async def fake_get(url):
        if url.endswith("/connections/issuer"):
            return conn
        raise AssertionError(url)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    adapters = await m._participant_downstream(flow)
    assert adapters["issuer"]["host"] == "10.0.0.9"
    assert "name" not in adapters["issuer"]
    assert "environment_overrides" not in adapters["issuer"]


async def test_downstream_resolves_group_target(monkeypatch):
    """A flow's outbound node can target a participant *group*; it is inlined as
    an ``adapter:"group"`` config with each member's resolved connection."""
    flow = {
        "id": "switch",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "call", "kind": "action", "target": "issuers",
             "action": "send_0200", "payload": {}},
            {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}},
        ],
    }
    group = {"id": "g1", "name": "issuers", "selection": {"policy": "weighted"},
             "members": [{"connection": "issuer_a", "weight": 2},
                         {"connection": "issuer_b", "weight": 1}]}
    conns = {
        "issuer_a": {"name": "issuer_a", "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.0.1", "port": 7001, "mode": "outbound"},
        "issuer_b": {"name": "issuer_b", "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.0.2", "port": 7002, "mode": "outbound"},
    }

    async def fake_get(url):
        if url.endswith("/connections/issuers"):
            raise RuntimeError("not a connection")     # the target names a group
        if url.endswith("/participant-groups"):
            return [group]
        if url.endswith("/connections/issuer_a"):
            return conns["issuer_a"]
        if url.endswith("/connections/issuer_b"):
            return conns["issuer_b"]
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    adapters = await m._participant_downstream(flow)
    g = adapters["issuers"]
    assert g["adapter"] == "group"
    assert g["selection"] == {"policy": "weighted"}
    assert [mem["connection"] for mem in g["members"]] == ["issuer_a", "issuer_b"]
    assert g["members"][0]["weight"] == 2
    assert g["members"][0]["config"]["host"] == "10.0.0.1"
    assert "name" not in g["members"][0]["config"]      # registry metadata stripped


async def test_disabled_member_dropped_from_group(monkeypatch):
    """A connection flagged ``disabled`` is skipped as a group member (out of
    rotation) while the other members remain."""
    group = {"id": "g1", "name": "issuers", "selection": {"policy": "round_robin"},
             "members": [{"connection": "issuer_a", "weight": 1},
                         {"connection": "issuer_b", "weight": 1}]}
    conns = {
        "issuer_a": {"name": "issuer_a", "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.0.1", "port": 7001, "mode": "outbound"},
        "issuer_b": {"name": "issuer_b", "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.0.2", "port": 7002, "mode": "outbound",
                     "disabled": True},
    }

    async def fake_get(url):
        if url.endswith("/connections/issuer_a"):
            return conns["issuer_a"]
        if url.endswith("/connections/issuer_b"):
            return conns["issuer_b"]
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    cfg = await m._group_adapter_config(group)
    names = [mem["connection"] for mem in cfg["members"]]
    assert names == ["issuer_a"]                       # disabled member dropped
    assert "disabled" not in cfg["members"][0]["config"]  # metadata stripped


async def test_start_unknown_flow_is_404(monkeypatch):
    import httpx

    async def boom(url):
        req = httpx.Request("GET", url)
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("not found", request=req, response=resp)

    monkeypatch.setattr(m, "_http_get_json", boom)
    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException) as ei:
        await m.start_participant(m.StartParticipant(flow_id="nope"))
    assert ei.value.status_code == 404
