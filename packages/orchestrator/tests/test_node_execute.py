"""Tests for the single-node execute endpoint (editor 'Execute step')."""

import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def test_execute_code_node(client):
    resp = client.post("/nodes/execute", json={
        "node": {"id": "c", "kind": "code",
                 "config": {"language": "python",
                            "code": "return {'v': context.get('x', 0) + 1}"}},
        "context": {"x": 41},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "passed"
    assert body["response"] == {"v": 42}


def test_execute_code_node_error(client):
    resp = client.post("/nodes/execute", json={
        "node": {"id": "c", "kind": "code",
                 "config": {"language": "python", "code": "raise ValueError('x')"}},
        "context": {},
    })
    body = resp.json()
    assert body["status"] == "error"
    assert "ValueError" in body["error"]


def test_execute_action_node_uses_mock_env(client):
    """An action node runs against the default mock environment, no hardware."""
    resp = client.post("/nodes/execute", json={
        "node": {"id": "a", "kind": "action", "target": "http",
                 "action": "echo_test", "payload": {}},
        "context": {},
    })
    body = resp.json()
    assert body["status"] in ("passed", "failed")
    assert "response" in body


def test_list_environments(client):
    resp = client.get("/environments")
    assert resp.status_code == 200
    envs = resp.json()
    names = {e["name"] for e in envs}
    assert "mock" in names
    mock = next(e for e in envs if e["name"] == "mock")
    assert mock["label"] == "mock"


def test_run_by_environment_name(client):
    """A preview run can pick a bundled environment by name."""
    scenario = {
        "id": "preview", "name": "preview_case", "test_class": "e2e",
        "steps": [
            {"id": "echo", "target": "http", "action": "echo_test",
             "payload": {}, "assertions": []},
        ],
        "edges": [],
    }
    resp = client.post("/runs", json={
        "scenarios": [scenario], "environment_name": "mock", "label": "preview",
    })
    assert resp.status_code == 200
    assert resp.json()["run_id"]


def test_run_unknown_environment_name_400(client):
    resp = client.post("/runs", json={
        "scenarios": [{"id": "x", "name": "x", "steps": []}],
        "environment_name": "does-not-exist",
    })
    assert resp.status_code == 400


def test_validate_code_python_ok(client):
    r = client.post("/code/validate", json={
        "language": "python", "code": "x = 1\nreturn {'a': x}",
    })
    body = r.json()
    assert body["ok"] is True
    assert body["errors"] == []


def test_validate_code_python_error_has_line(client):
    r = client.post("/code/validate", json={
        "language": "python", "code": "x = \nreturn 1",
    })
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["line"] == 1
    assert "message" in body["errors"][0]


def test_validate_code_javascript_error(client):
    r = client.post("/code/validate", json={
        "language": "javascript", "code": "const a = ;",
    })
    body = r.json()
    assert body["ok"] is False
    assert body["errors"]


def test_validate_code_empty_is_ok(client):
    r = client.post("/code/validate", json={"language": "python", "code": "  "})
    assert r.json() == {"ok": True, "errors": []}


def test_inline_run_keeps_failure_routes_through_the_orchestrator(client):
    """ADR-0014: parent / on_failure / error edges survive the orchestrator's
    run build and reach the worker (a handled failure, a passed run)."""
    import time

    scenario = {
        "id": "routes", "name": "routes_case", "test_class": "e2e",
        "handled_failures": "pass",
        "steps": [
            {"id": "lp", "kind": "loop", "config": {"mode": "times", "count": 2}},
            {"id": "bad", "target": "http", "action": "echo_test", "payload": {},
             "parent": "lp", "on_failure": "next",
             "assertions": [{"field": "no_such_field", "operator": "present"}]},
            {"id": "after", "target": "http", "action": "echo_test",
             "payload": {}, "assertions": []},
        ],
        "edges": [
            {"source": "lp", "source_port": "loop", "target": "bad"},
            {"source": "lp", "source_port": "done", "target": "after"},
        ],
    }
    resp = client.post("/runs", json={"scenarios": [scenario], "environment_name": "mock"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["run_id"]
    detail = {}
    for _ in range(100):
        detail = client.get(f"/runs/{rid}").json()
        if (detail.get("summary") or {}).get("scenarios"):
            break
        time.sleep(0.1)
    sc = detail["summary"]["scenarios"][0]
    statuses = [s["status"] for s in sc["steps"] if s["step_id"] == "bad"]
    assert statuses == ["handled", "handled"]
    assert sc["status"] == "passed"
    assert sc["handled"] == {"bad:next": 2}
