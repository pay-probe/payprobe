"""HTTP transport to the PayProbe services for agent-hub.

Two credentials, deliberately distinct:

* :func:`request` — the *service* credential (static ``API_TOKEN`` or a minted
  ``svc`` JWT), used only for service-gated platform reads such as the LLM
  key material. Never handed to an agent.
* :func:`request_as` — a per-heartbeat **on-behalf-of** token (see
  :mod:`agent_hub.principal`): the agent acts as the invoking user, so every
  downstream service applies that user's RBAC and logs the ``act`` claim.

Base URLs: ``SCENARIO_API_URL`` (:8000), ``RUN_API_URL`` (:8100),
``INSIGHT_API_URL`` (:8500).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

SCENARIO_API = os.environ.get("SCENARIO_API_URL", "http://localhost:8000").rstrip("/")
RUN_API = os.environ.get("RUN_API_URL", "http://localhost:8100").rstrip("/")
INSIGHT_API = os.environ.get("INSIGHT_API_URL", "http://localhost:8500").rstrip("/")

_jwt_cache: dict[str, Any] = {}


def s(path: str) -> str:
    return f"{SCENARIO_API}{path}"


def _service_jwt(secret: str) -> str:
    now = int(time.time())
    cached = _jwt_cache.get("token")
    if cached and _jwt_cache.get("exp", 0) - 60 > now:
        return cached
    import jwt

    ttl = int(os.environ.get("AGENT_HUB_JWT_TTL", "3600"))
    claims: dict[str, Any] = {"sub": "agent-hub", "svc": "agent-hub", "iat": now, "exp": now + ttl}
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        claims["iss"] = iss
    token = jwt.encode(claims, secret, algorithm="HS256")
    _jwt_cache.update(token=token, exp=now + ttl)
    return token


def _service_header() -> dict[str, str]:
    token = os.environ.get("API_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    secret = os.environ.get("AUTH_JWT_SECRET")
    if secret:
        return {"Authorization": f"Bearer {_service_jwt(secret)}"}
    return {}


def _do(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTP 0 {url}: connection error: {exc.reason}") from exc


def request(
    method: str, url: str, body: dict | None = None, raw: bool = False, timeout: float = 30
) -> Any:
    """Service-credential request (platform reads only)."""
    return _do(method, url, body, _service_header(), timeout)


def request_as(token: str) -> Callable[..., Any]:
    """A ``request`` function bound to one on-behalf-of bearer."""

    def _req(
        method: str, url: str, body: dict | None = None, raw: bool = False, timeout: float = 30
    ) -> Any:
        return _do(method, url, body, {"Authorization": f"Bearer {token}"}, timeout)

    return _req
