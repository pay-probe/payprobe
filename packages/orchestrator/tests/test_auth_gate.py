"""The fail-closed auth gate (api/auth.py) wired into the orchestrator app."""

import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def test_health_is_public_even_when_gated(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("API_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200


def test_request_without_token_is_rejected_in_prod(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("API_TOKEN", "s3cret")
    r = client.post("/nodes/execute", json={"node": {"kind": "code", "config": {}}})
    assert r.status_code == 401


def test_unconfigured_prod_fails_closed(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    r = client.get("/runs")
    assert r.status_code == 503


def test_valid_static_bearer_passes(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("API_TOKEN", "s3cret")
    r = client.get("/runs", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_jwt_from_auth_service_is_accepted(client, monkeypatch):
    """A token signed with the shared secret (as auth-service does) is valid."""
    import jwt
    import time
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", "shared")
    now = int(time.time())
    tok = jwt.encode(
        {"sub": "alice", "roles": ["operator"], "project_ids": ["p1"],
         "iat": now, "exp": now + 60},
        "shared", algorithm="HS256",
    )
    r = client.get("/runs", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_dev_env_leaves_gate_open(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "dev")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    assert client.get("/runs").status_code == 200


def test_participant_and_topology_endpoints_require_auth(client, monkeypatch):
    """The participant-flow + topology lifecycle endpoints inherit the global
    fail-closed gate — unauthenticated requests are rejected in prod."""
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("API_TOKEN", "s3cret")
    assert client.get("/participants").status_code == 401
    assert client.post("/participants/start", json={"flow_id": "x"}).status_code == 401
    assert client.get("/topology-runs").status_code == 401
    assert client.post("/network-flows/net/start").status_code == 401


# -- WebSocket run stream through the real dependency layer --------------------
#
# Regression for the run stream that never delivered events: the app applies
# `dependencies=[Depends(require_auth)]` to *every* route, including the
# WebSocket stream. When that dependency asked for a `Request` (which doesn't
# exist in a WebSocket scope) the handshake crashed during dependency injection
# — before `authorize_websocket` ran — so every socket failed (browser code
# 1006) and the live panel hung on "Waiting for step results…". The existing
# stream tests call `stream_run(fake_ws, ...)` directly and so never exercised
# this layer; these go through TestClient's real ASGI routing.

def _seed_completed(run_id: str) -> None:
    """Append a terminal event so stream_run replays then closes cleanly."""
    import asyncio
    from worker.engine.events import RUN_COMPLETED
    asyncio.run(
        main.backbone.append(
            run_id, {"type": RUN_COMPLETED, "run_id": run_id, "data": {"status": "passed"}}
        )
    )


def test_ws_stream_handshake_survives_global_auth_dependency(client, monkeypatch):
    """Dev/open mode: the socket must connect and stream, not crash on require_auth."""
    monkeypatch.setenv("PAYPROBE_ENV", "dev")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    _seed_completed("ws-dev")
    with client.websocket_connect("/runs/ws-dev/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "run.completed"


def test_ws_stream_rejected_without_token_when_enforced(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", "shared")
    _seed_completed("ws-noauth")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/runs/ws-noauth/stream") as ws:
            ws.receive_json()


def test_ws_stream_accepts_valid_token_query_param(client, monkeypatch):
    import jwt
    import time
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", "shared")
    now = int(time.time())
    tok = jwt.encode({"sub": "alice", "exp": now + 60}, "shared", algorithm="HS256")
    _seed_completed("ws-auth")
    # browsers can't set WS headers, so the token rides as ?token= (see
    # authorize_websocket); the portal's RunMonitorService builds the URL this way.
    with client.websocket_connect(f"/runs/ws-auth/stream?token={tok}") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "run.completed"
