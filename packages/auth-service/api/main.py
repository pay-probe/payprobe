"""PayProbe Auth Service — JWT issuance + user management.

The single source of identity for the platform. It issues short-lived JWTs that
the orchestrator and scenario-service verify (HS256, shared ``AUTH_JWT_SECRET``)
and embeds ``roles`` + ``project_ids`` so those services can enforce RBAC and
per-project isolation without calling back here.

Run locally (SQLite, zero config):
    uvicorn api.main:app --reload --port 8300

Environment:
    AUTH_DB             SQLite path (default: /data/auth.db)
    AUTH_JWT_SECRET     HS256 signing secret — MUST match the verifiers
    AUTH_TOKEN_TTL_SEC  access-token lifetime (default 7200)
    AUTH_JWT_ISSUER     iss claim (default: payprobe-auth)
    AUTH_ADMIN_USER     bootstrap admin username (default: admin)
    AUTH_ADMIN_PASSWORD bootstrap admin password (dev default: admin)
    CORS_ORIGINS        comma-separated allowed origins
    PAYPROBE_ENV        dev/test relaxes the bootstrap-password requirement
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from models import (
    UserStore, decode_token, hash_password, issue_token, verify_password,
)
from .metrics import (
    Counter as _Counter, Histogram as _Histogram,
    render as _render_metrics, CONTENT_TYPE as _METRICS_CT,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

import time as _time

_HTTP_REQUESTS = _Counter(
    "payprobe_http_requests_total", "HTTP requests by route and status.",
    ["method", "route", "code"],
)
_HTTP_LATENCY = _Histogram(
    "payprobe_http_request_duration_seconds", "HTTP request latency (s).", ["route"],
)


def _bootstrap_admin(store: UserStore) -> None:
    """Seed an admin on first start so the service is usable out of the box."""
    if store.count() > 0:
        return
    user = os.environ.get("AUTH_ADMIN_USER", "admin")
    pwd = os.environ.get("AUTH_ADMIN_PASSWORD")
    if not pwd:
        if os.environ.get("PAYPROBE_ENV", "").lower() in {"dev", "development", "test", "local"}:
            pwd = "admin"
        else:
            # No admin password in prod ⇒ start with no users rather than a
            # known-default account. Operators must seed explicitly.
            return
    store.create(user, hash_password(pwd), roles=["admin"], project_ids=["*"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = UserStore()
    _bootstrap_admin(store)
    app.state.store = store
    yield


app = FastAPI(title="PayProbe Auth Service", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:8080,http://localhost:4200"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- models ------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserDraft(BaseModel):
    username: str
    password: str
    roles: list[str] = ["operator"]
    project_ids: list[str] = []


class UserUpdate(BaseModel):
    """Editable fields of an existing user. Blank ``password`` keeps the current one."""

    roles: list[str] = ["operator"]
    project_ids: list[str] = []
    password: str = ""


class UserInfo(BaseModel):
    username: str
    roles: list[str]
    project_ids: list[str]


# -- dependencies ------------------------------------------------------------

def get_store(token=Depends(oauth2_scheme)) -> UserStore:  # placeholder for DI symmetry
    return app.state.store


def current_claims(token: Annotated[str | None, Depends(oauth2_scheme)]) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return decode_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            f"invalid token: {type(exc).__name__}",
                            headers={"WWW-Authenticate": "Bearer"}) from exc


def require_admin(claims: Annotated[dict, Depends(current_claims)]) -> dict:
    if "admin" not in (claims.get("roles") or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return claims


# -- endpoints ---------------------------------------------------------------

@app.middleware("http")
async def _metrics_mw(request, call_next):
    start = _time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "other"
        _HTTP_LATENCY.observe(_time.perf_counter() - start, route=route)
        _HTTP_REQUESTS.inc(method=request.method, route=route, code=str(status))


@app.get("/health")
async def health() -> dict:
    """Liveness."""
    return {"status": "ok", "users": app.state.store.count()}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(_render_metrics(), media_type=_METRICS_CT)


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: the user store answers."""
    try:
        app.state.store.count()
        return JSONResponse({"ready": True, "checks": {"store": "ok"}})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ready": False, "checks": {"store": f"error: {exc}"}}, status_code=503
        )


@app.post("/token", response_model=TokenResponse)
async def token(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> TokenResponse:
    """OAuth2 password grant → signed JWT."""
    user = app.state.store.get(username)
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    access, ttl = issue_token(user.username, user.roles, user.project_ids)
    return TokenResponse(access_token=access, expires_in=ttl)


@app.get("/me", response_model=UserInfo)
async def me(claims: Annotated[dict, Depends(current_claims)]) -> UserInfo:
    return UserInfo(username=claims["sub"], roles=claims.get("roles", []),
                    project_ids=claims.get("project_ids", []))


@app.get("/verify")
async def verify(claims: Annotated[dict, Depends(current_claims)]) -> dict:
    """Token introspection for services that prefer a callback over local verify."""
    return {"active": True, "claims": claims}


@app.get("/users", response_model=list[UserInfo], dependencies=[Depends(require_admin)])
async def list_users() -> list[UserInfo]:
    return [UserInfo(**u.public()) for u in app.state.store.list()]


@app.post("/users", response_model=UserInfo, status_code=201,
          dependencies=[Depends(require_admin)])
async def create_user(draft: UserDraft) -> UserInfo:
    try:
        user = app.state.store.create(
            draft.username, hash_password(draft.password),
            draft.roles, draft.project_ids,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return UserInfo(**user.public())


@app.put("/users/{username}", response_model=UserInfo,
         dependencies=[Depends(require_admin)])
async def update_user(username: str, draft: UserUpdate) -> UserInfo:
    store = app.state.store
    existing = store.get(username)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user '{username}'")
    # Lockout guard: never strip admin from the last remaining admin.
    if ("admin" in existing.roles and "admin" not in draft.roles
            and store.admin_count() <= 1):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "cannot remove the last admin")
    password_hash = hash_password(draft.password) if draft.password else None
    user = store.update(username, draft.roles, draft.project_ids, password_hash)
    return UserInfo(**user.public())


@app.delete("/users/{username}", status_code=204,
            dependencies=[Depends(require_admin)])
async def delete_user(username: str) -> None:
    store = app.state.store
    existing = store.get(username)
    # Lockout guard: never delete the last remaining admin.
    if existing and "admin" in existing.roles and store.admin_count() <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "cannot delete the last admin")
    if not store.delete(username):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user '{username}'")
