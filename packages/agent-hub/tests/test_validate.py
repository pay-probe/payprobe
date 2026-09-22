"""Registry validation — guardrails in machinery, checked at publish time."""

import pytest
from agent_hub.models import AgentSpec, Node, Trigger, WorkflowSpec
from agent_hub.validate import (
    tool_catalog,
    validate_agent_spec,
    validate_workflow_spec,
)
from conftest import agent_spec
from pydantic import ValidationError

# -- shapes ------------------------------------------------------------------------


def test_catalog_lists_the_toolkit_with_tiers():
    cat = tool_catalog()
    names = {t["name"] for t in cat}
    assert {"list_runs", "upsert_connection", "start_load_run"} <= names
    tiers = {t["name"]: t["tier"] for t in cat}
    assert tiers["upsert_connection"] == "write"
    assert tiers["start_load_run"] == "execute"


def test_agent_spec_defaults_are_conservative():
    s = AgentSpec.model_validate(agent_spec())
    assert s.mode == "advisor"  # as given
    assert AgentSpec.model_validate(agent_spec(mode=None) | {"mode": "plan"}).mode == "plan"
    assert AgentSpec(role="r", instructions="i").mode == "plan"  # D3 default
    assert s.write_scope.projects == [] and s.write_scope.environments == []
    assert s.limits.max_steps == 8 and s.budget.hard_stop is True
    assert [t.kind for t in s.triggers] == ["manual"]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "schedule"},  # no cadence
        {"kind": "schedule", "daily_at": "25:00"},
        {"kind": "event"},  # no event name
        {"kind": "manual", "interval_sec": 60},  # stray field
    ],
)
def test_trigger_shapes(bad):
    with pytest.raises(ValidationError):
        Trigger.model_validate(bad)


def test_agent_spec_field_limits():
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(agent_spec(instructions="x" * 20_001))
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(agent_spec(tools=["list_runs", "list_runs"]))
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(agent_spec(temperature=3))
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(agent_spec(limits={"max_steps": 0}))


def test_node_shapes():
    with pytest.raises(ValidationError):
        Node(id="end", type="approval", roles=["admin"])  # reserved id
    with pytest.raises(ValidationError):
        Node(id="a", type="agent_task")  # no agent
    with pytest.raises(ValidationError):
        Node(id="a", type="tool", agent="x")  # agent on non-task
    with pytest.raises(ValidationError):
        Node(id="a", type="approval")  # no roles
    with pytest.raises(ValidationError):
        Node(id="a", type="agent_task", agent="Bad Name")


# -- agent rules ---------------------------------------------------------------------


def test_unknown_tool_is_refused():
    p = validate_agent_spec(AgentSpec.model_validate(agent_spec(tools=["nope"])))
    assert p and "unknown tool 'nope'" in p[0]


def test_advisor_cannot_hold_write_or_execute_tools():
    spec = AgentSpec.model_validate(
        agent_spec(mode="advisor", tools=["list_runs", "upsert_connection", "start_load_run"])
    )
    p = validate_agent_spec(spec)
    assert len(p) == 2
    assert all("advisor mode is read-only" in x for x in p)


def test_plan_mode_may_list_write_tools():
    spec = AgentSpec.model_validate(agent_spec(mode="plan", tools=["upsert_connection"]))
    assert validate_agent_spec(spec) == []


def test_full_mode_needs_write_scope():
    spec = AgentSpec.model_validate(agent_spec(mode="full", tools=["upsert_connection"]))
    assert any("write_scope" in x for x in validate_agent_spec(spec))
    ok = AgentSpec.model_validate(
        agent_spec(
            mode="full",
            tools=["upsert_connection"],
            write_scope={"projects": ["p1"], "environments": ["mock"]},
        )
    )
    assert validate_agent_spec(ok) == []


def test_model_allowlist_env(monkeypatch):
    monkeypatch.setenv("AGENT_HUB_MODEL_ALLOWLIST", "gpt-4o-mini, claude-sonnet")
    assert validate_agent_spec(AgentSpec.model_validate(agent_spec(model="gpt-4o-mini"))) == []
    p = validate_agent_spec(AgentSpec.model_validate(agent_spec(model="other")))
    assert p and "AGENT_HUB_MODEL_ALLOWLIST" in p[0]
    monkeypatch.delenv("AGENT_HUB_MODEL_ALLOWLIST")
    assert validate_agent_spec(AgentSpec.model_validate(agent_spec(model="other"))) == []


# -- workflow rules ------------------------------------------------------------------


def _registry(**agents):
    """A resolver over in-memory (name → mode) pairs, versioned as v1."""
    specs = {n: AgentSpec.model_validate(agent_spec(mode=m, tools=[])) for n, m in agents.items()}

    def resolve(ref):
        name, _, ver = ref.partition("@")
        if name in specs and ver in ("", "1"):  # only v1 exists, like the store
            return name, 1, specs[name]
        return None

    return resolve


def _wf(nodes, edges):
    return WorkflowSpec.model_validate({"nodes": nodes, "edges": edges})


def test_valid_linear_workflow():
    wf = _wf(
        [
            {"id": "a", "type": "tool", "tool": "list_runs"},
            {"id": "b", "type": "agent_task", "agent": "obs"},
        ],
        [{"from": "a", "to": "b"}, {"from": "b", "to": "end"}],
    )
    assert validate_workflow_spec(wf, _registry(obs="advisor")) == []


def test_duplicate_ids_and_bad_edges():
    wf = _wf(
        [
            {"id": "a", "type": "tool", "tool": "list_runs"},
            {"id": "a", "type": "tool", "tool": "list_runs"},
        ],
        [{"from": "a", "to": "zzz"}],
    )
    p = validate_workflow_spec(wf, _registry())
    assert any("duplicate node ids" in x for x in p)
    assert any("unknown node 'zzz'" in x for x in p)


def test_cycle_is_refused():
    wf = _wf(
        [
            {"id": "a", "type": "tool", "tool": "list_runs"},
            {"id": "b", "type": "tool", "tool": "list_runs"},
        ],
        [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    )
    assert any("cycle" in x for x in validate_workflow_spec(wf, _registry()))


def test_unknown_tool_node_and_unresolved_agent():
    wf = _wf(
        [
            {"id": "a", "type": "tool", "tool": "nope"},
            {"id": "b", "type": "agent_task", "agent": "ghost"},
            {"id": "c", "type": "agent_task", "agent": "obs@7"},
        ],
        [],
    )
    p = validate_workflow_spec(wf, _registry(obs="advisor"))
    assert any("unknown tool 'nope'" in x for x in p)
    assert any("an active version of 'ghost'" in x for x in p)
    assert any("obs@7" in x for x in p)


def test_node_cannot_escalate_agent_mode():
    wf = _wf([{"id": "a", "type": "agent_task", "agent": "obs", "mode": "plan"}], [])
    p = validate_workflow_spec(wf, _registry(obs="advisor"))
    assert p and "escalates" in p[0]
    # narrowing is fine
    wf = _wf([{"id": "a", "type": "agent_task", "agent": "cfg", "mode": "advisor"}], [])
    assert validate_workflow_spec(wf, _registry(cfg="full")) == []


def test_full_mode_needs_approval_ancestor_outside_mock():
    apply = {"id": "apply", "type": "agent_task", "agent": "cfg", "environment": "uat"}
    # no approval anywhere → refused
    wf = _wf([apply], [])
    p = validate_workflow_spec(wf, _registry(cfg="full"))
    assert p and "needs an approval node before it" in p[0]
    # approval as a sibling, not an ancestor → still refused
    wf = _wf(
        [apply, {"id": "gate", "type": "approval", "roles": ["admin"]}],
        [{"from": "apply", "to": "gate"}],
    )
    assert validate_workflow_spec(wf, _registry(cfg="full"))
    # approval upstream (transitively) → ok
    wf = _wf(
        [
            {"id": "gate", "type": "approval", "roles": ["admin"]},
            {"id": "mid", "type": "tool", "tool": "list_runs"},
            apply,
        ],
        [{"from": "gate", "to": "mid"}, {"from": "mid", "to": "apply"}],
    )
    assert validate_workflow_spec(wf, _registry(cfg="full")) == []
    # mock environment needs no gate
    wf = _wf([dict(apply, environment="mock")], [])
    assert validate_workflow_spec(wf, _registry(cfg="full")) == []
    # the mode may come from the node override too
    wf = _wf([{"id": "apply", "type": "agent_task", "agent": "cfg", "mode": "full"}], [])
    assert validate_workflow_spec(wf, _registry(cfg="full"))


def test_executor_and_reviewer_must_differ():
    wf = _wf(
        [
            {"id": "do", "type": "agent_task", "agent": "planner"},
            {"id": "rev", "type": "agent_task", "agent": "planner@1", "reviews": "do"},
        ],
        [{"from": "do", "to": "rev"}],
    )
    p = validate_workflow_spec(wf, _registry(planner="plan"))
    assert p and "executor and reviewer must differ" in p[0]
    wf = _wf(
        [
            {"id": "do", "type": "agent_task", "agent": "planner"},
            {"id": "rev", "type": "agent_task", "agent": "reviewer", "reviews": "do"},
        ],
        [{"from": "do", "to": "rev"}],
    )
    assert validate_workflow_spec(wf, _registry(planner="plan", reviewer="advisor")) == []
    wf = _wf([{"id": "rev", "type": "agent_task", "agent": "reviewer", "reviews": "nope"}], [])
    assert any(
        "reviews unknown" in x for x in validate_workflow_spec(wf, _registry(reviewer="advisor"))
    )
