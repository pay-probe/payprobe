"""Service-to-service credential the orchestrator presents to scenario-service.

scenario-service fails closed, so outbound calls must carry a bearer or they get
a 401 ("scenario-service returned HTTP 401 for scenario ..."). _service_auth_header
mirrors the credential kinds the peer accepts."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


def _clear(monkeypatch):
    for k in ("SCENARIO_API_TOKEN", "API_TOKEN", "AUTH_JWT_SECRET",
              "AUTH_JWT_AUDIENCE", "AUTH_JWT_ISSUER"):
        monkeypatch.delenv(k, raising=False)


def test_no_credentials_sends_no_header(monkeypatch):
    _clear(monkeypatch)
    assert m._service_auth_header() == {}


def test_static_api_token(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("API_TOKEN", "shared-secret")
    assert m._service_auth_header() == {"Authorization": "Bearer shared-secret"}


def test_scenario_token_overrides_api_token(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("API_TOKEN", "shared-secret")
    monkeypatch.setenv("SCENARIO_API_TOKEN", "scenario-only")
    assert m._service_auth_header() == {"Authorization": "Bearer scenario-only"}


def test_minted_jwt_is_verifiable_with_the_shared_secret(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AUTH_JWT_SECRET", "jwt-secret")
    header = m._service_auth_header()
    token = header["Authorization"].removeprefix("Bearer ")

    import jwt
    claims = jwt.decode(token, "jwt-secret", algorithms=["HS256"],
                        options={"require": ["exp"]})
    assert claims["sub"] == "orchestrator"
