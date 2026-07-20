import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

import api.main as main
from api.main import app


@pytest.fixture()
def client():
    """Fresh in-memory store per test (lifespan runs inside the context)."""
    with TestClient(app) as c:
        yield c


def draft(name="test scenario", steps=None):
    return {
        "name": name,
        "description": "",
        "tags": ["unit"],
        "stop_on_failure": True,
        "timeout_ms": 5000,
        "steps": steps if steps is not None else [],
    }


def step(sid, target="http", action="send_auth_request", payload=None, assertions=None):
    return {
        "id": sid,
        "target": target,
        "action": action,
        "payload": payload or {},
        "assertions": assertions or [],
    }


def create(client, **kwargs):
    res = client.post("/scenarios", json={"scenario": draft(**kwargs)})
    assert res.status_code == 201
    return res.json()


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_seeded_examples_present(client):
    names = {s["name"] for s in client.get("/scenarios").json()}
    assert "payshield_hsm_smoke" in names
    assert "TestPay · Refund" in names


def test_catalog(client):
    catalog = client.get("/catalog").json()
    targets = {t["target"] for t in catalog}
    assert {"http", "hsm", "db_probe_core"} <= targets
    http = next(t for t in catalog if t["target"] == "http")
    auth = next(a for a in http["actions"] if a["name"] == "send_auth_request")
    assert "rrn" in auth["response_fields"]


def test_crud_roundtrip(client):
    sid = create(client, steps=[step("step_001")])["id"]
    body = client.get(f"/scenarios/{sid}").json()
    assert body["version"] == 1
    assert body["steps"][0]["id"] == "step_001"

    updated = draft(steps=[step("step_001"), step("step_002")])
    res = client.put(f"/scenarios/{sid}", json={"scenario": updated, "comment": "add step"})
    assert res.status_code == 200
    assert res.json()["version"] == 2
    assert len(res.json()["steps"]) == 2


def test_soft_delete(client):
    sid = create(client)["id"]
    assert client.delete(f"/scenarios/{sid}").status_code == 204
    assert client.get(f"/scenarios/{sid}").status_code == 404
    assert client.delete(f"/scenarios/{sid}").status_code == 404
    assert sid not in {s["id"] for s in client.get("/scenarios").json()}


def test_version_history_and_restore(client):
    sid = create(client, steps=[step("step_001")])["id"]
    client.put(
        f"/scenarios/{sid}",
        json={"scenario": draft(steps=[step("step_001"), step("step_002")]), "comment": "v2"},
    )
    versions = client.get(f"/scenarios/{sid}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["step_count"] == 2

    v1 = client.get(f"/scenarios/{sid}/versions/1").json()
    assert len(v1["steps"]) == 1

    restored = client.post(f"/scenarios/{sid}/versions/1/restore").json()
    assert restored["version"] == 3
    assert len(restored["steps"]) == 1


def test_optimistic_locking_conflict(client):
    sid = create(client, steps=[step("step_001")])["id"]
    # Someone else saves v2…
    res = client.put(
        f"/scenarios/{sid}",
        json={"scenario": draft(steps=[step("step_001")]), "base_version": 1},
    )
    assert res.status_code == 200
    # …then a stale client tries to save on top of v1.
    res = client.put(
        f"/scenarios/{sid}",
        json={"scenario": draft(), "base_version": 1},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["current_version"] == 2
    # Saving without base_version (explicit overwrite) still works.
    res = client.put(f"/scenarios/{sid}", json={"scenario": draft()})
    assert res.status_code == 200
    assert res.json()["version"] == 3


def test_bearer_token_auth(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    assert client.get("/scenarios").status_code == 401
    assert client.get("/health").status_code == 200  # health stays open
    res = client.get("/scenarios", headers={"Authorization": "Bearer s3cret"})
    assert res.status_code == 200
    assert client.get(
        "/scenarios", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_body_size_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 100)
    big = draft(steps=[step("step_001", payload={"junk": "x" * 500})])
    res = client.post("/scenarios", json={"scenario": big})
    assert res.status_code == 413


def test_duplicate_step_ids_rejected(client):
    res = client.post(
        "/scenarios", json={"scenario": draft(steps=[step("step_001"), step("step_001")])}
    )
    assert res.status_code == 422
    assert any("duplicate" in i["message"] for i in res.json()["detail"])


def test_forward_variable_reference_rejected(client):
    steps = [
        step("step_001", payload={"rrn": "${step_002.response.rrn}"}),
        step("step_002"),
    ]
    res = client.post("/scenarios", json={"scenario": draft(steps=steps)})
    assert res.status_code == 422
    assert any("does not resolve" in i["message"] for i in res.json()["detail"])


def test_generator_namespace_references_accepted(client):
    # ${card.*}/${seq.*}/${now.*}/${pool.*} are runtime generators, not steps —
    # they must not be flagged as unresolved references.
    steps = [
        step("step_001", payload={
            "pan": "${card.pan}",
            "expiry": "${card.expiry}",
            "track2": "${card.track2}",
            "ksn": "${seq.ksn}",
            "rrn": "${now.rrn}",
            "term": "${pool.terminal}",
        }),
    ]
    res = client.post("/scenarios", json={"scenario": draft(steps=steps)})
    assert res.status_code == 201, res.text


def test_valid_backward_reference_accepted(client):
    steps = [
        step("step_001"),
        step(
            "step_002",
            target="db_probe_core",
            action="query_transaction",
            payload={"rrn": "${step_001.response.rrn}"},
        ),
    ]
    res = client.post("/scenarios", json={"scenario": draft(steps=steps)})
    assert res.status_code == 201


def test_validate_endpoint_warnings(client):
    body = draft(
        steps=[
            step(
                "step_001",
                assertions=[
                    {"field": "auth_code", "operator": "present", "expected": "oops"},
                    {"field": "response_code", "operator": "eq"},
                ],
            )
        ]
    )
    result = client.post("/validate", json=body).json()
    assert result["valid"] is False
    severities = {i["severity"] for i in result["issues"]}
    assert severities == {"error", "warning"}


def test_self_reference_rejected(client):
    steps = [step("step_001", payload={"x": "${step_001.response.rrn}"})]
    result = client.post("/validate", json=draft(steps=steps)).json()
    assert result["valid"] is False
    assert any("references itself" in i["message"] for i in result["issues"])
