"""Auth gate for the scenario-service.

Mirrors ``orchestrator/api/auth.py`` so both services accept the same
credentials and fail closed identically — previously this service only had an
optional static bearer (``API_TOKEN``, off by default), while the orchestrator
verified JWTs and refused to serve unauthenticated outside dev. That mismatch
meant the orchestrator could be locked down while this service stayed wide open.

Two credential kinds are accepted:

* **static bearer** — ``Authorization: Bearer <API_TOKEN>`` (service-to-service).
* **JWT** — set ``AUTH_JWT_SECRET`` (HS256) or ``AUTH_JWT_PUBLIC_KEY`` (RS256),
  the tokens ``auth-service`` issues; we verify signature + ``exp`` and stash the
  claims on ``request.state.auth`` for downstream RBAC / project scoping.

Fails closed: if nothing is configured and we are not in an explicit dev/test
environment, every request is rejected (503) rather than served open.

Environment:
    PAYPROBE_ENV          dev | development | test | local → auth optional;
                          anything else (or unset) → auth REQUIRED (fail closed)
    API_TOKEN             static bearer accepted by the gate
    AUTH_JWT_SECRET       HS256 shared secret for JWT verification
    AUTH_JWT_PUBLIC_KEY   RS256 public key (PEM) for JWT verification
    AUTH_JWT_AUDIENCE     optional expected ``aud`` claim
    AUTH_JWT_ISSUER       optional expected ``iss`` claim
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Header, HTTPException, Request, status

_DEV_ENVS = {"dev", "development", "test", "local"}

# Paths that must stay open: liveness/readiness probes, metrics scraping, and
# the API reference/schema.
PUBLIC_PATHS: set[str] = {
    "/health", "/ready", "/metrics", "/status",
    "/reference", "/openapi.json", "/docs", "/redoc",
}


def _is_dev() -> bool:
    return os.environ.get("PAYPROBE_ENV", "").lower() in _DEV_ENVS


def _auth_configured() -> bool:
    return bool(
        os.environ.get("API_TOKEN")
        or os.environ.get("AUTH_JWT_SECRET")
        or os.environ.get("AUTH_JWT_PUBLIC_KEY")
    )


def auth_enforced() -> bool:
    """True when requests must carry a valid credential."""
    return _auth_configured() or not _is_dev()


def _verify_jwt(token: str) -> dict[str, Any] | None:
    """Return claims if the token is a valid JWT for our config, else None
    (so the caller can fall back to static-bearer comparison)."""
    secret = os.environ.get("AUTH_JWT_SECRET")
    public_key = os.environ.get("AUTH_JWT_PUBLIC_KEY")
    if not (secret or public_key):
        return None
    try:
        import jwt  # PyJWT, imported lazily
    except ImportError as exc:  # pragma: no cover - config error
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "JWT auth configured but PyJWT is not installed",
        ) from exc

    key = public_key or secret
    alg = "RS256" if public_key else "HS256"
    kwargs: dict[str, Any] = {"algorithms": [alg], "options": {"require": ["exp"]}}
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        kwargs["audience"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        kwargs["issuer"] = iss
    try:
        return jwt.decode(token, key, **kwargs)
    except Exception as exc:  # noqa: BLE001 - any verify failure ⇒ 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            f"invalid token: {type(exc).__name__}") from exc


def _check(authorization: str | None) -> dict[str, Any] | None:
    """Core decision shared by the dependency (raises on failure)."""
    if not _auth_configured():
        if _is_dev():
            return {"sub": "dev", "dev": True}
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "auth is not configured; refusing to serve (set API_TOKEN or "
            "AUTH_JWT_SECRET, or PAYPROBE_ENV=dev to bypass)",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "missing or malformed Authorization header")
    token = authorization[len("Bearer "):].strip()

    claims = _verify_jwt(token)
    if claims is not None:
        return claims

    api_token = os.environ.get("API_TOKEN")
    if api_token and token == api_token:
        return {"sub": "service", "static": True}

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")


async def require_auth(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """FastAPI dependency. Public paths are allow-listed."""
    if request.url.path in PUBLIC_PATHS:
        return
    request.state.auth = _check(authorization)
