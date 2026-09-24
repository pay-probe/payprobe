"""Agent principals: on-behalf-of tokens (ADR-0010 D10).

An agent never runs as a service. Each heartbeat mints a short-lived HS256 JWT
with the shared ``AUTH_JWT_SECRET`` (the same pattern the assistant uses for
its service JWT) whose ``sub``, ``roles`` and ``project_ids`` are the
*invoking user's*, plus an ``act`` claim naming the agent, its version and the
heartbeat. Downstream services verify it like any user token, so RBAC stays
per human and the audit trail records both. There is no ``svc`` claim: an
agent can never reach the service-gated material endpoints.

In dev mode with no secret configured (``PAYPROBE_ENV=dev`` and no
``AUTH_JWT_SECRET``) the token is the literal ``dev`` marker and downstream
services run open, exactly as they do for a human in that mode.
"""

from __future__ import annotations

import os
import time
from typing import Any


def act_claim(agent: str, version: int, heartbeat_id: str) -> dict:
    return {"agent": agent, "version": version, "heartbeat": heartbeat_id}


def mint_obo(user: dict[str, Any], agent: str, version: int, heartbeat_id: str, ttl_s: int) -> str:
    """Return a bearer for downstream calls made on behalf of ``user``."""
    secret = os.environ.get("AUTH_JWT_SECRET")
    if not secret:
        # No JWT secret: the platform is either open (dev) or on a static
        # bearer. Never hand the static service bearer to an agent: it would
        # unlock service-gated reads. Downstream calls then run unauthenticated
        # (which only dev mode accepts).
        return "dev"
    import jwt

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(user.get("sub") or "agent-hub"),
        "roles": list(user.get("roles") or []),
        "project_ids": list(user.get("project_ids") or []),
        "act": act_claim(agent, version, heartbeat_id),
        "iat": now,
        "exp": now + max(60, int(ttl_s)),
    }
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        claims["iss"] = iss
    return jwt.encode(claims, secret, algorithm="HS256")
