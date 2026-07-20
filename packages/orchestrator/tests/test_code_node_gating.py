"""/nodes/execute refuses code nodes when the API is open and unsandboxed."""
import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


_CODE_NODE = {
    "node": {"id": "c", "kind": "code",
             "config": {"language": "python", "code": "return {'v': 1}"}},
    "context": {},
}


def test_refused_when_auth_off_no_sandbox_no_override(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "test")          # auth not enforced
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.delenv("PAYPROBE_ALLOW_UNAUTH_CODE", raising=False)
    monkeypatch.setattr(main, "auth_enforced", lambda: False)
    monkeypatch.setattr(
        "worker.engine.code_runner.network_isolation_active", lambda: False
    )

    r = client.post("/nodes/execute", json=_CODE_NODE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert "code execution is disabled" in body["error"]


def test_allowed_with_explicit_override(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "test")
    monkeypatch.setenv("PAYPROBE_ALLOW_UNAUTH_CODE", "1")
    monkeypatch.setattr(main, "auth_enforced", lambda: False)
    monkeypatch.setattr(
        "worker.engine.code_runner.network_isolation_active", lambda: False
    )

    r = client.post("/nodes/execute", json=_CODE_NODE)
    assert r.status_code == 200
    assert r.json()["status"] == "passed"


def test_allowed_when_auth_enforced(client, monkeypatch):
    """With auth enforced, code runs (the request already passed the gate)."""
    monkeypatch.delenv("PAYPROBE_ALLOW_UNAUTH_CODE", raising=False)
    monkeypatch.setattr(main, "auth_enforced", lambda: True)
    monkeypatch.setattr(
        "worker.engine.code_runner.network_isolation_active", lambda: False
    )

    r = client.post("/nodes/execute", json=_CODE_NODE)
    assert r.status_code == 200
    assert r.json()["status"] == "passed"
