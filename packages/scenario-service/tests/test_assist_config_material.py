"""The service-gated assist-config material endpoint.

Settings → AI assistant is the ONE place the LLM key is managed; the
standalone payprobe-assistant (the egress boundary) pulls it from
/assist/config/material with a service credential. Same rule as key
material: user tokens get 403 and keep seeing only the masked view.
"""
import os
import time

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed(client) -> None:
    r = client.put("/assist/config", json={
        "provider": "anthropic", "enabled": True,
        "model": "claude-haiku-4-5-20251001", "api_key": "sk-test-material"})
    assert r.status_code == 200


def test_material_flow_by_credential_kind(client, monkeypatch):
    pyjwt = pytest.importorskip("jwt")
    monkeypatch.delenv("ASSIST_LLM_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _seed(client)

    # open dev/test mode: works locally
    r = client.get("/assist/config/material")
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"] == "sk-test-material" and body["enabled"]
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-haiku-4-5-20251001"

    # configured JWT auth: a USER token must NOT reveal the key…
    monkeypatch.setenv("AUTH_JWT_SECRET", "s3cret")
    now = int(time.time())
    user = pyjwt.encode({"sub": "alice", "roles": ["admin"],
                         "iat": now, "exp": now + 60}, "s3cret", algorithm="HS256")
    r = client.get("/assist/config/material",
                   headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 403

    # …while the masked view still works for that user (no api_key field)
    r = client.get("/assist/config",
                   headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 200
    pub = r.json()
    assert "api_key" not in pub and pub["key_set"]

    # a minted service JWT (svc claim — as the assistant sends) gets the key
    svc = pyjwt.encode({"sub": "payprobe-assistant", "svc": "payprobe-assistant",
                        "iat": now, "exp": now + 60}, "s3cret", algorithm="HS256")
    r = client.get("/assist/config/material",
                   headers={"Authorization": f"Bearer {svc}"})
    assert r.status_code == 200 and r.json()["api_key"] == "sk-test-material"

    # static bearer (the other service credential) also works
    monkeypatch.delenv("AUTH_JWT_SECRET")
    monkeypatch.setenv("API_TOKEN", "svc-token")
    r = client.get("/assist/config/material",
                   headers={"Authorization": "Bearer svc-token"})
    assert r.status_code == 200 and r.json()["api_key"] == "sk-test-material"


def test_material_reflects_toggle_off(client, monkeypatch):
    monkeypatch.delenv("ASSIST_LLM_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.put("/assist/config", json={
        "provider": "anthropic", "enabled": False, "api_key": "sk-off"})
    assert r.status_code == 200
    body = client.get("/assist/config/material").json()
    # saved key exists, but the provider toggle is off ⇒ not enabled
    assert not body["enabled"]
