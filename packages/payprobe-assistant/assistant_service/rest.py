"""HTTP transport for the assistant.

Two kinds of calls live here:

* :func:`request` — authenticated calls to the PayProbe services
  (scenario-service / orchestrator). Carries a service credential: an explicit
  static bearer (``ASSIST_API_TOKEN`` / ``API_TOKEN``) if set, otherwise a
  short-lived JWT minted from the shared ``AUTH_JWT_SECRET`` (the same scheme the
  MCP server uses). Stdlib ``urllib`` — no extra runtime dependency.
* :func:`post_json` — plain JSON POST used to call LLM providers (their own auth
  headers are passed in by the caller).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

SCENARIO_API = os.environ.get("SCENARIO_API_URL", "http://localhost:8000").rstrip("/")
RUN_API = os.environ.get("RUN_API_URL", "http://localhost:8001").rstrip("/")
# Advisory ML insight service (ADR-0005). Optional deployment.
INSIGHT_API = os.environ.get(
    "INSIGHT_API_URL", "http://localhost:8500").rstrip("/")

_jwt_cache: dict[str, Any] = {"token": None, "exp": 0}


def _service_jwt(secret: str) -> str:
    """Mint (and cache) a short-lived HS256 JWT the upstream services accept."""
    now = int(time.time())
    cached = _jwt_cache.get("token")
    if cached and _jwt_cache.get("exp", 0) - 60 > now:
        return cached
    try:
        import jwt  # PyJWT, imported lazily
    except ImportError as exc:  # pragma: no cover - config error
        raise RuntimeError(
            "AUTH_JWT_SECRET is set but PyJWT is not installed — "
            "install with: pip install pyjwt") from exc

    ttl = int(os.environ.get("ASSIST_JWT_TTL", "3600"))
    sub = os.environ.get("ASSIST_JWT_SUB", "payprobe-assistant")
    claims: dict[str, Any] = {
        # "svc" marks this as a service credential (same convention as the
        # orchestrator) — needed for service-gated reads like
        # /assist/config/material and /test-data/keys/{name}/material.
        "sub": sub, "svc": sub,
        "iat": now, "exp": now + ttl,
    }
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        claims["iss"] = iss
    token = jwt.encode(claims, secret, algorithm="HS256")
    _jwt_cache.update(token=token, exp=now + ttl)
    return token


def _auth_header() -> dict[str, str]:
    """Authorization header for upstream calls (static bearer wins, else JWT)."""
    token = os.environ.get("ASSIST_API_TOKEN") or os.environ.get("API_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    secret = os.environ.get("AUTH_JWT_SECRET")
    if secret:
        return {"Authorization": f"Bearer {_service_jwt(secret)}"}
    return {}


def request(method: str, url: str, body: dict | None = None,
            raw: bool = False, timeout: float = 30) -> Any:
    """Authenticated request to a PayProbe service. Raises RuntimeError with the
    upstream status + detail on error (callers map that to tool errors)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **_auth_header()}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            if raw:
                return text
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}")


def post_json(url: str, headers: dict, body: dict) -> dict:
    """Plain JSON POST (for LLM providers). Returns the parsed response."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection error: {exc.reason}")


def post_stream(url: str, headers: dict, body: dict, timeout: float = 120):
    """Streaming JSON POST (for LLM provider SSE). Yields decoded lines.

    Raises RuntimeError on connection/HTTP errors *before* the first line, so
    callers can fall back to the non-streaming :func:`post_json` path."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream", **headers})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection error: {exc.reason}")

    def lines():
        with resp:
            for raw in resp:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")

    return lines()


def s(path: str) -> str:
    """scenario-service URL for ``path``."""
    return f"{SCENARIO_API}{path}"


def r(path: str) -> str:
    """orchestrator (run API) URL for ``path``."""
    return f"{RUN_API}{path}"


def i(path: str) -> str:
    """insight-service URL for ``path`` (ADR-0005, optional deployment)."""
    return f"{INSIGHT_API}{path}"
