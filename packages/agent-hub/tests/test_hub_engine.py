"""Workflow engine over HTTP: approval mid-flow, reviewer gate, condition
branches, tool nodes, cancel, rejection, expiry, restart resume, seeds."""

import json
import threading
import time

from agent_hub.alerts import Alerter
from agent_hub.engine import Engine
from agent_hub.llm import FakeLLMBackend
from agent_hub.seed import WORKFLOW_SEEDS
from hub_testkit import FakeBackend, agent_spec

# -- helpers -----------------------------------------------------------------------------


def _publish(client, kind, name, spec):
    r = client.post(f"/{kind}s", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    r = client.post(f"/{kind}s/{name}/versions/1/publish")
    assert r.status_code == 200, r.text


def _wait_run(client, run_id, until=("done", "failed", "cancelled", "rejected", "waiting"), t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in until:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run never reached {until}: {run['status']} {run['node_states']}")


class _Receiver:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, url, body, headers):
        self.events.append((headers["X-PayProbe-Event"], json.loads(body)["data"]))
        return 200


def _arm_alerts(client):
    rx = _Receiver()
    client.app.state.alerts = Alerter("http://alerts.local/hook", "", backoff=(0.0,), transport=rx)
    client.app.state.engine.alerts = client.app.state.alerts
    return rx


def _wire(client, backend, by_role: dict):
    """LLM factory keyed by the agent's role; a fresh scripted backend per wake."""

    def factory(spec):
        make = by_role.get(spec.role)
        assert make is not None, f"no fake LLM for role {spec.role!r}"
        return make()

    client.app.state.backend_factory = lambda token: backend
    client.app.state.llm_factory = factory


def _observer_workflow():
    return {
        "inputs": {"focus": "free text"},
        "nodes": [
            {
                "id": "observe",
                "type": "agent_task",
                "agent": "obs",
                "input": {"focus": "${inputs.focus}"},
            },
            {
                "id": "review",
                "type": "agent_task",
                "agent": "rev",
                "reviews": "observe",
                "input": {"findings": "${observe.result}"},
            },
            {"id": "gate", "type": "approval", "roles": ["admin"], "timeout_s": 3600},
        ],
        "edges": [
            {"from": "observe", "to": "review"},
            {"from": "review", "to": "gate"},
            {"from": "gate", "to": "end"},
        ],
    }


def _seed_observer_agents(client):
    _publish(client, "agent", "obs", agent_spec(role="Obs"))
    _publish(client, "agent", "rev", agent_spec(role="Rev"))


# -- tests -------------------------------------------------------------------------------


def test_run_pauses_at_the_human_gate_and_finishes_on_approval(client):
    rx = _arm_alerts(client)
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    be = FakeBackend()
    _wire(
        client,
        be,
        {
            "Obs": lambda: FakeLLMBackend(
                [{"tool_calls": [{"name": "list_runs"}]}],
                final='[{"severity": "warn", "headline": "run-1 failed"}]',
            ),
            "Rev": lambda: FakeLLMBackend(final='{"verdict": "approve", "reasons": []}'),
        },
    )
    r = client.post("/workflows/w-obs/run", json={"inputs": {"focus": "runs"}})
    assert r.status_code == 202, r.text
    run = _wait_run(client, r.json()["id"], until=("waiting",))
    st = run["node_states"]
    assert st["observe"]["status"] == "done" and st["observe"]["heartbeat_id"]
    assert st["review"]["status"] == "done"
    assert st["gate"]["status"] == "waiting" and st["gate"]["approval_id"]
    assert run["results"]["review"]["json"] == {"verdict": "approve", "reasons": []}
    # the observer's input was rendered from the run inputs
    hb = client.get(f"/heartbeats/{st['observe']['heartbeat_id']}").json()
    assert json.loads(hb["input"]) == {"focus": "runs"} and hb["wake"] == "event"

    inbox = client.get("/approvals").json()
    assert [a["id"] for a in inbox] == [st["gate"]["approval_id"]]
    ap = inbox[0]
    assert ap["run_id"] == run["id"] and ap["node_id"] == "gate" and ap["roles"] == ["admin"]
    assert ap["context"]["results"]["observe"]["result"].startswith("[")
    assert ap["expires_at"]
    assert ("approval.requested",) in {(e,) for e, _ in rx.events}

    r = client.post(f"/approvals/{ap['id']}/decide", json={"decision": "approved", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    run = _wait_run(client, run["id"], until=("done",))
    assert run["results"]["gate"] == {"decision": "approved", "by": "dev", "note": "ok"}
    assert run["finished_at"] and run["error"] is None
    assert client.get("/approvals").json() == []  # inbox drained
    assert client.get("/approvals?status=all").json()[0]["decided_by"] == "dev"
    listed = client.get("/runs?workflow=w-obs").json()
    assert listed[0]["id"] == run["id"] and listed[0]["n_done"] == 3 and listed[0]["n_nodes"] == 3


def _plan_workflow():
    return {
        "inputs": {"network": "id"},
        "nodes": [
            {
                "id": "plan",
                "type": "agent_task",
                "agent": "planner",
                "input": {"network": "${inputs.network}"},
            },
            {
                "id": "review",
                "type": "agent_task",
                "agent": "rev",
                "reviews": "plan",
                "input": {"plan": "${plan.result}", "calls": "${plan.proposed}"},
            },
            {"id": "verdict", "type": "condition", "expr": '${review.json.verdict} == "approve"'},
            {"id": "gate", "type": "approval", "roles": ["admin"]},
            {
                "id": "apply",
                "type": "agent_task",
                "agent": "exec",
                "input": {"calls": "${plan.proposed}"},
            },
        ],
        "edges": [
            {"from": "plan", "to": "review"},
            {"from": "review", "to": "verdict"},
            {"from": "verdict", "to": "gate", "when": "true"},
            {"from": "verdict", "to": "end", "when": "false"},
            {"from": "gate", "to": "apply"},
            {"from": "apply", "to": "end"},
        ],
    }


def _seed_plan_agents(client):
    _publish(
        client,
        "agent",
        "planner",
        agent_spec(
            role="Planner",
            mode="plan",
            tools=["upsert_connection", "list_connections"],
            write_scope={"projects": ["*"], "environments": []},
        ),
    )
    _publish(client, "agent", "rev", agent_spec(role="Rev"))
    _publish(
        client,
        "agent",
        "exec",
        agent_spec(
            role="Exec",
            mode="full",
            tools=["upsert_connection"],
            write_scope={"projects": ["*"], "environments": []},
        ),
    )


def _planner_llm():
    return FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {
                        "name": "upsert_connection",
                        "args": {"name": "switch", "config": {"port": 9999}},
                    }
                ]
            }
        ],
        final="plan: raise switch port to 9999",
    )


def test_reviewer_blocks_a_bad_plan_before_any_human_is_asked(client):
    _seed_plan_agents(client)
    _publish(client, "workflow", "w-plan", _plan_workflow())
    be = FakeBackend()
    _wire(
        client,
        be,
        {
            "Planner": _planner_llm,
            "Rev": lambda: FakeLLMBackend(
                final='{"verdict": "changes_requested", "reasons": ["no evidence"]}'
            ),
            "Exec": lambda: FakeLLMBackend(),
        },
    )
    run_id = client.post("/workflows/w-plan/run", json={"inputs": {"network": "n1"}}).json()["id"]
    run = _wait_run(client, run_id, until=("done", "failed", "rejected"))
    assert run["status"] == "done", run
    st = run["node_states"]
    assert st["verdict"]["status"] == "done" and run["results"]["verdict"] == {"value": False}
    assert st["gate"]["status"] == "skipped" and st["apply"]["status"] == "skipped"
    assert client.get("/approvals").json() == []  # nobody was asked
    assert be.connections["switch"]["port"] == 9000  # nothing was applied
    # the plan-mode proposal became a durable plan artifact
    plans = client.get(f"/plans?run={run_id}").json()
    assert len(plans) == 1 and plans[0]["node_id"] == "plan"
    assert plans[0]["proposed"][0]["tool"] == "upsert_connection"
    assert run["results"]["plan"]["plan_id"] == plans[0]["id"]
    assert client.get(f"/plans/{plans[0]['id']}").json()["agent"] == "planner"


def test_approved_plan_is_applied_by_the_full_mode_executor(client):
    _seed_plan_agents(client)
    _publish(client, "workflow", "w-plan", _plan_workflow())
    be = FakeBackend()
    _wire(
        client,
        be,
        {
            "Planner": _planner_llm,
            "Rev": lambda: FakeLLMBackend(final='{"verdict": "approve"}'),
            "Exec": lambda: FakeLLMBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "name": "upsert_connection",
                                "args": {"name": "switch", "config": {"port": 9999}},
                            }
                        ]
                    }
                ],
                final="applied 1 call",
            ),
        },
    )
    run_id = client.post("/workflows/w-plan/run", json={"inputs": {"network": "n1"}}).json()["id"]
    run = _wait_run(client, run_id, until=("waiting",))
    assert be.connections["switch"]["port"] == 9000  # plan mode proposed, did not write
    ap = client.get("/approvals").json()[0]
    assert ap["context"]["plans"] == [run["results"]["plan"]["plan_id"]]
    client.post(f"/approvals/{ap['id']}/decide", json={"decision": "approved"})
    run = _wait_run(client, run_id, until=("done", "failed"))
    assert run["status"] == "done", run
    assert run["node_states"]["apply"]["status"] == "done"
    assert run["node_states"]["apply"]["mode"] == "full"
    assert be.connections["switch"]["port"] == 9999  # the executor applied it
    apply_hb = client.get(f"/heartbeats/{run['node_states']['apply']['heartbeat_id']}").json()
    assert len(apply_hb["journal"]) == 1  # and it is revertable


def test_rejection_ends_the_run_rejected(client):
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    _wire(
        client,
        FakeBackend(),
        {"Obs": lambda: FakeLLMBackend(final="[]"), "Rev": lambda: FakeLLMBackend(final="{}")},
    )
    run_id = client.post("/workflows/w-obs/run", json={"inputs": {"focus": "x"}}).json()["id"]
    _wait_run(client, run_id, until=("waiting",))
    ap = client.get("/approvals").json()[0]
    r = client.post(f"/approvals/{ap['id']}/decide", json={"decision": "rejected", "note": "no"})
    assert r.status_code == 200
    run = _wait_run(client, run_id, until=("rejected",))
    assert "rejected by dev" in run["error"]
    assert run["results"]["gate"]["decision"] == "rejected"
    # a decided approval cannot be decided again
    assert (
        client.post(f"/approvals/{ap['id']}/decide", json={"decision": "approved"}).status_code
        == 409
    )


def test_cancel_while_waiting_closes_the_approval(client):
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    _wire(
        client,
        FakeBackend(),
        {"Obs": lambda: FakeLLMBackend(final="[]"), "Rev": lambda: FakeLLMBackend(final="{}")},
    )
    run_id = client.post("/workflows/w-obs/run", json={"inputs": {"focus": "x"}}).json()["id"]
    _wait_run(client, run_id, until=("waiting",))
    r = client.post(f"/runs/{run_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["node_states"]["gate"]["status"] == "cancelled"
    assert client.get("/approvals").json() == []
    assert client.get("/approvals?status=cancelled").json()[0]["run_id"] == run_id
    assert client.post(f"/runs/{run_id}/cancel").status_code == 409  # not active any more


def test_condition_routes_and_tool_node_runs_under_the_user(client):
    wf = {
        "inputs": {"tps": "number"},
        "nodes": [
            {"id": "big", "type": "condition", "expr": "${inputs.tps} > 100"},
            {"id": "look", "type": "tool", "tool": "list_connections", "args": {}},
        ],
        "edges": [
            {"from": "big", "to": "look", "when": "true"},
            {"from": "big", "to": "end", "when": "false"},
            {"from": "look", "to": "end"},
        ],
    }
    _publish(client, "workflow", "w-cond", wf)
    be = FakeBackend()
    _wire(client, be, {})
    small = client.post("/workflows/w-cond/run", json={"inputs": {"tps": 50}}).json()
    assert small["status"] == "done" and small["node_states"]["look"]["status"] == "skipped"
    big = client.post("/workflows/w-cond/run", json={"inputs": {"tps": 500}}).json()
    assert big["status"] == "done", big
    assert big["node_states"]["look"]["status"] == "done"
    assert big["results"]["look"]["ok"] is True
    assert [c["name"] for c in big["results"]["look"]["result"]] == ["switch"]
    assert big["node_states"]["look"]["journal"] == []  # a read leaves no journal


def test_write_tool_node_outside_mock_needs_an_approval_ancestor():
    from agent_hub.models import WorkflowSpec
    from agent_hub.validate import validate_workflow_spec

    spec = WorkflowSpec.model_validate(
        {
            "nodes": [
                {"id": "w", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}}
            ],
            "edges": [{"from": "w", "to": "end"}],
        }
    )
    problems = validate_workflow_spec(spec, lambda ref: None)
    assert any("needs an approval node" in p for p in problems)
    ok = WorkflowSpec.model_validate(
        {
            "nodes": [
                {"id": "g", "type": "approval", "roles": ["admin"]},
                {"id": "w", "type": "tool", "tool": "delete_connection", "args": {"name": "x"}},
            ],
            "edges": [{"from": "g", "to": "w"}, {"from": "w", "to": "end"}],
        }
    )
    assert validate_workflow_spec(ok, lambda ref: None) == []
    bad_label = WorkflowSpec.model_validate(
        {
            "nodes": [{"id": "c", "type": "condition", "expr": "true"}],
            "edges": [{"from": "c", "to": "end", "when": "yes"}],
        }
    )
    assert any("'true'/'false'" in p for p in validate_workflow_spec(bad_label, lambda ref: None))


def test_missing_inputs_are_refused_and_unknown_workflow_is_404(client):
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    r = client.post("/workflows/w-obs/run", json={"inputs": {}})
    assert r.status_code == 422 and "missing input 'focus'" in r.text
    assert client.post("/workflows/nope/run", json={}).status_code == 404


def test_approval_timeout_fails_the_run_on_the_tick(client):
    rx = _arm_alerts(client)
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    _wire(
        client,
        FakeBackend(),
        {"Obs": lambda: FakeLLMBackend(final="[]"), "Rev": lambda: FakeLLMBackend(final="{}")},
    )
    run_id = client.post("/workflows/w-obs/run", json={"inputs": {"focus": "x"}}).json()["id"]
    _wait_run(client, run_id, until=("waiting",))
    ap = client.get("/approvals").json()[0]
    store = client.app.state.store
    client.portal.call(
        store._pool.execute,
        "UPDATE agent_hub_approvals SET expires_at = NOW() - interval '1 minute' WHERE id=$1",
        ap["id"],
    )
    client.portal.call(client.app.state.engine.tick)
    run = _wait_run(client, run_id, until=("failed",))
    assert "approval timed out" in run["error"]
    assert client.get(f"/approvals/{ap['id']}").json()["status"] == "expired"
    time.sleep(0.05)
    assert any(e == "run.failed" and d["run_id"] == run_id for e, d in rx.events)


def test_restart_mid_run_resumes_from_the_row(client):
    """Kill the process while an agent task runs: a fresh engine over the same
    database re-attaches to the heartbeat and finishes the workflow."""
    _seed_observer_agents(client)
    _publish(client, "workflow", "w-obs", _observer_workflow())
    gate = threading.Event()

    class Slow(FakeLLMBackend):
        def complete(self, convo, tools):
            gate.wait(5)
            return super().complete(convo, tools)

    _wire(
        client,
        FakeBackend(),
        {"Obs": lambda: Slow(final="[]"), "Rev": lambda: FakeLLMBackend(final="{}")},
    )
    run_id = client.post("/workflows/w-obs/run", json={"inputs": {"focus": "x"}}).json()["id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "running" and run["node_states"]["observe"]["status"] == "running"

    # "restart": the old engine object is gone; a new one learns everything from the row
    old = client.app.state.engine
    old.stop()
    fresh = Engine(
        client.app.state.store,
        client.app.state.alerts,
        launch_heartbeat=old.launch_heartbeat,
        tool_backend=old.tool_backend,
        retry_s=0.05,
    )
    client.app.state.engine = fresh
    touched = client.portal.call(fresh.reconcile)
    assert touched == [run_id]
    gate.set()  # the heartbeat finishes now; the fresh engine's watcher picks it up
    run = _wait_run(client, run_id, until=("waiting",))
    assert run["node_states"]["observe"]["status"] == "done"
    assert run["node_states"]["review"]["status"] == "done"
    assert run["node_states"]["gate"]["status"] == "waiting"


def test_reference_workflows_are_seeded_and_valid(client):
    names = {w["name"] for w in client.get("/workflows").json()}
    assert set(WORKFLOW_SEEDS) <= names
    for name in WORKFLOW_SEEDS:
        d = client.get(f"/workflows/{name}").json()
        assert d["builtin"] is True and d["active_version"] == 1
        v = client.post(f"/workflows/{name}/versions/1/validate").json()
        assert v["valid"], v
    cp = client.get("/workflows/certification-plan").json()["spec"]
    assert [n["id"] for n in cp["nodes"]] == ["plan", "review", "verdict", "gate", "apply"]
    assert cp["nodes"][-1]["agent"] == "plan-executor"
    # the executor is full mode, approval-gated: the validator would refuse it otherwise
    assert client.get("/agents/plan-executor").json()["spec"]["mode"] == "full"
