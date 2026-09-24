"""Auth gate hardening from the ADR-0010 security review (2026-09-24):
on-behalf-of tokens are refused at agent-hub's own door, service credentials
cannot decide approvals / revert / pause, a static bearer still works when a
JWT secret is configured, and the compose placeholder secret is refused
outside dev.
"""

import time

import jwt
from agent_hub.auth import INSECURE_DEFAULT_SECRET
from agent_hub.principal import mint_obo
from hub_testkit import agent_spec


def _prod(monkeypatch, **env):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    for k in ("API_TOKEN", "AUTH_JWT_SECRET", "AUTH_JWT_PUBLIC_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _bearer(claims, secret="k"):
    now = int(time.time())
    return {
        "Authorization": "Bearer "
        + jwt.encode({"iat": now, "exp": now + 300, **claims}, secret, "HS256")
    }


def test_on_behalf_of_token_is_refused_at_the_door(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k")
    caller = {"sub": "ann", "roles": ["admin"], "project_ids": ["*"]}
    obo = mint_obo(caller, "observer", 1, "hb-1", 120)
    assert jwt.decode(obo, "k", algorithms=["HS256"])["act"]["agent"] == "observer"
    r = client.get("/agents", headers={"Authorization": "Bearer " + obo})
    assert r.status_code == 401 and "on-behalf-of" in r.json()["detail"]
    # the same user's own token is fine
    assert (
        client.get("/agents", headers=_bearer({"sub": "ann", "roles": ["admin"]})).status_code
        == 200
    )


def test_service_credentials_cannot_perform_a_humans_act(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k", API_TOKEN="s3cret")
    svc = _bearer({"sub": "mcp-server", "svc": "mcp-server"})
    static = {"Authorization": "Bearer s3cret"}
    adm = _bearer({"sub": "ann", "roles": ["admin"]})
    for hdr in (svc, static):
        assert client.put("/pause", json={"paused": True}, headers=hdr).status_code == 403
        assert client.post(
            "/approvals/nope/decide", json={"decision": "approved"}, headers=hdr
        ).status_code in (403, 404)
        assert client.post("/heartbeats/nope/revert", headers=hdr).status_code in (403, 404)
    # decide on a real pending approval: 403 for a service, 200 for the human
    r = client.post("/agents", json={"name": "a1", "spec": agent_spec()}, headers=adm)
    assert r.status_code == 201
    client.post("/agents/a1/versions/1/publish", headers=adm)
    wf = {
        "nodes": [{"id": "gate", "type": "approval", "roles": ["admin"]}],
        "edges": [{"from": "gate", "to": "end"}],
    }
    client.post("/workflows", json={"name": "w1", "spec": wf}, headers=adm)
    client.post("/workflows/w1/versions/1/publish", headers=adm)
    run = client.post(
        "/workflows/w1/run", json={"inputs": {}}, headers=svc
    ).json()  # a service may start
    ap = client.get("/approvals", headers=svc).json()[0]
    assert ap["run_id"] == run["id"]
    assert (
        client.post(
            f"/approvals/{ap['id']}/decide", json={"decision": "approved"}, headers=svc
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/approvals/{ap['id']}/decide", json={"decision": "approved"}, headers=static
        ).status_code
        == 403
    )
    r = client.post(f"/approvals/{ap['id']}/decide", json={"decision": "approved"}, headers=adm)
    assert r.status_code == 200 and r.json()["decided_by"] == "ann"
    # services keep their service jobs: events and pause reads
    assert (
        client.post(
            "/events", json={"event": "run.failed", "subject": {"run_id": "r"}}, headers=svc
        ).status_code
        == 202
    )
    assert client.get("/pause", headers=static).status_code == 200


def test_static_bearer_works_beside_a_jwt_secret(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k", API_TOKEN="s3cret")
    assert client.get("/agents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    bad_jwt = _bearer({"sub": "x", "roles": ["admin"]}, secret="other")
    assert client.get("/agents", headers=bad_jwt).status_code == 401


def test_placeholder_secret_is_refused_outside_dev(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET=INSECURE_DEFAULT_SECRET)
    r = client.get(
        "/agents", headers=_bearer({"sub": "x", "roles": ["admin"]}, secret=INSECURE_DEFAULT_SECRET)
    )
    assert r.status_code == 503 and "placeholder" in r.json()["detail"]
    assert client.get("/health").status_code == 200
    monkeypatch.setenv("PAYPROBE_ENV", "dev")
    assert (
        client.get(
            "/agents",
            headers=_bearer({"sub": "x", "roles": ["admin"]}, secret=INSECURE_DEFAULT_SECRET),
        ).status_code
        == 200
    )


def test_webhook_declared_oversize_is_refused_before_the_body_is_read(client, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_WEBHOOK_SECRET", "w")
    r = client.post(
        "/webhooks/events/run.failed",
        content=b"{}",
        headers={"Content-Length": "70000", "X-PayProbe-Signature": "t=1,v1=00"},
    )
    assert r.status_code == 413
