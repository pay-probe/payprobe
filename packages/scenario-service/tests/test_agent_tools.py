"""Agent tool registry — dispatch, guardrails, and the reversible change journal.

These exercise the *config* surface the general assistant drives, with no LLM or
network involved: the loop hands the registry a tool name + args, the registry
mutates real (in-memory) stores, and the journal can undo every write.
"""
from types import SimpleNamespace

import pytest

from api.agent_tools import (
    ChangeJournal,
    ToolContext,
    dispatch,
    schemas_for,
    tools_for,
)
from api.catalog_store import CatalogStore
from api.connection_store import ConnectionStore
from api.environment_store import EnvironmentDraft, EnvironmentStore
from api.flow_store import StarterFlowStore
from api.format_store import FormatStore
from api.network_flow_store import NetworkFlowStore
from api.participant_flow_store import ParticipantFlowStore
from api.participant_group_store import ParticipantGroupStore
from api.table_store import TableStore
from api.variable_store import VariableStore


def _ctx(**kw) -> ToolContext:
    stores = SimpleNamespace(
        connections=ConnectionStore(":memory:"),
        catalog=CatalogStore(":memory:"),
        flows=StarterFlowStore(":memory:"),
        formats=FormatStore(":memory:"),
        tables=TableStore(":memory:"),
        variables=VariableStore(":memory:"),
        environments=EnvironmentStore(":memory:"),
        network_flows=NetworkFlowStore(":memory:"),
        participant_flows=ParticipantFlowStore(":memory:"),
        participant_groups=ParticipantGroupStore(":memory:"),
    )
    return ToolContext(stores=stores, journal=ChangeJournal(), **kw)


# -- registry shape -----------------------------------------------------------

def test_read_tier_excludes_writes():
    read_names = {t.name for t in tools_for(("read",))}
    write_names = {t.name for t in tools_for(("write",))}
    assert "list_connections" in read_names
    assert "upsert_connection" in write_names
    assert read_names.isdisjoint(write_names)
    # advisor mode hands the model only read schemas
    assert all(s["name"] in read_names for s in schemas_for(("read",)))


def test_unknown_tool_is_a_clean_error():
    out = dispatch(_ctx(), "frobnicate", {})
    assert out["ok"] is False and "unknown tool" in out["error"]


# -- read ---------------------------------------------------------------------

def test_get_connection_not_found():
    out = dispatch(_ctx(), "get_connection", {"name": "nope"})
    assert out["ok"] is False and out["guardrail"] is False


def test_list_environments_is_a_read_tool():
    # the assistant must be able to list environments (not fall back to
    # list_connections, which is a different resource)
    assert "list_environments" in {t.name for t in tools_for(("read",))}
    ctx = _ctx()
    ctx.stores.environments.upsert("staging", EnvironmentDraft(name="Staging"))
    out = dispatch(ctx, "list_environments", {})
    assert out["ok"] is True
    assert any(e["name"] == "Staging" for e in out["result"])


def test_get_environment_not_found():
    out = dispatch(_ctx(), "get_environment", {"name": "nope"})
    assert out["ok"] is False and out["guardrail"] is False


# -- write + journal ----------------------------------------------------------

def test_upsert_connection_then_revert():
    ctx = _ctx()
    out = dispatch(ctx, "upsert_connection",
                   {"name": "switch", "config": {"host": "10.0.0.1", "port": 5000}})
    assert out["ok"] and out["result"]["action"] == "created"
    assert ctx.stores.connections.get("switch") is not None
    assert len(ctx.journal.entries()) == 1

    assert ctx.journal.revert(ctx) == 1
    assert ctx.stores.connections.get("switch") is None  # rolled back
    assert ctx.journal.entries() == []


def test_upsert_connection_update_revert_restores_prior_value():
    ctx = _ctx()
    dispatch(ctx, "upsert_connection", {"name": "sw", "config": {"port": 1000}})
    ctx.journal.revert(ctx)                      # back to empty baseline
    dispatch(ctx, "upsert_connection", {"name": "sw", "config": {"port": 1000}})
    out = dispatch(ctx, "upsert_connection", {"name": "sw", "config": {"port": 2000}})
    assert out["result"]["action"] == "updated"
    assert ctx.stores.connections.get("sw").port == 2000
    ctx.journal.revert(ctx)                      # undo both writes, newest first
    assert ctx.stores.connections.get("sw") is None


def test_invalid_connection_config_rejected():
    out = dispatch(_ctx(), "upsert_connection",
                   {"name": "bad", "config": {"port": 99999}})
    assert out["ok"] is False and out["guardrail"] is False


def test_delete_connection_blocked_when_referenced():
    ctx = _ctx(referenced_connections={"switch"})
    dispatch(ctx, "upsert_connection", {"name": "switch", "config": {"port": 5000}})
    out = dispatch(ctx, "delete_connection", {"name": "switch"})
    assert out["ok"] is False and out["guardrail"] is True
    assert ctx.stores.connections.get("switch") is not None  # not deleted


def test_delete_connection_revert_restores():
    ctx = _ctx()
    # baseline connection (not part of the journal) so revert tests delete's undo
    from api.connection_store import ConnectionDraft
    ctx.stores.connections.upsert("acq", ConnectionDraft(host="h", port=7000))
    out = dispatch(ctx, "delete_connection", {"name": "acq"})
    assert out["ok"] and ctx.stores.connections.get("acq") is None
    ctx.journal.revert(ctx)
    restored = ctx.stores.connections.get("acq")
    assert restored is not None and restored.port == 7000


def test_save_table_and_revert():
    ctx = _ctx()
    out = dispatch(ctx, "save_table",
                   {"name": "bins", "draft": {"columns": ["bin"], "rows": [{"bin": "4"}]}})
    assert out["ok"] and ctx.stores.tables.get("bins") is not None
    ctx.journal.revert(ctx)
    assert ctx.stores.tables.get("bins") is None


def test_set_global_variables_revert():
    ctx = _ctx()
    dispatch(ctx, "set_global_variables", {"variables": {"a": "1"}})
    assert ctx.stores.variables.get_global() == {"a": "1"}
    dispatch(ctx, "set_global_variables", {"variables": {"a": "2", "b": "3"}})
    assert ctx.stores.variables.get_global()["a"] == "2"
    ctx.journal.revert(ctx)                      # both reverted -> empty
    assert ctx.stores.variables.get_global() == {}


def test_delete_builtin_starter_flow_is_guarded():
    ctx = _ctx()
    flows = ctx.stores.flows.list()
    builtin = next((f for f in flows if f.builtin), None)
    assert builtin is not None, "expected at least one builtin starter flow"
    out = dispatch(ctx, "delete_starter_flow", {"fid": builtin.id})
    assert out["ok"] is False and out["guardrail"] is True


def test_create_starter_flow_and_revert():
    ctx = _ctx()
    out = dispatch(ctx, "create_starter_flow",
                   {"draft": {"label": "My flow", "steps": []}})
    assert out["ok"]
    fid = out["result"]["saved"]["id"]
    assert ctx.stores.flows.get(fid) is not None
    ctx.journal.revert(ctx)
    assert ctx.stores.flows.get(fid) is None


# -- networks (network flows) ---------------------------------------------------

def _seed_network_refs(ctx):
    """A participant flow the network can reference."""
    from models.participant_flow import ParticipantFlowDraft, FlowTrigger
    from models.scenario import Step
    return ctx.stores.participant_flows.create(ParticipantFlowDraft(
        name="issuer stub", trigger=FlowTrigger(connection=""),
        nodes=[Step(id="t", kind="trigger"),
               Step(id="r", kind="reply", payload={"set": {"39": "00"}})],
    ))


def _network_doc(flow_id):
    return {
        "name": "hsm net",
        "nodes": [
            {"id": "iss", "kind": "participant",
             "config": {"flow_id": flow_id, "instances": 2}},
            {"id": "drv", "kind": "scenario",
             "config": {"scenario_id": "scn-1", "autostart": True}},
        ],
        "edges": [{"source": "drv", "target": "iss"}],
    }


def test_network_tools_list_get_plan():
    ctx = _ctx()
    flow = _seed_network_refs(ctx)
    dispatch(ctx, "save_network",
             {"network_id": "net-1", "network": _network_doc(flow.id)})
    rows = dispatch(ctx, "list_networks", {})["result"]
    assert rows[0]["id"] == "net-1"
    assert rows[0]["nodes"] == {"participant": 1, "scenario": 1}

    full = dispatch(ctx, "get_network", {"network_id": "net-1"})["result"]
    assert len(full["nodes"]) == 2

    plan = dispatch(ctx, "plan_network", {"network_id": "net-1"})["result"]
    assert plan["participants"][0]["flow_id"] == flow.id
    assert plan["participants"][0]["instances"] == 2
    assert plan["initiators"][0]["scenario_id"] == "scn-1"


def test_save_network_rejects_dangling_flow_reference():
    ctx = _ctx()
    out = dispatch(ctx, "save_network",
                   {"network_id": "bad", "network": _network_doc("ghost-flow")})
    assert out["ok"] is False
    assert "ghost-flow" in out["error"]
    assert ctx.stores.network_flows.get("bad") is None   # nothing saved


def test_save_and_delete_network_revert():
    ctx = _ctx()
    flow = _seed_network_refs(ctx)
    dispatch(ctx, "save_network",
             {"network_id": "net-2", "network": _network_doc(flow.id)})
    # update it, then delete it — revert must restore the ORIGINAL then remove it
    doc = _network_doc(flow.id)
    doc["description"] = "v2"
    dispatch(ctx, "save_network", {"network_id": "net-2", "network": doc})
    dispatch(ctx, "delete_network", {"network_id": "net-2"})
    assert ctx.stores.network_flows.get("net-2") is None
    assert len(ctx.journal.records) == 3
    ctx.journal.revert(ctx)
    assert ctx.stores.network_flows.get("net-2") is None  # create rolled back too


def test_validate_network_reports_wiring_errors():
    ctx = _ctx()
    out = dispatch(ctx, "validate_network", {"network": {
        "name": "bad",
        "nodes": [{"id": "s", "kind": "simulator", "config": {"simulator_id": "x"}},
                  {"id": "p", "kind": "participant", "config": {"flow_id": "f"}}],
        "edges": [{"source": "s", "target": "p"}],
    }})["result"]
    assert out["valid"] is False
    assert any("only receive traffic" in i["message"] for i in out["issues"])


# -- runtime visibility (orchestrator over HTTP) ----------------------------------

def test_runtime_read_tools_bridge_to_orchestrator(monkeypatch):
    from api import agent_tools

    calls = []

    def fake_get(path):
        calls.append(path)
        return {"/status": {"status": "ok", "services": {}},
                "/runs": [{"id": "r1"}],
                "/topology-runs": [{"id": "tr1", "health": {"ready": True}}],
                "/participants": [],
                "/simulators": [{"id": "hsm-8000"}],
                "/load-runs": [{"id": "lr1"}]}[path]

    monkeypatch.setattr(agent_tools, "_run_api_get", fake_get)
    ctx = _ctx()
    assert dispatch(ctx, "platform_status", {})["result"]["status"] == "ok"
    assert dispatch(ctx, "list_runs", {"limit": 5})["result"] == [{"id": "r1"}]
    assert dispatch(ctx, "list_network_runs", {})["result"][0]["id"] == "tr1"
    assert dispatch(ctx, "list_running_simulators", {})["result"][0]["id"] == "hsm-8000"
    assert calls[0] == "/status"
    assert ctx.journal.entries() == []           # read-tier, nothing journalled


def test_runtime_tools_fail_clean_when_orchestrator_down():
    out = dispatch(_ctx(), "list_network_runs", {})
    # no orchestrator in unit tests → a clean ToolError envelope, not a crash
    assert out["ok"] is False and "orchestrator unreachable" in out["error"]


# -- model insights (insight-service over HTTP, ADR-0005) -------------------------

def test_insight_read_tools_bridge_to_insight_service(monkeypatch):
    from api import agent_tools

    calls = []

    def fake_get(path):
        calls.append(path)
        if path.startswith("/insights/failures/"):
            return {"run_id": "r9", "failures": [
                {"category": "timeout", "novel": False,
                 "explanation": "Category: timeout…"}], "advisory_only": True}
        return {"predictions": [
            {"scenario_id": "sc-1", "p_fail_next": 0.7, "p_flaky": 0.1,
             "n_history": 12, "top_factors": []}], "advisory_only": True}

    monkeypatch.setattr(agent_tools, "_insight_api_get", fake_get)
    ctx = _ctx()
    out = dispatch(ctx, "get_run_insights", {"run_id": "r9"})["result"]
    assert out["failures"][0]["category"] == "timeout"
    assert out["advisory_only"] is True
    preds = dispatch(ctx, "list_insight_predictions",
                     {"environment": "staging"})["result"]
    assert preds["predictions"][0]["p_fail_next"] == 0.7
    assert "environment=staging" in calls[1]
    assert ctx.journal.entries() == []           # read-tier, nothing journalled


def test_insight_get_maps_404_to_tool_error(monkeypatch):
    from api import agent_tools

    monkeypatch.setattr(agent_tools, "_insight_api_get", lambda path: None)
    out = dispatch(_ctx(), "get_run_insights", {"run_id": "nope"})
    assert out["ok"] is False and "nope" in out["error"]


def test_insight_tools_fail_clean_when_service_down():
    out = dispatch(_ctx(), "list_insight_predictions", {})
    # no insight service in unit tests → clean ToolError, and the message says
    # the deployment is optional rather than implying a platform fault
    assert out["ok"] is False and "insight service unreachable" in out["error"]


# -- playground (orchestrator over HTTP, ADR-0007: execute tier) -------------------

def test_playground_tools_tiering():
    """playground_execute is EXECUTE tier: not a read (it fires traffic), not a
    journalled write (nothing to restore) — and advisor mode never sees it."""
    exec_names = {t.name for t in tools_for(("execute",))}
    assert exec_names == {"playground_execute", "start_load_run"}
    assert "playground_targets" in {t.name for t in tools_for(("read",))}
    assert "playground_execute" not in {s["name"] for s in schemas_for(("read",))}
    assert "playground_execute" not in {s["name"] for s in schemas_for(("read", "write"))}
    assert "start_load_run" not in {s["name"] for s in schemas_for(("read", "write"))}
    # the default surface includes all three tiers
    assert "playground_execute" in {s["name"] for s in schemas_for()}
    assert "start_load_run" in {s["name"] for s in schemas_for()}


def test_playground_execute_refused_outside_allowed_tiers():
    """Plan/advisor defense-in-depth: execution-time tier enforcement refuses
    an execute tool even if the model calls it."""
    out = dispatch(_ctx(), "playground_execute",
                   {"target": {"kind": "raw"}, "action": "send_message"},
                   tiers=("read",))
    assert out["ok"] is False and out["guardrail"] is True


def test_playground_tools_bridge_to_orchestrator(monkeypatch):
    from api import agent_tools

    def fake_get(path):
        assert path == "/playground/targets"
        return {"connections": [{"name": "switch_visa"}], "simulators": []}

    posted = {}

    def fake_post(path, body):
        posted["path"], posted["body"] = path, body
        return {"ok": True, "status": "passed", "history_seq": 1}

    monkeypatch.setattr(agent_tools, "_run_api_get", fake_get)
    monkeypatch.setattr(agent_tools, "_run_api_post", fake_post)
    ctx = _ctx()
    out = dispatch(ctx, "playground_targets", {})
    assert out["ok"] is True
    assert out["result"]["connections"][0]["name"] == "switch_visa"

    out = dispatch(ctx, "playground_execute", {
        "target": {"kind": "connection", "id": "switch_visa",
                   "environment": "staging"},
        "action": "send_message", "payload": {"mti": "0200"},
        "message_format_id": "visa-base1"})
    assert out["ok"] is True and out["tier"] == "execute"
    assert posted["path"] == "/playground/execute"
    assert posted["body"]["target"]["id"] == "switch_visa"
    assert posted["body"]["message_format_id"] == "visa-base1"
    assert ctx.journal.entries() == []           # execute tier: never journalled


def test_start_load_run_bridges_to_orchestrator(monkeypatch):
    """start_load_run is EXECUTE tier: builds the profile body, POSTs to
    /load-runs, never journals; get_load_run reads one run's metrics."""
    from api import agent_tools

    posted = {}

    def fake_post(path, body):
        posted["path"], posted["body"] = path, body
        return {"run_id": "lr-1", "status": "running"}

    def fake_get(path):
        assert path == "/load-runs/lr-1"
        return {"status": "completed", "tps": 50.0,
                "latency_ms": {"p95": 21.5}}

    monkeypatch.setattr(agent_tools, "_run_api_post", fake_post)
    monkeypatch.setattr(agent_tools, "_run_api_get", fake_get)
    ctx = _ctx()

    out = dispatch(ctx, "start_load_run", {
        "scenario_ids": ["scn-abc"], "profile_type": "steady",
        "target_tps": 50, "workers": 2, "duration_s": 30,
        "label": "smoke"})
    assert out["ok"] is True and out["tier"] == "execute"
    assert out["result"]["run_id"] == "lr-1"
    assert posted["path"] == "/load-runs"
    assert posted["body"]["type"] == "steady"
    assert posted["body"]["scenario_ids"] == ["scn-abc"]
    assert posted["body"]["workers"] == 2
    assert ctx.journal.entries() == []           # execute tier: never journalled

    got = dispatch(ctx, "get_load_run", {"run_id": "lr-1"})
    assert got["ok"] is True and got["tier"] == "read"
    assert got["result"]["status"] == "completed"

    # a scenario-less start is refused before any HTTP call
    bad = dispatch(_ctx(), "start_load_run", {"target_tps": 10})
    assert bad["ok"] is False and "scenario" in bad["error"]
