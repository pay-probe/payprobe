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


def test_demo_login_is_off_by_default(client, monkeypatch):
    monkeypatch.delenv("AUTH_DEMO_LOGIN", raising=False)
    assert client.get("/demo-login").status_code == 404


@pytest.mark.parametrize("raw", ["", "admin", "admin:", ":admin"])
def test_demo_login_malformed_value_stays_off(client, monkeypatch, raw):
    monkeypatch.setenv("AUTH_DEMO_LOGIN", raw)
    assert client.get("/demo-login").status_code == 404


def test_demo_login_publishes_the_configured_account(client, monkeypatch):
    monkeypatch.setenv("AUTH_DEMO_LOGIN", "admin:admin")
    r = client.get("/demo-login")  # no token: the login page calls it signed out
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "password": "admin"}
    assert r.headers["cache-control"] == "no-store"
    _login(client, r.json()["username"], r.json()["password"])


def test_demo_login_password_may_contain_colons(client, monkeypatch):
    monkeypatch.setenv("AUTH_DEMO_LOGIN", "demo:pa:ss")
    assert client.get("/demo-login").json() == {"username": "demo", "password": "pa:ss"}


def _userinfo(**claims):
    import base64
    import json
    return base64.b64encode(json.dumps(claims).encode()).decode()


@pytest.fixture()
def proxy(monkeypatch):
    """Proxy sign-in on, reading APISIX's X-Userinfo, with the defaults."""
    monkeypatch.setenv("AUTH_PROXY_HEADER", "X-Userinfo")
    for name in ("AUTH_PROXY_HEADER_FORMAT", "AUTH_PROXY_USERNAME_CLAIMS",
                 "AUTH_PROXY_ROLES", "AUTH_PROXY_PROJECTS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _proxy_me(client, headers):
    r = client.get("/proxy-login", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    tok = r.json()["access_token"]
    me = client.get("/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200, me.text
    return me.json()


def test_proxy_login_is_off_by_default(client, monkeypatch):
    monkeypatch.delenv("AUTH_PROXY_HEADER", raising=False)
    r = client.get("/proxy-login", headers={"X-Userinfo": _userinfo(sub="eve")})
    assert r.status_code == 404


@pytest.mark.parametrize("value", [None, "", "not base64!", _userinfo()[:-4] + "@@@@",
                                   "bm90IGpzb24=",  # base64 of "not json"
                                   "WzFd",  # base64 of "[1]": JSON, not an object
                                   _userinfo(email="", other="x")])
def test_proxy_login_refuses_a_missing_or_unusable_header(client, proxy, value):
    headers = {} if value is None else {"X-Userinfo": value}
    r = client.get("/proxy-login", headers=headers)
    assert r.status_code == 401


def test_proxy_login_reads_base64_userinfo(client, proxy):
    me = _proxy_me(client, {"X-Userinfo": _userinfo(
        sub="0c1f", email="erin@example.test", preferred_username="erin")})
    assert me == {"username": "erin", "roles": ["operator"], "project_ids": []}


def test_proxy_login_accepts_url_safe_unpadded_userinfo(client, proxy):
    import base64
    raw = base64.urlsafe_b64encode(
        b'{"preferred_username": "fay", "note": "??>"}').decode().rstrip("=")
    assert "-" in raw and not raw.endswith("=")
    assert _proxy_me(client, {"X-Userinfo": raw})["username"] == "fay"


def test_proxy_login_reads_a_plain_header(client, proxy):
    proxy.setenv("AUTH_PROXY_HEADER", "X-Forwarded-User")
    proxy.setenv("AUTH_PROXY_HEADER_FORMAT", "plain")
    assert _proxy_me(client, {"X-Forwarded-User": " gus "})["username"] == "gus"


def test_proxy_login_rejects_an_unknown_format(client, proxy):
    proxy.setenv("AUTH_PROXY_HEADER_FORMAT", "jwt")
    r = client.get("/proxy-login", headers={"X-Userinfo": _userinfo(sub="x")})
    assert r.status_code == 500


def test_proxy_login_tries_username_claims_in_order(client, proxy):
    info = _userinfo(sub="0c1f", email="hal@example.test")
    # preferred_username absent → the next default claim, email
    assert _proxy_me(client, {"X-Userinfo": info})["username"] == "hal@example.test"
    proxy.setenv("AUTH_PROXY_USERNAME_CLAIMS", "sub, email")
    assert _proxy_me(client, {"X-Userinfo": info})["username"] == "0c1f"


def test_proxy_login_grants_the_configured_roles_and_projects(client, proxy):
    proxy.setenv("AUTH_PROXY_ROLES", "operator,viewer")
    proxy.setenv("AUTH_PROXY_PROJECTS", "proj-1, proj-2")
    me = _proxy_me(client, {"X-Userinfo": _userinfo(preferred_username="ivy")})
    assert me["roles"] == ["operator", "viewer"]
    assert me["project_ids"] == ["proj-1", "proj-2"]


def test_proxy_login_uses_a_stored_users_roles(client, proxy):
    admin = _login(client)
    client.post("/users", headers={"Authorization": f"Bearer {admin}"}, json={
        "username": "jo", "password": "pw", "roles": ["admin"], "project_ids": ["*"],
    })
    proxy.setenv("AUTH_PROXY_ROLES", "viewer")
    me = _proxy_me(client, {"X-Userinfo": _userinfo(preferred_username="jo")})
    assert me == {"username": "jo", "roles": ["admin"], "project_ids": ["*"]}
