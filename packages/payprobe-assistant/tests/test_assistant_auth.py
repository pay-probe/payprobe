"""Caller auth gate: the assistant fails closed like every other service."""
import pytest
from fastapi.testclient import TestClient

from assistant_service.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _prod(monkeypatch, **env):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    # other test modules set a fake LLM key process-wide; drop it so a request
    # that passes the gate takes the deterministic needs-config path (no
    # provider call escapes the test)
    for k in ("API_TOKEN", "AUTH_JWT_SECRET", "AUTH_JWT_PUBLIC_KEY",
              "ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_fails_closed_when_nothing_configured(client, monkeypatch):
    _prod(monkeypatch)
    resp = client.post("/agent/chat", json={"messages": []})
    assert resp.status_code == 503                      # refuse, don't serve open


def test_requires_bearer_with_static_token(client, monkeypatch):
    _prod(monkeypatch, API_TOKEN="s3cret")
    assert client.post("/agent/chat", json={"messages": []}).status_code == 401
    assert client.post("/agent/revert", json={"session_id": "x"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_valid_static_token_passes_gate(client, monkeypatch, backend):
    _prod(monkeypatch, API_TOKEN="s3cret")
    resp = client.post("/agent/chat", json={"messages": [
        {"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer s3cret"})
    # past the gate: no LLM configured → the friendly needs-config reply, not 401
    assert resp.status_code == 200


def test_valid_jwt_passes_gate(client, monkeypatch, backend):
    jwt = pytest.importorskip("jwt")
    import time
    _prod(monkeypatch, AUTH_JWT_SECRET="topsecret")
    token = jwt.encode({"sub": "david", "exp": int(time.time()) + 60},
                       "topsecret", algorithm="HS256")
    resp = client.post("/agent/chat", json={"messages": [
        {"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    # an expired token is rejected
    stale = jwt.encode({"sub": "david", "exp": int(time.time()) - 60},
                       "topsecret", algorithm="HS256")
    assert client.post("/agent/chat", json={"messages": []},
                       headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_health_stays_public(client, monkeypatch):
    _prod(monkeypatch, API_TOKEN="s3cret")
    assert client.get("/health").status_code == 200


def test_dev_env_stays_open(client, backend, monkeypatch):
    # conftest sets PAYPROBE_ENV=test → gate open for the rest of the suite
    for k in ("ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    resp = client.post("/agent/chat", json={"messages": [
        {"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
