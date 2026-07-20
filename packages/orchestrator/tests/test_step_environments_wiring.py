"""Orchestrator wiring: per-step environment_override -> run env + step targets."""
import pytest

from orchestrator.api import main as m


@pytest.fixture()
def envs(monkeypatch):
    """Stub the environment registry lookup used by _attach_step_environments."""
    catalogue = {
        "staging": {
            "name": "staging",
            "adapters": {
                "tcp_iso8583": {"name": "tcp_iso8583", "host": "stg.example", "port": 7100},
            },
        },
        "prod": {
            "name": "prod",
            "adapters": {
                "tcp_iso8583": {"host": "prod.example", "port": 7200},
            },
        },
    }

    async def fake_load(name):
        if name not in catalogue:
            raise m.HTTPException(400, f"unknown environment '{name}'")
        return catalogue[name]

    monkeypatch.setattr(m, "_load_env_by_name", fake_load)
    return catalogue


def _scn(*overrides):
    """A scenario whose action steps carry the given environment_override values."""
    steps = [
        {"id": f"s{i}", "kind": "action", "target": "tcp_iso8583",
         "action": "send_0200", "environment_override": ov}
        for i, ov in enumerate(overrides)
    ]
    return [{"id": "sc", "steps": steps}]


async def test_override_env_adapter_merged_and_step_repointed(envs):
    env = {"mode": "real", "adapters": {"tcp_iso8583": {"host": "default", "port": 1}}}
    scs = _scn("staging")
    await m._attach_step_environments(env, scs)
    # the override env's adapter is registered under a derived, non-colliding key
    assert env["adapters"]["tcp_iso8583@staging"]["host"] == "stg.example"
    assert "name" not in env["adapters"]["tcp_iso8583@staging"]
    # the default adapter is left intact for non-overriding steps
    assert env["adapters"]["tcp_iso8583"]["host"] == "default"
    # the step is repointed at the derived adapter, action preserved
    step = scs[0]["steps"][0]
    assert step["target"] == "tcp_iso8583@staging"
    assert step["action"] == "send_0200"
    assert step["config"]["_environment_override"] == "staging"
    assert step["config"]["_catalog_target"] == "tcp_iso8583"


async def test_no_overrides_leaves_env_untouched(envs):
    env = {"mode": "real", "adapters": {"tcp_iso8583": {"host": "default"}}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "action", "target": "tcp_iso8583", "action": "x"}]}]
    await m._attach_step_environments(env, scs)
    assert env["adapters"] == {"tcp_iso8583": {"host": "default"}}
    assert scs[0]["steps"][0]["target"] == "tcp_iso8583"


async def test_unknown_environment_is_best_effort(envs):
    env = {"adapters": {"tcp_iso8583": {"host": "default"}}}
    scs = _scn("does_not_exist")
    await m._attach_step_environments(env, scs)  # must not raise
    # the step stays on its default target; nothing injected
    assert scs[0]["steps"][0]["target"] == "tcp_iso8583"
    assert "tcp_iso8583@does_not_exist" not in env["adapters"]


async def test_override_env_without_matching_adapter_leaves_step(envs):
    env = {"adapters": {"grpc": {"host": "default"}}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "action", "target": "grpc", "action": "call",
         "environment_override": "staging"}]}]  # staging has no 'grpc' adapter
    await m._attach_step_environments(env, scs)
    assert scs[0]["steps"][0]["target"] == "grpc"
    assert "grpc@staging" not in env["adapters"]


async def test_control_nodes_ignored(envs):
    env = {"adapters": {}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "if", "target": "", "environment_override": "staging"}]}]
    await m._attach_step_environments(env, scs)
    assert env["adapters"] == {}
    assert scs[0]["steps"][0]["target"] == ""
