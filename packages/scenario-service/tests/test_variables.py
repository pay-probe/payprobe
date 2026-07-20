"""Tests for scoped variables (global / project / set / scenario) + merge."""

import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _scenario(name="s1", variables=None):
    return {
        "name": name, "description": "", "tags": [], "stop_on_failure": True,
        "timeout_ms": 5000, "test_class": "e2e", "variables": variables or {},
        "steps": [], "edges": [],
    }


def test_global_variables_roundtrip(client):
    client.put("/variables/global", json={"variables": {"currency": "USD"}})
    assert client.get("/variables/global").json()["variables"] == {"currency": "USD"}


def test_effective_precedence(client):
    client.put("/variables/global", json={"variables": {"currency": "USD", "terminal": "G"}})
    pid = client.post("/projects", json={"name": "P", "description": ""}).json()["id"]
    client.put(f"/variables/project/{pid}", json={"variables": {"terminal": "P"}})
    sid = client.post("/scenarios", json={
        "scenario": _scenario(variables={"amount": "100"}), "project_id": pid,
    }).json()["id"]

    eff = client.get(f"/scenarios/{sid}/variables").json()["effective"]
    # global terminal overridden by project; scenario adds amount
    assert eff == {"currency": "USD", "terminal": "P", "amount": "100"}


def test_set_level_overrides_project(client):
    client.put("/variables/global", json={"variables": {"k": "global"}})
    pid = client.post("/projects", json={"name": "P2", "description": ""}).json()["id"]
    client.put(f"/variables/project/{pid}", json={"variables": {"k": "project"}})
    sid = client.post("/scenarios", json={
        "scenario": _scenario("in-set"), "project_id": pid,
    }).json()["id"]
    setobj = client.post(f"/projects/{pid}/sets", json={
        "name": "S", "description": "", "scenario_ids": [sid],
    }).json()
    client.put(f"/variables/set/{setobj['id']}", json={"variables": {"k": "set"}})

    eff = client.get(f"/scenarios/{sid}/variables").json()["effective"]
    assert eff["k"] == "set"


def test_secret_variables_roundtrip_and_values(client):
    client.put("/variables/global", json={
        "variables": {"apikey": "SECRET123", "currency": "USD"},
        "secrets": ["apikey"],
    })
    g = client.get("/variables/global").json()
    assert g["secrets"] == ["apikey"]

    # resolve returns the secret *values* (for redaction), not which are public
    res = client.post("/variables/resolve", json={
        "project_id": None, "set_ids": [], "variables": {}, "secret_vars": [],
    }).json()
    assert "SECRET123" in res["secret_values"]
    assert "USD" not in res["secret_values"]


def test_scenario_secret_vars_in_effective_secret_values(client):
    scenario = _scenario(variables={"token": "T0PSECRET"})
    scenario["secret_vars"] = ["token"]
    sid = client.post("/scenarios", json={"scenario": scenario}).json()["id"]
    v = client.get(f"/scenarios/{sid}/variables").json()
    assert "T0PSECRET" in v["secret_values"]


def test_resolve_endpoint_for_preview(client):
    client.put("/variables/global", json={"variables": {"a": "1", "b": "2"}})
    res = client.post("/variables/resolve", json={
        "project_id": None, "set_ids": [], "variables": {"b": "override"},
    }).json()
    assert res["effective"] == {"a": "1", "b": "override"}
