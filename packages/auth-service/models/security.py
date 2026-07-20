"""Password hashing and JWT issuance/verification.

Passwords use PBKDF2-HMAC-SHA256 (stdlib only — no native build step, keeps the
image slim). Tokens are signed HS256 with ``AUTH_JWT_SECRET``: the same secret
the orchestrator and scenario-service use to *verify*, so issuing and checking
stay in lockstep with a single shared value. (RS256 + JWKS is the hardening
path — swap ``_sign``/``_verify`` and publish the public key; nothing else
changes.)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import jwt

_PBKDF2_ROUNDS = 240_000
_ALG = "HS256"

DEFAULT_TTL_SEC = int(os.environ.get("AUTH_TOKEN_TTL_SEC", "7200"))
ISSUER = os.environ.get("AUTH_JWT_ISSUER", "payprobe-auth")


def _secret() -> str:
    secret = os.environ.get("AUTH_JWT_SECRET")
    if not secret:
        # Fail closed: never sign with a guessable empty key in prod.
        if os.environ.get("PAYPROBE_ENV", "").lower() in {"dev", "development", "test", "local"}:
            return "dev-insecure-secret"
        raise RuntimeError("AUTH_JWT_SECRET is not set; refusing to issue tokens")
    return secret


# -- passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return ``pbkdf2_sha256$<rounds>$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# -- tokens ------------------------------------------------------------------

def issue_token(
    subject: str,
    roles: list[str],
    project_ids: list[str],
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> tuple[str, int]:
    """Sign a JWT. Returns ``(token, expires_in_seconds)``."""
    now = int(time.time())
    claims = {
        "sub": subject,
        "roles": roles,
        "project_ids": project_ids,
        "iss": ISSUER,
        "iat": now,
        "exp": now + ttl_sec,
    }
    return jwt.encode(claims, _secret(), algorithm=_ALG), ttl_sec


def decode_token(token: str) -> dict[str, Any]:
    """Verify signature + ``exp``; raises ``jwt.PyJWTError`` on failure."""
    return jwt.decode(
        token, _secret(), algorithms=[_ALG],
        options={"require": ["exp", "sub"]}, issuer=ISSUER,
    )
