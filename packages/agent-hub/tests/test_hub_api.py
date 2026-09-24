"""agent-hub HTTP surface: registry CRUD, versioning, RBAC, pause."""

import time

import jwt
from agent_hub.main import app
from agent_hub.seed import SEEDS
from fastapi.testclient import TestClient
from hub_testkit import agent_spec

# -- seeds -----------------------------------------------------------------------------


def test_seeds_are_published_builtins(client):
    names = {a["name"] for a in client.get("/agents").json()}
    assert names == set(SEEDS)
    for name in SEEDS:
        d = client.get(f"/agents/{name}").json()
        assert d["builtin"] is True and d["active_version"] == 1
        assert d["spec"]["tools"], name
    obs = client.get("/agents/observer").json()["spec"]
    assert obs["mode"] == "advisor"
    assert {t["kind"] for t in obs["triggers"]} == {"manual", "schedule", "event"}


def test_failure_triage_seed_is_read_only_and_wakes_on_run_failed(client):
    spec = client.get("/agents/failure-triage").json()["spec"]
    tiers = {t["name"]: t["tier"] for t in client.get("/catalog").json()["tools"]}
    assert spec["mode"] == "advisor"
    assert {tiers[t] for t in spec["tools"]} == {"read"}
    assert {"get_run_insights", "list_runs", "get_scenario", "get_network"} <= set(spec["tools"])
    assert {t.get("event") for t in spec["triggers"] if t["kind"] == "event"} == {"run.failed"}


def test_seed_can_be_disabled(monkeypatch):
    import asyncio

    from hub_testkit import truncate

    monkeypatch.setenv("AGENT_HUB_SEED", "0")
    asyncio.run(truncate())
    with TestClient(app) as c:
        assert c.get("/agents").json() == []
        assert c.app.state.seeded == []


def test_builtin_retire_is_a_guardrail(client):
    r = client.post("/agents/config/retire")
    assert r.status_code == 400
    assert r.json()["detail"]["guardrail"] is True


# -- catalog + health ----------------------------------------------------------------------


def test_health_and_catalog(client):
    h = client.get("/health").json()
    assert h["service"] == "agent-hub" and h["paused"] is False
    assert h["schema_version"] >= 1
    cat = client.get("/catalog").json()
    assert "list_runs" in {t["name"] for t in cat["tools"]}
    assert cat["modes"] == ["advisor", "plan", "full"]


# -- agent lifecycle over HTTP ------------------------------------------------------------------


def test_agent_create_edit_publish_supersede(client):
    r = client.post("/agents", json={"name": "triage", "owner": "qa", "spec": agent_spec()})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft" and r.json()["owner"] == "qa"

    # draft is editable; validation dry-run reports ok
    r = client.put("/agents/triage/versions/1", json={"spec": agent_spec(role="edited")})
    assert r.status_code == 200 and r.json()["spec"]["role"] == "edited"
    assert client.post("/agents/triage/versions/1/validate").json()["valid"] is True

    r = client.post("/agents/triage/versions/1/publish")
    assert r.status_code == 200 and r.json()["status"] == "active"
    # published = immutable
    r = client.put("/agents/triage/versions/1", json={"spec": agent_spec()})
    assert r.status_code == 409
    assert client.post("/agents/triage/versions/1/publish").status_code == 409

    r = client.post("/agents/triage/versions", json={"spec": agent_spec(role="v2")})
    assert r.status_code == 201 and r.json()["version"] == 2
    client.post("/agents/triage/versions/2/publish")
    d = client.get("/agents/triage").json()
    assert d["active_version"] == 2
    assert [v["status"] for v in d["versions"]] == ["superseded", "active"]
    assert d["versions"][1]["spec_sha256"] and "spec" not in d["versions"][1]

    assert client.post("/agents/triage/retire").json()["status"] == "retired"
    assert client.get("/agents?status=retired").json()[0]["name"] == "triage"


def test_publish_refuses_invalid_spec_with_problems(client):
    client.post("/agents", json={"name": "bad", "spec": agent_spec(tools=["nope"])})
    r = client.post("/agents/bad/versions/1/publish")
    assert r.status_code == 422
    assert "unknown tool 'nope'" in r.json()["detail"]["problems"][0]
    assert client.get("/agents/bad").json()["active_version"] is None


def test_shape_errors_are_422_with_paths(client):
    r = client.post("/agents", json={"name": "bad", "spec": {"role": "x"}})
    assert r.status_code == 422
    assert any(p.startswith("instructions") for p in r.json()["detail"]["problems"])
    r = client.post("/agents", json={"name": "Bad Name", "spec": agent_spec()})
    assert r.status_code == 422


def test_conflicts_and_not_found(client):
    assert client.get("/agents/nope").status_code == 404
    assert client.get("/agents/config/versions/9").status_code == 404
    r = client.post("/agents", json={"name": "config", "spec": agent_spec()})
    assert r.status_code == 409


# -- workflows ----------------------------------------------------------------------------------


def _wf_body(name, nodes, edges):
    return {"name": name, "spec": {"nodes": nodes, "edges": edges}}


def test_workflow_publish_validates_against_live_registry(client):
    body = _wf_body(
        "triage-flow",
        [
            {"id": "collect", "type": "tool", "tool": "list_runs"},
            {"id": "diag", "type": "agent_task", "agent": "observer"},
            {"id": "rev", "type": "agent_task", "agent": "reviewer", "reviews": "diag"},
            {"id": "gate", "type": "approval", "roles": ["admin"], "timeout_s": 3600},
        ],
        [
            {"from": "collect", "to": "diag"},
            {"from": "diag", "to": "rev"},
            {"from": "rev", "to": "gate"},
            {"from": "gate", "to": "end"},
        ],
    )
    assert client.post("/workflows", json=body).status_code == 201
    r = client.post("/workflows/triage-flow/versions/1/publish")
    assert r.status_code == 200, r.text
    assert client.get("/workflows/triage-flow").json()["active_version"] == 1


def test_workflow_full_mode_needs_approval_and_registered_mode(client):
    # config is seeded in plan mode: a full-mode node is an escalation AND lacks a gate
    body = _wf_body(
        "apply-flow",
        [
            {
                "id": "apply",
                "type": "agent_task",
                "agent": "config",
                "mode": "full",
                "environment": "uat",
            }
        ],
        [],
    )
    client.post("/workflows", json=body)
    r = client.post("/workflows/apply-flow/versions/1/publish")
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"]
    assert any("escalates" in p for p in problems)
    assert any("ADR-0010 D3" in p for p in problems)


def test_workflow_pin_survives_agent_republish(client):
    body = _wf_body("pinned", [{"id": "a", "type": "agent_task", "agent": "observer@1"}], [])
    client.post("/workflows", json=body)
    assert client.post("/workflows/pinned/versions/1/publish").status_code == 200
    # publish observer v2; the pinned workflow still validates against v1
    spec = client.get("/agents/observer").json()["spec"]
    client.post("/agents/observer/versions", json={"spec": spec})
    assert client.post("/agents/observer/versions/2/publish").status_code == 200
    assert client.post("/workflows/pinned/versions/1/validate").json()["valid"] is True


# -- pause ------------------------------------------------------------------------------------


def test_pause_switch(client):
    assert client.get("/pause").json()["paused"] is False
    r = client.put("/pause", json={"paused": True})
    assert r.status_code == 200 and r.json()["paused"] is True
    assert client.get("/health").json()["paused"] is True
    client.put("/pause", json={"paused": False})
    assert client.get("/pause").json()["paused"] is False


# -- auth + RBAC --------------------------------------------------------------------------------


def _prod(monkeypatch, **env):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    for k in ("API_TOKEN", "AUTH_JWT_SECRET", "AUTH_JWT_PUBLIC_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _token(secret, sub, roles):
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "roles": roles, "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def test_fails_closed_when_nothing_configured(client, monkeypatch):
    _prod(monkeypatch)
    assert client.get("/agents").status_code == 503
    assert client.get("/health").status_code == 200  # liveness stays open


def test_static_bearer_gate(client, monkeypatch):
    _prod(monkeypatch, API_TOKEN="s3cret")
    assert client.get("/agents").status_code == 401
    assert client.get("/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/agents", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    # static (service) bearer may mutate
    r = client.post(
        "/agents",
        json={"name": "svc-made", "spec": agent_spec()},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 201


def test_jwt_roles_gate_mutations(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k")
    op = {"Authorization": "Bearer " + _token("k", "olga", ["operator"])}
    adm = {"Authorization": "Bearer " + _token("k", "ann", ["admin"])}

    assert client.get("/agents", headers=op).status_code == 200  # reads: anyone
    assert (
        client.post("/agents", json={"name": "x1", "spec": agent_spec()}, headers=op).status_code
        == 403
    )
    assert client.put("/pause", json={"paused": True}, headers=op).status_code == 403
    assert (
        client.post("/agents/config/versions", json={"spec": agent_spec()}, headers=op).status_code
        == 403
    )

    r = client.post("/agents", json={"name": "x1", "spec": agent_spec()}, headers=adm)
    assert r.status_code == 201 and r.json()["created_by"] == "ann"
    assert client.put("/pause", json={"paused": True}, headers=adm).json()["by"] == "ann"


def test_definition_rbac_edit_grants_operator(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k")
    op = {"Authorization": "Bearer " + _token("k", "olga", ["operator"])}
    adm = {"Authorization": "Bearer " + _token("k", "ann", ["admin"])}
    spec = agent_spec(rbac={"invoke": ["operator"], "edit": ["operator"]})
    client.post("/agents", json={"name": "shared", "spec": spec}, headers=adm)
    # rbac.edit applies once a version is *active* (it is read from the active spec)
    assert (
        client.post("/agents/shared/versions", json={"spec": spec}, headers=op).status_code == 403
    )
    client.post("/agents/shared/versions/1/publish", headers=adm)
    r = client.post("/agents/shared/versions", json={"spec": spec}, headers=op)
    assert r.status_code == 201
    assert client.post("/agents/shared/versions/2/publish", headers=op).status_code == 200


def test_expired_jwt_is_401(client, monkeypatch):
    _prod(monkeypatch, AUTH_JWT_SECRET="k")
    old = jwt.encode(
        {"sub": "x", "roles": ["admin"], "exp": int(time.time()) - 10}, "k", algorithm="HS256"
    )
    assert client.get("/agents", headers={"Authorization": "Bearer " + old}).status_code == 401
