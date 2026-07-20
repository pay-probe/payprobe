"""Test-case packs: list/get/install + the built-in scenarios actually pass."""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api.main as m  # noqa: E402
from models.pack import BUILTIN_PACKS, list_packs  # noqa: E402


def _client():
    return TestClient(m.app)


def test_list_and_get_packs():
    with _client() as c:
        listed = c.get("/packs").json()
        assert {p["id"] for p in listed} == {p.id for p in BUILTIN_PACKS}
        first = listed[0]["id"]
        detail = c.get(f"/packs/{first}").json()
        assert detail["cases"] and "scenario" in detail["cases"][0]
        assert c.get("/packs/nope").status_code == 404


def test_install_creates_project_with_scenarios():
    with _client() as c:
        res = c.post("/packs/switch_settlement/install")
        assert res.status_code == 200
        body = res.json()
        assert body["imported"] == 2
        # scenarios now exist under the new project
        scenarios = c.get(f"/scenarios?project_id={body['project_id']}").json()
        names = {s["name"] for s in scenarios}
        assert names == set(body["scenario_names"])


@pytest.mark.parametrize("pack", list_packs(), ids=lambda p: p.id)
async def test_pack_scenarios_pass_in_mock(pack):
    """Every built-in pack case must run green in mock mode (runnable baseline)."""
    from worker.engine import WorkerEngine, InMemorySink, PASSED

    scenarios = [{"id": case.id, **case.scenario} for case in pack.cases]
    engine = WorkerEngine({"mode": "mock", "adapters": {}}, InMemorySink())
    summary = await engine.run_scenario_batch(scenarios, run_id=f"pack-{pack.id}")
    statuses = {s["name"]: s["status"] for s in summary["scenarios"]}
    assert all(v == PASSED for v in statuses.values()), statuses
