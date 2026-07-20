"""Import/export endpoint tests (v0.3 — scenario import/export)."""

import json
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app

from .test_scenario_service import create, draft, step  # reuse helpers


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_export_yaml_default(client):
    sid = create(client, name="export me", steps=[step("step_001")])["id"]
    res = client.get(f"/scenarios/{sid}/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/yaml")
    assert 'filename="export_me.yaml"' in res.headers["content-disposition"]
    doc = yaml.safe_load(res.text)
    assert doc["name"] == "export me"
    assert doc["steps"][0]["id"] == "step_001"
    assert "id" not in doc and "version" not in doc  # portable: no server fields


def test_export_json(client):
    sid = create(client, name="export me", steps=[step("step_001")])["id"]
    res = client.get(f"/scenarios/{sid}/export", params={"format": "json"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    doc = json.loads(res.text)
    assert doc["name"] == "export me"


def test_export_unknown_scenario(client):
    assert client.get("/scenarios/nope/export").status_code == 404


def test_import_json(client):
    body = json.dumps(draft(name="imported json", steps=[step("step_001")]))
    res = client.post("/scenarios/import", content=body)
    assert res.status_code == 201
    sc = res.json()
    assert sc["name"] == "imported json"
    assert sc["version"] == 1
    assert client.get(f"/scenarios/{sc['id']}").status_code == 200


def test_import_yaml(client):
    body = yaml.safe_dump(draft(name="imported yaml", steps=[step("step_001")]))
    res = client.post("/scenarios/import", content=body)
    assert res.status_code == 201
    assert res.json()["name"] == "imported yaml"


def test_roundtrip_json_and_yaml(client):
    steps = [
        step("step_001"),
        step(
            "step_002",
            target="db_probe_core",
            action="query_transaction",
            payload={"rrn": "${step_001.response.rrn}"},
            assertions=[{"field": "status", "operator": "eq", "expected": "APPROVED"}],
        ),
    ]
    sid = create(client, name="roundtrip", steps=steps)["id"]
    for fmt in ("json", "yaml"):
        exported = client.get(f"/scenarios/{sid}/export", params={"format": fmt}).text
        res = client.post("/scenarios/import", content=exported)
        assert res.status_code == 201, res.text
        clone = res.json()
        assert clone["id"] != sid  # fresh id assigned
        original = client.get(f"/scenarios/{sid}").json()
        for key in ("name", "description", "tags", "stop_on_failure", "timeout_ms", "steps"):
            assert clone[key] == original[key]


def test_import_strips_server_fields(client):
    doc = draft(name="with server fields", steps=[step("step_001")])
    doc |= {"id": "evil-id", "version": 99, "created_at": "x", "updated_at": "y"}
    res = client.post("/scenarios/import", content=json.dumps(doc))
    assert res.status_code == 201
    assert res.json()["id"] != "evil-id"
    assert res.json()["version"] == 1


def test_import_rejects_garbage(client):
    res = client.post("/scenarios/import", content="{not json: [nor yaml")
    assert res.status_code == 422


def test_import_rejects_non_object(client):
    assert client.post("/scenarios/import", content="[1, 2, 3]").status_code == 422
    assert client.post("/scenarios/import", content="").status_code == 422


def test_import_rejects_invalid_scenario(client):
    res = client.post("/scenarios/import", content=json.dumps({"description": "no name"}))
    assert res.status_code == 422


def test_import_runs_structural_validation(client):
    bad = draft(
        name="bad refs",
        steps=[step("step_001", payload={"rrn": "${step_999.response.rrn}"})],
    )
    res = client.post("/scenarios/import", content=json.dumps(bad))
    assert res.status_code == 422


def test_layout_roundtrip(client):
    doc = draft(name="with layout", steps=[step("step_001"), step("step_002")])
    doc["layout"] = {"step_001": {"x": 60, "y": 200}, "step_002": {"x": 340, "y": 200}}
    created = client.post("/scenarios", json={"scenario": doc}).json()
    assert created["layout"]["step_002"] == {"x": 340.0, "y": 200.0}
    exported = client.get(f"/scenarios/{created['id']}/export", params={"format": "yaml"}).text
    res = client.post("/scenarios/import", content=exported)
    assert res.status_code == 201
    assert res.json()["layout"] == created["layout"]
