"""The human gate, closed on every path (ADR-0010 security review, 2026-09-24).

Each test here is the exploit the review wrote down, turned into a check that
it no longer works: a gate a condition can skip, a gate crossed on its
``rejected`` edge, an agent republished as ``full`` after the workflow was
validated, a ``mock`` label that scoped nothing, a full-mode agent on a timer,
pause ignored by tool nodes, an editor widening their own grant, ``extra``
sneaking an environment past the write scope, a spike's base rate outside the
load cap, and credentials reaching the model or a viewer.
"""

import time

from agent_hub.llm import FakeLLMBackend
from agent_hub.models import WorkflowSpec
from agent_hub.validate import approval_gated, validate_agent_spec, validate_workflow_spec, widens
from hub_testkit import FakeBackend, agent_spec, wire_fakes
from payprobe_common import agent_toolkit as tk

# -- helpers -----------------------------------------------------------------------------


def _publish(client, kind, name, spec, expect=200):
    r = client.post(f"/{kind}s", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    r = client.post(f"/{kind}s/{name}/versions/1/publish")
    assert r.status_code == expect, r.text
    return r


def _wait_run(client, run_id, until=("done", "failed", "cancelled", "rejected", "waiting"), t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in until:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run never reached {until}: {run['status']} {run['node_states']}")


def _wait_hb(client, hb_id, t=5.0, headers=None):
    t0 = time.time()
    while time.time() - t0 < t:
        hb = client.get(f"/heartbeats/{hb_id}", headers=headers).json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat never finished")


def _wf(nodes, edges, inputs=None):
    return {"inputs": inputs or {}, "nodes": nodes, "edges": edges}


def _full_exec():
    return agent_spec(
        role="Exec",
        mode="full",
        tools=["upsert_connection"],
        write_scope={"projects": ["*"], "environments": []},
    )


def _writer_llm():
    return FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {"name": "upsert_connection", "args": {"name": "switch", "config": {"port": 1}}}
                ]
            }
        ],
        final="wrote",
    )


# -- 1. the validator gates every path, not "some ancestor" -----------------------------


def test_gate_a_condition_can_skip_does_not_gate():
    spec = WorkflowSpec.model_validate(
        _wf(
            [
                {"id": "cond", "type": "condition", "expr": "true"},
                {"id": "gate", "type": "approval", "roles": ["admin"]},
                {"id": "apply", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}},
            ],
            [
                {"from": "cond", "to": "gate", "when": "true"},
                {"from": "gate", "to": "apply"},
                {"from": "cond", "to": "apply", "when": "false"},
                {"from": "apply", "to": "end"},
            ],
        )
    )
    gated = approval_gated([n.id for n in spec.nodes], spec)
    assert gated == {"cond": False, "gate": False, "apply": False}
    problems = validate_workflow_spec(spec, lambda ref: None)
    assert any("approval node on every path" in p for p in problems), problems


def test_gate_crossed_on_its_rejected_edge_gates_nothing():
    spec = WorkflowSpec.model_validate(
        _wf(
            [
                {"id": "gate", "type": "approval", "roles": ["admin"]},
                {"id": "apply", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}},
            ],
            [{"from": "gate", "to": "apply", "when": "rejected"}, {"from": "apply", "to": "end"}],
        )
    )
    assert validate_workflow_spec(spec, lambda ref: None)
    ok = WorkflowSpec.model_validate(
        _wf(
            [
                {"id": "gate", "type": "approval", "roles": ["admin"]},
                {"id": "apply", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}},
            ],
            [{"from": "gate", "to": "apply", "when": "approved"}, {"from": "apply", "to": "end"}],
        )
    )
    assert validate_workflow_spec(ok, lambda ref: None) == []


def test_two_gated_paths_and_a_diamond_after_the_gate_are_fine():
    spec = WorkflowSpec.model_validate(
        _wf(
            [
                {"id": "gate", "type": "approval", "roles": ["admin"]},
                {"id": "a", "type": "condition", "expr": "true"},
                {"id": "apply", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}},
            ],
            [
                {"from": "gate", "to": "a"},
                {"from": "a", "to": "apply", "when": "true"},
                {"from": "a", "to": "apply", "when": "false"},
                {"from": "apply", "to": "end"},
            ],
        )
    )
    assert validate_workflow_spec(spec, lambda ref: None) == []


# -- 2. full mode is never woken unattended ---------------------------------------------


def test_full_mode_agent_may_not_carry_an_unattended_trigger():
    for trig in (
        {"kind": "schedule", "interval_sec": 60},
        {"kind": "event", "event": "run.failed"},
        {"kind": "webhook"},
    ):
        from agent_hub.models import AgentSpec

        spec = AgentSpec.model_validate({**_full_exec(), "triggers": [trig]})
        problems = validate_agent_spec(spec)
        assert any("cannot be woken unattended" in p for p in problems), (trig, problems)
    plan = AgentSpec.model_validate(
        {**_full_exec(), "mode": "plan", "triggers": [{"kind": "event", "event": "run.failed"}]}
    )
    assert validate_agent_spec(plan) == []


def test_full_mode_wake_by_a_service_token_is_refused_and_recorded(client, monkeypatch):
    import jwt

    _publish(client, "agent", "exec", _full_exec())
    be = FakeBackend()
    wire_fakes(client.app, be, lambda spec: _writer_llm())
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    now = int(time.time())
    svc = jwt.encode({"sub": "mcp-server", "svc": "mcp-server", "exp": now + 300}, "k", "HS256")
    r = client.post(
        "/agents/exec/wake", json={"input": "go"}, headers={"Authorization": "Bearer " + svc}
    )
    assert r.status_code == 200, r.text  # recorded refusal, nothing accepted
    assert r.json()["status"] == "failed" and "human" in r.json()["error"]
    assert be.connections["switch"]["port"] == 9000
    # a human with the invoke role runs it
    user = jwt.encode({"sub": "ann", "roles": ["admin"], "exp": now + 300}, "k", "HS256")
    r = client.post(
        "/agents/exec/wake", json={"input": "go"}, headers={"Authorization": "Bearer " + user}
    )
    assert r.status_code == 202, r.text
    hb = _wait_hb(client, r.json()["id"], headers={"Authorization": "Bearer " + user})
    assert hb["status"] == "done" and be.connections["switch"]["port"] == 1


# -- 3. the engine re-checks the gate at run time ---------------------------------------


def test_agent_republished_as_full_after_validation_is_stopped_at_run_time(client):
    """M2: the workflow validated against a plan agent; v2 is full. The node
    now fails instead of writing without a gate."""
    _publish(
        client, "agent", "cfg", agent_spec(role="Cfg", mode="plan", tools=["upsert_connection"])
    )
    _publish(
        client,
        "workflow",
        "wf",
        _wf(
            [{"id": "t", "type": "agent_task", "agent": "cfg"}],
            [{"from": "t", "to": "end"}],
        ),
    )
    r = client.post("/agents/cfg/versions", json={"spec": _full_exec()})
    assert r.status_code == 201
    assert client.post("/agents/cfg/versions/2/publish").status_code == 200
    be = FakeBackend()
    wire_fakes(client.app, be, lambda spec: _writer_llm())
    run_id = client.post("/workflows/wf/run", json={"inputs": {}}).json()["id"]
    run = _wait_run(client, run_id, until=("done", "failed"))
    assert run["status"] == "failed"
    assert "no approved gate" in run["node_states"]["t"]["error"]
    assert be.connections["switch"]["port"] == 9000 and not be.calls


def test_write_tool_node_labelled_mock_is_scoped_to_mock(client):
    """H2/F5: the mock label exempts the node from the gate, so the engine
    holds it to that environment; args naming another one are refused."""
    _publish(
        client,
        "workflow",
        "wf",
        _wf(
            [
                {
                    "id": "load",
                    "type": "tool",
                    "tool": "start_load_run",
                    "environment": "mock",
                    "args": {"scenario_ids": ["s1"], "environment_name": "uat", "target_tps": 5},
                }
            ],
            [{"from": "load", "to": "end"}],
        ),
    )
    be = FakeBackend()
    wire_fakes(client.app, be, lambda spec: FakeLLMBackend())
    run_id = client.post("/workflows/wf/run", json={"inputs": {}}).json()["id"]
    run = _wait_run(client, run_id, until=("done", "failed"))
    assert run["status"] == "failed"
    assert "outside" in run["node_states"]["load"]["error"]


def test_pause_stops_tool_nodes_too(client):
    _publish(
        client,
        "workflow",
        "wf",
        _wf(
            [
                {
                    "id": "w",
                    "type": "tool",
                    "tool": "upsert_connection",
                    "environment": "mock",
                    "args": {"name": "switch", "config": {"port": 2}},
                }
            ],
            [{"from": "w", "to": "end"}],
        ),
    )
    be = FakeBackend()
    wire_fakes(client.app, be, lambda spec: FakeLLMBackend())
    assert client.put("/pause", json={"paused": True}).status_code == 200
    run_id = client.post("/workflows/wf/run", json={"inputs": {}}).json()["id"]
    run = _wait_run(client, run_id, until=("done", "failed"))
    assert run["status"] == "failed" and "paused" in run["node_states"]["w"]["error"]
    assert be.connections["switch"]["port"] == 9000
    client.put("/pause", json={"paused": False})


def test_a_failed_node_stops_its_running_siblings(client):
    """L3: one failed branch ends the run; the parallel heartbeat is cancelled."""
    _publish(client, "agent", "slow", agent_spec(role="Slow"))
    _publish(
        client,
        "workflow",
        "wf",
        _wf(
            [
                {"id": "slow", "type": "agent_task", "agent": "slow"},
                {"id": "boom", "type": "condition", "expr": "${inputs.x} > 1"},
            ],
            [{"from": "slow", "to": "end"}, {"from": "boom", "to": "end"}],
            inputs={"x": "a number"},
        ),
    )
    started = []

    class _Stuck(FakeLLMBackend):
        def complete(self, convo, schemas):
            started.append(1)
            time.sleep(0.3)
            return super().complete(convo, schemas)

    wire_fakes(client.app, FakeBackend(), lambda spec: _Stuck(final="late"))
    run_id = client.post("/workflows/wf/run", json={"inputs": {"x": "not-a-number"}}).json()["id"]
    run = _wait_run(client, run_id, until=("done", "failed"))
    assert run["status"] == "failed"
    assert run["node_states"]["slow"]["status"] == "cancelled"
    hb = _wait_hb(client, run["node_states"]["slow"]["heartbeat_id"])
    assert hb["status"] in ("cancelled", "done")  # cancel requested; may already have answered


# -- 4. an editor cannot widen their own grant ------------------------------------------


def test_widens_names_every_grown_field():
    base = _full_exec()
    assert widens(base, base) == []
    assert widens(agent_spec(mode="plan"), agent_spec(mode="full")) == ["mode 'plan' -> 'full'"]
    grown = widens(
        base,
        {
            **base,
            "tools": ["upsert_connection", "delete_connection"],
            "write_scope": {"projects": ["*"], "environments": ["uat"]},
            "rbac": {"invoke": ["operator"], "edit": ["operator"]},
            "triggers": [{"kind": "manual"}, {"kind": "mcp"}],
            "budget": {"daily_tokens": 5000},
            "limits": {"max_steps": 64},
        },
    )
    assert grown == [
        "tools added: delete_connection",
        "write_scope.environments grows",
        "rbac changes",
        "trigger kinds added: mcp",
        "daily token budget grows",
        "per-wake limits grow",
    ]
    # narrowing is never widening
    assert widens({**base, "tools": ["upsert_connection", "get_connection"]}, base) == []


def test_rbac_edit_holder_cannot_publish_a_wider_version(client, monkeypatch):
    import jwt

    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    now = int(time.time())
    op = {
        "Authorization": "Bearer "
        + jwt.encode({"sub": "olga", "roles": ["operator"], "exp": now + 300}, "k", "HS256")
    }
    adm = {
        "Authorization": "Bearer "
        + jwt.encode({"sub": "ann", "roles": ["admin"], "exp": now + 300}, "k", "HS256")
    }
    spec = agent_spec(
        mode="plan", tools=["list_runs"], rbac={"invoke": ["operator"], "edit": ["operator"]}
    )
    assert (
        client.post("/agents", json={"name": "shared", "spec": spec}, headers=adm).status_code
        == 201
    )
    assert client.post("/agents/shared/versions/1/publish", headers=adm).status_code == 200
    # same grant, new instructions: fine
    same = {**spec, "instructions": "Do it differently."}
    assert (
        client.post("/agents/shared/versions", json={"spec": same}, headers=op).status_code == 201
    )
    assert client.post("/agents/shared/versions/2/publish", headers=op).status_code == 200
    # wider: refused with the reasons
    wider = {
        **spec,
        "mode": "full",
        "tools": ["list_runs", "upsert_connection"],
        "write_scope": {"projects": ["*"], "environments": []},
    }
    assert (
        client.post("/agents/shared/versions", json={"spec": wider}, headers=op).status_code == 201
    )
    r = client.post("/agents/shared/versions/3/publish", headers=op)
    assert r.status_code == 403, r.text
    assert "mode 'plan' -> 'full'" in r.json()["detail"]["widens"]
    # an admin may
    assert client.post("/agents/shared/versions/3/publish", headers=adm).status_code == 200


# -- 5. the tool layer ------------------------------------------------------------------


def test_extra_cannot_smuggle_an_environment_past_the_write_scope():
    scope = tk.ToolScope(
        allow=frozenset({"start_load_run"}), mode="full", projects=("*",), environments=("mock",)
    )
    spec = tk.REGISTRY["start_load_run"]
    args = {"scenario_ids": ["s"], "environment_name": "mock", "extra": {"environment_name": "uat"}}
    assert "uat" in (tk.check_write_scope(scope, spec, args) or "")


def test_load_cap_sees_base_tps_and_non_finite_rates():
    scope = tk.ToolScope(
        allow=frozenset({"start_load_run"}),
        mode="full",
        projects=("*",),
        environments=("*",),
        load_tps_cap=100,
    )
    assert tk.check_load_cap(scope, "start_load_run", {"target_tps": 50}) is None
    assert tk.check_load_cap(
        scope, "start_load_run", {"extra": {"base_tps": 5000, "spike_tps": 50}}
    )
    assert tk.check_load_cap(scope, "start_load_run", {"target_tps": "nan"})
    assert tk.check_load_cap(scope, "start_load_run", {"target_tps": "inf"})


def test_every_read_and_execute_result_is_untrusted():
    assert tk.UNTRUSTED_RESULT_TOOLS == frozenset(
        n for n, t in tk.REGISTRY.items() if t.tier in ("read", "execute")
    )
    assert (
        "get_scenario" in tk.UNTRUSTED_RESULT_TOOLS
        and "get_connection" in tk.UNTRUSTED_RESULT_TOOLS
    )


def test_credentials_are_masked_before_the_model_and_the_viewer_see_them(client):
    be = FakeBackend()
    be.connections["issuer"] = {
        "adapter": "tcp",
        "password": "hunter2",
        "config": {"api_key": "k9"},
    }
    scope = tk.ToolScope(allow=frozenset({"get_connection"}), mode="advisor")
    out = tk.scoped_dispatch(
        tk.ToolContext(backend=be), scope, "get_connection", {"name": "issuer"}
    )
    data = out["result"]["data"]
    assert data["password"].startswith("<secret:") and data["config"]["api_key"].startswith(
        "<secret:"
    )
    assert "hunter2" not in str(out) and "k9" not in str(out)
    # a full-mode write journals the plaintext (restore needs it) but the API view masks it
    _publish(client, "agent", "exec", _full_exec())
    wire_fakes(
        client.app,
        be,
        lambda spec: FakeLLMBackend(
            [
                {
                    "tool_calls": [
                        {
                            "name": "upsert_connection",
                            "args": {"name": "issuer", "config": {"password": "new-pw"}},
                        }
                    ]
                }
            ],
            final="rotated",
        ),
    )
    r = client.post("/agents/exec/wake", json={"input": "rotate"})
    assert r.status_code == 202, r.text
    hb = _wait_hb(client, r.json()["id"])
    assert hb["status"] == "done" and len(hb["journal"]) == 1
    blob = str(hb)
    assert "hunter2" not in blob and "new-pw" not in blob and "<secret:" in blob
    # and revert restores the real value from the stored (unmasked) journal
    assert client.post(f"/heartbeats/{hb['id']}/revert").json()["reverted"] == 1
    assert be.connections["issuer"]["password"] == "hunter2"
