"""REST-backed tool registry: dispatch, guardrails, journal, restore.

The ``backend`` fixture (an in-memory fake of the PayProbe REST surface) is
defined in conftest.py.
"""
from assistant_service import tools
from assistant_service.tools import ChangeJournal, ToolContext, dispatch, restore_journal


def _ctx(**kw):
    return ToolContext(journal=ChangeJournal(), **kw)


def test_tiers_split():
    read = {t.name for t in tools.tools_for(("read",))}
    write = {t.name for t in tools.tools_for(("write",))}
    assert "list_connections" in read and "upsert_connection" in write
    assert read.isdisjoint(write)


def test_unknown_tool(backend):
    out = dispatch(_ctx(), "frobnicate", {})
    assert out["ok"] is False and "unknown tool" in out["error"]


def test_list_and_get_environment(backend):
    assert "list_environments" in {t.name for t in tools.tools_for(("read",))}
    out = dispatch(_ctx(), "list_environments", {})
    assert out["ok"] and any(e["name"] == "mock" for e in out["result"])
    got = dispatch(_ctx(), "get_environment", {"name": "mock"})
    assert got["ok"] and got["result"]["name"] == "mock"
    missing = dispatch(_ctx(), "get_environment", {"name": "nope"})
    assert missing["ok"] is False


def test_upsert_connection_and_journal(backend):
    ctx = _ctx()
    out = dispatch(ctx, "upsert_connection",
                   {"name": "switch", "config": {"host": "h", "port": 5000}})
    assert out["ok"] and out["result"]["action"] == "created"
    assert "switch" in backend.connections
    assert ctx.journal.entries()[0]["resource"] == "connection"


def test_revert_upsert_then_delete(backend):
    ctx = _ctx()
    dispatch(ctx, "upsert_connection", {"name": "c", "config": {"port": 1}})
    assert restore_journal(ctx.journal.dump()) == 1
    assert "c" not in backend.connections  # created → removed on revert


def test_revert_delete_restores(backend):
    backend.connections["acq"] = {"host": "h", "port": 7000}
    ctx = _ctx()
    out = dispatch(ctx, "delete_connection", {"name": "acq"})
    assert out["ok"] and "acq" not in backend.connections
    restore_journal(ctx.journal.dump())
    assert backend.connections.get("acq", {}).get("port") == 7000


def test_delete_connection_referenced_guardrail(backend):
    backend.connections["sw"] = {"port": 1}
    ctx = _ctx(referenced_connections={"sw"})
    out = dispatch(ctx, "delete_connection", {"name": "sw"})
    assert out["ok"] is False and out["guardrail"] is True
    assert "sw" in backend.connections


def test_delete_builtin_starter_flow_guardrail(backend):
    ctx = _ctx()
    out = dispatch(ctx, "delete_starter_flow", {"fid": "builtin_demo"})
    assert out["ok"] is False and out["guardrail"] is True


def test_set_variables_revert(backend):
    backend.variables = {"variables": {"a": "1"}, "secrets": []}
    ctx = _ctx()
    dispatch(ctx, "set_global_variables", {"variables": {"a": "2", "b": "3"}})
    assert backend.variables["variables"]["a"] == "2"
    restore_journal(ctx.journal.dump())
    assert backend.variables["variables"] == {"a": "1"}


def test_create_scenario_validates_and_reverts(backend):
    ctx = _ctx()
    good = {"name": "flow", "steps": [{"id": "s1", "target": "http",
                                       "action": "send_auth_request"}]}
    out = dispatch(ctx, "create_scenario", {"scenario": good})
    assert out["ok"]
    sid = out["result"]["id"]
    assert sid in backend.scenarios
    restore_journal(ctx.journal.dump())
    assert sid not in backend.scenarios


def test_create_scenario_rejects_invalid(backend):
    ctx = _ctx()
    bad = {"name": "bad", "steps": [{"id": "dup"}, {"id": "dup"}]}
    out = dispatch(ctx, "create_scenario", {"scenario": bad})
    assert out["ok"] is False and "invalid" in out["error"]
    assert backend.scenarios == {}


def test_referenced_connection_names_scans_scenarios(backend):
    backend.connections["sw"] = {"port": 1}
    backend.scenarios["sc_1"] = {"id": "sc_1", "name": "x",
                                 "steps": [{"target": "sw"}]}
    assert "sw" in tools.referenced_connection_names()


def _network_doc(flow_id="issuer-flow"):
    return {"name": "hsm net", "nodes": [
        {"id": "iss", "kind": "participant",
         "config": {"flow_id": flow_id, "instances": 2}}], "edges": []}


def test_network_tools_roundtrip_and_revert(backend):
    ctx = ToolContext()
    out = dispatch(ctx, "save_network",
                   {"network_id": "net-1", "network": _network_doc()})
    assert out["ok"] and out["result"]["action"] == "created"
    assert "net-1" in backend.network_flows

    rows = dispatch(ctx, "list_networks", {})["result"]
    assert rows[0]["id"] == "net-1" and rows[0]["nodes"] == {"participant": 1}

    plan = dispatch(ctx, "plan_network", {"network_id": "net-1"})["result"]
    assert plan["participants"][0]["flow_id"] == "issuer-flow"

    dispatch(ctx, "delete_network", {"network_id": "net-1"})
    assert "net-1" not in backend.network_flows

    # revert newest-first: delete undone (restored), then create undone (gone)
    restore_journal(ctx.journal.dump())
    assert "net-1" not in backend.network_flows


def test_save_network_rejects_dangling_reference(backend):
    ctx = ToolContext()
    out = dispatch(ctx, "save_network",
                   {"network_id": "bad", "network": _network_doc("ghost")})
    assert out["ok"] is False and "ghost" in out["error"]
    assert "bad" not in backend.network_flows
    assert ctx.journal.entries() == []           # nothing journalled


def test_get_network_not_found(backend):
    out = dispatch(ToolContext(), "get_network", {"network_id": "nope"})
    assert out["ok"] is False and out["guardrail"] is False


def test_runtime_read_tools(backend):
    ctx = ToolContext()
    assert dispatch(ctx, "platform_status", {})["result"]["status"] == "ok"
    assert dispatch(ctx, "list_runs", {})["result"][0]["id"] == "run-1"
    runs = dispatch(ctx, "list_network_runs", {})["result"]
    assert runs[0]["health"]["ready"] is True
    assert dispatch(ctx, "list_running_participants", {})["result"][0]["port"] == 8201
    assert dispatch(ctx, "list_running_simulators", {})["result"][0]["id"] == "hsm-8000"
    assert dispatch(ctx, "list_load_runs", {"limit": 1})["result"][0]["tps"] == 1200
    # runtime tools are read-tier: available in advisor mode, never journalled
    assert ctx.journal.entries() == []
    read_names = {t.name for t in tools.tools_for(("read",))}
    assert {"platform_status", "list_runs", "list_network_runs",
            "list_running_participants", "list_running_simulators",
            "list_load_runs"} <= read_names
