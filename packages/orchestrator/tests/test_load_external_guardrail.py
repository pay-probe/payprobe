"""The load engine refuses ``external: true`` connections (ADR-0009).

A connection marked external points at a real provider sandbox/live API.
Load-testing one violates the provider's terms of service, so the refusal is
enforced in the registry layer at ``POST /load-runs`` — loudly, before any
worker dispatch — never left to documentation (invariant #6). Functional runs
and the playground stay allowed; that boundary is the point.
"""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


def _scenario(target="stripe_sandbox", connection=None):
    step = {"id": "s1", "kind": "action", "target": target, "action": "create_intent"}
    if connection:
        step["config"] = {"connection": connection}
    return {"id": "sc1", "name": "stripe auth", "steps": [step]}


def _patch_resolution(monkeypatch, env, scenarios, conns=None):
    async def fake_mix(req):
        return env, scenarios, [1.0], "test"

    async def fake_get_json(url, *a, **k):
        if url.endswith("/connections"):
            return conns or []
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(m, "_resolve_load_mix", fake_mix)
    monkeypatch.setattr(m, "_http_get_json", fake_get_json)


async def test_external_env_adapter_refused(store, monkeypatch):
    env = {"adapters": {"stripe_sandbox": {"adapter": "http", "external": True}}}
    _patch_resolution(monkeypatch, env, [_scenario()])

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)

    assert exc.value.status_code == 400
    assert "external" in exc.value.detail
    assert "stripe_sandbox" in exc.value.detail
    assert "simulator" in exc.value.detail  # the message names the way out
    assert store.list() == []  # refused before any run record exists


async def test_external_connection_doc_refused_on_mix_path(store, monkeypatch):
    # the mix path doesn't attach connections into env — the registry doc is
    # the source of truth there
    env = {"adapters": {}}
    conns = [{"name": "paypal_sandbox", "adapter": "http", "external": True}]
    _patch_resolution(
        monkeypatch, env, [_scenario(target="http", connection="paypal_sandbox")], conns
    )

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)
    assert exc.value.status_code == 400
    assert "paypal_sandbox" in exc.value.detail


async def test_external_group_member_refused(store, monkeypatch):
    env = {
        "adapters": {
            "psp_pool": {
                "adapter": "group",
                "members": [
                    {"connection": "local_sim", "config": {"adapter": "http"}},
                    {
                        "connection": "adyen_test",
                        "config": {"adapter": "http", "external": True},
                    },
                ],
            }
        }
    }
    _patch_resolution(monkeypatch, env, [_scenario(target="psp_pool")])

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)
    assert exc.value.status_code == 400
    assert "adyen_test" in exc.value.detail


async def test_registry_unreachable_still_checks_env_configs(store, monkeypatch):
    env = {"adapters": {"stripe_sandbox": {"adapter": "http", "external": True}}}

    async def fake_mix(req):
        return env, [_scenario()], [1.0], "test"

    async def broken_get_json(url, *a, **k):
        raise ConnectionError("scenario-service down")

    monkeypatch.setattr(m, "_resolve_load_mix", fake_mix)
    monkeypatch.setattr(m, "_http_get_json", broken_get_json)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)
    assert exc.value.status_code == 400


async def test_non_external_target_starts_normally(store, monkeypatch):
    env = {"adapters": {"stripe_sim_local": {"adapter": "http"}}}
    _patch_resolution(monkeypatch, env, [_scenario(target="stripe_sim_local")])

    started = {}

    async def fake_start(run_id, profile, env, scenario, **kw):
        started["run_id"] = run_id

    monkeypatch.setattr(m.load_coordinator, "start", fake_start)

    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    out = await m.create_load_run(req)
    assert out["status"] == "running"
    assert started["run_id"] == out["run_id"]


def test_external_load_targets_helper_dedupes_and_sorts():
    env = {"adapters": {"b_ext": {"external": True}, "a_ext": {"external": True}}}
    scs = [
        {
            "steps": [
                {"id": "1", "kind": "action", "target": "b_ext"},
                {"id": "2", "kind": "action", "target": "a_ext"},
                {"id": "3", "kind": "action", "target": "b_ext"},
                {"id": "4", "kind": "init", "target": "b_ext"},  # non-action skipped
            ]
        }
    ]
    assert m._external_load_targets(env, scs, {}) == ["a_ext", "b_ext"]


# -- database probes (ADR-0012) ------------------------------------------------------


async def test_probe_in_a_load_scenario_is_refused_unless_load_ok(store, monkeypatch):
    env = {"adapters": {"core_db": {"adapter": "db_probe_core", "engine": "sqlite"}}}
    _patch_resolution(monkeypatch, env, [_scenario(target="core_db")])
    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=20)
    with pytest.raises(HTTPException) as exc:
        await m.create_load_run(req)
    assert exc.value.status_code == 400
    assert "core_db" in exc.value.detail and "database probe" in exc.value.detail
    assert "load_ok" in exc.value.detail  # the message names the opt-in
    assert store.list() == []


def test_probe_load_ok_opt_in_and_registry_doc_path():
    env = {"adapters": {"core_db": {"adapter": "db_probe_core", "engine": "sqlite", "load_ok": True}}}
    assert m._probe_load_targets(env, [_scenario(target="core_db")], {}) == []
    # mix path: the registry doc is the source of truth, keyed by the step's connection
    conns = {"switch_db": {"name": "switch_db", "adapter": "db_probe_switch"}}
    assert m._probe_load_targets({"adapters": {}}, [_scenario(target="db_probe_switch", connection="switch_db")], conns) == ["db_probe_switch", "switch_db"]
    # the target name itself resolving to a probe impl (default-connection type)
    assert m._probe_load_targets({"adapters": {}}, [_scenario(target="db_probe_core")], {}) == ["db_probe_core"]


def test_probe_guardrail_exempts_mocked_probes():
    """The default load mix runs every example (two carry a probe step) under the
    mock environment; a mocked probe touches no database, so it is not refused."""
    assert m._probe_load_targets({"mode": "mock", "adapters": {}}, [_scenario(target="db_probe_core")], {}) == []
    env = {"adapters": {"core_db": {"adapter": "db_probe_core", "mock": True}}}
    assert m._probe_load_targets(env, [_scenario(target="core_db")], {}) == []
