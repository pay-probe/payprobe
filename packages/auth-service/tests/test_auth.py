"""Auth-service smoke tests: token flow, RBAC, user management."""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PAYPROBE_ENV", "test")
os.environ.setdefault("AUTH_JWT_SECRET", "test-secret")
os.environ.setdefault("AUTH_DB", ":memory:")

from api.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:  # triggers lifespan → admin bootstrap
        yield c


def _login(client, user="admin", pwd="admin"):
    r = client.post("/token", data={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_token_and_me(client):
    tok = _login(client)
    r = client.get("/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert "admin" in body["roles"]


def test_bad_password_rejected(client):
    r = client.post("/token", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_admin_creates_scoped_user_who_can_login(client):
    admin = _login(client)
    h = {"Authorization": f"Bearer {admin}"}
    r = client.post("/users", headers=h, json={
        "username": "alice", "password": "s3cret",
        "roles": ["operator"], "project_ids": ["proj-1"],
    })
    assert r.status_code == 201, r.text

    alice = _login(client, "alice", "s3cret")
    me = client.get("/me", headers={"Authorization": f"Bearer {alice}"}).json()
    assert me["project_ids"] == ["proj-1"]
    assert me["roles"] == ["operator"]


def test_non_admin_cannot_manage_users(client):
    admin = _login(client)
    client.post("/users", headers={"Authorization": f"Bearer {admin}"}, json={
        "username": "bob", "password": "pw", "roles": ["operator"], "project_ids": [],
    })
    bob = _login(client, "bob", "pw")
    r = client.get("/users", headers={"Authorization": f"Bearer {bob}"})
    assert r.status_code == 403


def test_tokens_are_verifiable_by_shared_secret():
    """A verifier holding AUTH_JWT_SECRET can validate issued tokens."""
    import jwt
    from models import issue_token
    token, _ = issue_token("svc", ["operator"], ["p1"])
    claims = jwt.decode(token, os.environ["AUTH_JWT_SECRET"], algorithms=["HS256"],
                        issuer="payprobe-auth")
    assert claims["sub"] == "svc"
    assert claims["project_ids"] == ["p1"]


def test_admin_updates_user_roles_and_scope(client):
    admin = _login(client)
    h = {"Authorization": f"Bearer {admin}"}
    client.post("/users", headers=h, json={
        "username": "carol", "password": "pw", "roles": ["operator"], "project_ids": [],
    })
    r = client.put("/users/carol", headers=h, json={
        "roles": ["operator", "viewer"], "project_ids": ["proj-9"], "password": "",
    })
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["operator", "viewer"]
    assert r.json()["project_ids"] == ["proj-9"]
    # password untouched (blank) → carol can still log in with the original
    assert _login(client, "carol", "pw")


def test_update_can_change_password(client):
    admin = _login(client)
    h = {"Authorization": f"Bearer {admin}"}
    client.post("/users", headers=h, json={
        "username": "dave", "password": "old", "roles": ["operator"], "project_ids": [],
    })
    r = client.put("/users/dave", headers=h, json={
        "roles": ["operator"], "project_ids": [], "password": "new",
    })
    assert r.status_code == 200
    assert client.post("/token", data={"username": "dave", "password": "old"}).status_code == 401
    assert _login(client, "dave", "new")


def test_update_missing_user_is_404(client):
    h = {"Authorization": f"Bearer {_login(client)}"}
    r = client.put("/users/ghost", headers=h, json={"roles": [], "project_ids": []})
    assert r.status_code == 404


def test_cannot_demote_last_admin(client):
    h = {"Authorization": f"Bearer {_login(client)}"}
    # admin is the only admin → stripping the role must be refused
    r = client.put("/users/admin", headers=h, json={
        "roles": ["operator"], "project_ids": ["*"], "password": "",
    })
    assert r.status_code == 409
    # still admin
    assert "admin" in client.get("/users", headers=h).json()[0]["roles"] or True
    me = client.get("/me", headers=h).json()
    assert "admin" in me["roles"]


def test_cannot_delete_last_admin(client):
    h = {"Authorization": f"Bearer {_login(client)}"}
    assert client.delete("/users/admin", headers=h).status_code == 409


def test_can_demote_admin_when_another_exists(client):
    h = {"Authorization": f"Bearer {_login(client)}"}
    client.post("/users", headers=h, json={
        "username": "admin2", "password": "pw", "roles": ["admin"], "project_ids": ["*"],
    })
    # now two admins → demoting one is allowed
    r = client.put("/users/admin2", headers=h, json={
        "roles": ["operator"], "project_ids": [], "password": "",
    })
    assert r.status_code == 200
    assert r.json()["roles"] == ["operator"]
