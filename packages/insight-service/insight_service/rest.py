"""Read-only HTTP access to the orchestrator's run history.

Same transport pattern as the assistant's ``rest.py``: stdlib ``urllib``, a
static service bearer (``INSIGHT_API_TOKEN`` / ``API_TOKEN``) if set, otherwise
a short-lived HS256 JWT minted from the shared ``AUTH_JWT_SECRET``. The service
never writes upstream — it only ever GETs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

_jwt_cache: dict[str, Any] = {"token": None, "exp": 0}


def _service_jwt(secret: str) -> str:
    now = int(time.time())
    cached = _jwt_cache.get("token")
    if cached and _jwt_cache.get("exp", 0) - 60 > now:
        return cached
    try:
        import jwt  # PyJWT, lazily
    except ImportError as exc:  # pragma: no cover - config error
        raise RuntimeError(
            "AUTH_JWT_SECRET is set but PyJWT is not installed") from exc
    ttl = int(os.environ.get("INSIGHT_JWT_TTL", "3600"))
    claims: dict[str, Any] = {"sub": "insight-service", "iat": now, "exp": now + ttl}
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        claims["iss"] = iss
    token = jwt.encode(claims, secret, algorithm="HS256")
    _jwt_cache.update(token=token, exp=now + ttl)
    return token


def _auth_header() -> dict[str, str]:
    token = os.environ.get("INSIGHT_API_TOKEN") or os.environ.get("API_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    secret = os.environ.get("AUTH_JWT_SECRET")
    if secret:
        return {"Authorization": f"Bearer {_service_jwt(secret)}"}
    return {}


class OrchestratorReader:
    """GET-only client for the orchestrator. Injectable in tests: pass a
    ``fetch`` callable taking a path and returning parsed JSON."""

    def __init__(self, base_url: str | None = None, fetch=None) -> None:
        self.base = (base_url
                     or os.environ.get("RUN_API_URL", "http://localhost:8100")
                     ).rstrip("/")
        self._fetch = fetch or self._http_get

    def _http_get(self, path: str) -> Any:
        req = urllib.request.Request(self.base + path, headers=_auth_header())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"orchestrator GET {path} -> {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"orchestrator unreachable: {exc.reason}") from exc

    def list_runs(self) -> list[dict]:
        return self._fetch("/runs") or []

    def get_run(self, run_id: str) -> dict | None:
        return self._fetch(f"/runs/{run_id}")

    def list_load_runs(self) -> list[dict]:
        return self._fetch("/load-runs") or []

    def get_load_run(self, run_id: str) -> dict | None:
        return self._fetch(f"/load-runs/{run_id}")
