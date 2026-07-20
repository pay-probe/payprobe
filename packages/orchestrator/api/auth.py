"""Auth gate for the orchestrator.

This is the single place that decides whether a request may run. It accepts
two credential kinds so we can roll forward without a flag day:

* **static bearer** — ``Authorization: Bearer <API_TOKEN>``. Same mechanism the
  scenario-service already uses; good enough for service-to-service today.
* **JWT** — once ``auth-service`` issues tokens, set ``AUTH_JWT_SECRET`` (HS256)
  or ``AUTH_JWT_PUBLIC_KEY`` (RS256) and we verify signature + ``exp`` and stash
  the claims on ``request.state.auth`` for downstream RBAC / project scoping.

Crucially this gate **fails closed**: if nothing is configured and we are not in
an explicit dev/test environment, every request is rejected. Misconfiguration in
prod becomes a loud 503, never an open door.

Environment:
    PAYPROBE_ENV          dev | development | test → auth optional; anything
                          else (or unset) → auth REQUIRED (fail closed)
    API_TOKEN             static bearer accepted by the gate
    AUTH_JWT_SECRET       HS256 shared secret for JWT verification
    AUTH_JWT_PUBLIC_KEY   RS256 public key (PEM) for JWT verification
    AUTH_JWT_AUDIENCE     optional expected ``aud`` claim
    AUTH_JWT_ISSUER       optional expected ``iss`` claim
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Header, HTTPException, WebSocket, status
from starlette.requests import HTTPConnection

_DEV_ENVS = {"dev", "development", "test", "local"}

# Paths that must stay open: liveness/readiness probes, metrics scraping, the
# status aggregator, and the API reference/schema.
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
    """True when requests must carry a valid credential.

    Auth is enforced whenever it's configured, or whenever we're not in an
    explicit dev/test environment (where the gate fails closed). When this is
    False, the API is effectively open — callers that run untrusted work (e.g.
    code nodes) should refuse unless another boundary is in place.
    """
    return _auth_configured() or not _is_dev()


def _verify_jwt(token: str) -> dict[str, Any] | None:
    """Return claims if the token is a valid JWT for our config, else None.

    Returns None (rather than raising) when JWT auth isn't configured or PyJWT
    isn't installed, so the caller can fall back to static-bearer comparison.
    """
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
    options = {"require": ["exp"]}
    kwargs: dict[str, Any] = {"algorithms": [alg], "options": options}
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
    """Core decision shared by the HTTP dependency and the WS guard.

    Returns the auth principal (claims dict for JWT, a sentinel for the static
    bearer) on success; raises HTTPException otherwise.
    """
    # Fail closed: outside dev, refuse to serve unless auth is configured.
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

    # Prefer JWT verification when configured; fall back to static bearer.
    claims = _verify_jwt(token)
    if claims is not None:
        return claims

    api_token = os.environ.get("API_TOKEN")
    if api_token and token == api_token:
        return {"sub": "service", "static": True}

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")


async def require_auth(
    conn: HTTPConnection, authorization: str | None = Header(default=None)
) -> None:
    """FastAPI dependency. Apply app-wide; public paths are allow-listed.

    Typed against ``HTTPConnection`` (the base of both ``Request`` and
    ``WebSocket``) so this dependency can be resolved for HTTP *and* WebSocket
    routes. Asking for a ``Request`` here crashes the WebSocket handshake during
    dependency injection — the connection fails before the route runs — so WS
    routes are skipped and guarded separately by ``authorize_websocket``.
    """
    if conn.scope["type"] == "websocket":
        return
    if conn.url.path in PUBLIC_PATHS:
        return
    conn.state.auth = _check(authorization)


async def authorize_websocket(websocket: WebSocket) -> bool:
    """Guard a WebSocket before ``accept()``.

    App-level dependencies don't run for WS routes, so call this explicitly.
    Accepts the token from the ``Authorization`` header or a ``?token=`` query
    param (browsers can't set WS headers). Closes the socket and returns False
    on failure.
    """
    headers = getattr(websocket, "headers", {}) or {}
    params = getattr(websocket, "query_params", {}) or {}
    auth = headers.get("authorization")
    if not auth and (qp := params.get("token")):
        auth = f"Bearer {qp}"
    try:
        _check(auth)
        return True
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False
