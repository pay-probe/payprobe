"""Console controls: weighted traffic mix resolution + hot re-tune endpoint."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402


# -- weighted mix resolution -------------------------------------------------

async def test_resolve_load_mix_resolves_scenarios_and_weights(monkeypatch):
    async def fake_env(environment, environment_name):
        return {"name": "test"}

    async def fake_fetch(ids):
        return [{"id": i, "name": i, "steps": []} for i in ids]

    async def fake_attach(scs):  # no test-data pools in this test
        return None

    monkeypatch.setattr(m, "_resolve_run_env", fake_env)
    monkeypatch.setattr(m, "_fetch_scenarios_by_ids", fake_fetch)
    monkeypatch.setattr(m, "_attach_test_data", fake_attach)

    req = m.LoadRunRequest(
        type="steady", duration_s=5, workers=1, target_tps=100,
        mix=[
            {"scenario_id": "payment", "weight": 80},
            {"scenario_id": "refund", "weight": 15},
            {"scenario_id": "reversal", "weight": 5},
        ],
    )
    env, scenarios, weights, label = await m._resolve_load_mix(req)
    assert env["name"] == "test"
    assert [s["id"] for s in scenarios] == ["payment", "refund", "reversal"]
    assert weights == [80.0, 15.0, 5.0]
    assert "payment@80" in label


async def test_resolve_load_mix_without_mix_falls_back_to_single(monkeypatch):
    async def fake_single(req):
        return {"name": "e"}, {"id": "only", "steps": []}, "lbl"

    monkeypatch.setattr(m, "_resolve_load_transaction", fake_single)
    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=10)
    env, scenarios, weights, label = await m._resolve_load_mix(req)
    assert [s["id"] for s in scenarios] == ["only"]
    assert weights == [1.0] and label == "lbl"


async def test_resolve_load_mix_rejects_entry_without_scenario(monkeypatch):
    async def fake_env(*a):
        return {}

    async def fake_fetch(ids):
        return []

    monkeypatch.setattr(m, "_resolve_run_env", fake_env)
    monkeypatch.setattr(m, "_fetch_scenarios_by_ids", fake_fetch)
    req = m.LoadRunRequest(type="steady", duration_s=5, workers=1, target_tps=10,
                           mix=[{"weight": 1}])
    with pytest.raises(HTTPException) as exc:
        await m._resolve_load_mix(req)
    assert exc.value.status_code == 400


# -- hot re-tune endpoint ----------------------------------------------------

async def test_retune_endpoint_calls_coordinator(monkeypatch):
    seen = {}

    async def fake_retune(run_id, params):
        seen["run_id"] = run_id
        seen["params"] = params
        return True

    monkeypatch.setattr(m.load_coordinator, "retune", fake_retune)
    out = await m.retune_load_run("run1", m.RetuneRequest(target_tps=500))
    assert out["status"] == "retuned"
    assert seen["run_id"] == "run1" and seen["params"] == {"target_tps": 500.0}


async def test_retune_endpoint_400_when_empty():
    with pytest.raises(HTTPException) as exc:
        await m.retune_load_run("run1", m.RetuneRequest())
    assert exc.value.status_code == 400


async def test_retune_endpoint_404_when_not_running(monkeypatch):
    async def fake_retune(run_id, params):
        return False

    monkeypatch.setattr(m.load_coordinator, "retune", fake_retune)
    with pytest.raises(HTTPException) as exc:
        await m.retune_load_run("ghost", m.RetuneRequest(target_tps=10))
    assert exc.value.status_code == 404
