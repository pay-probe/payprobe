"""PayProbe agent-hub — FastAPI app (ADR-0010, phase 1: the registry).

Endpoints (all gated by the platform bearer except ``/health``):

* ``GET  /health``                                 liveness
* ``GET  /catalog``                                toolkit tools + tiers, modes, node types
* ``GET  /pause`` / ``PUT /pause``                 global "agents paused" switch
* ``GET  /agents``  ``POST /agents``               list / create (v1 as draft)
* ``GET  /agents/{name}``                          definition + versions + active spec
* ``POST /agents/{name}/versions``                 new draft version
* ``GET  /agents/{name}/versions/{v}``             one version (with spec)
* ``PUT  /agents/{name}/versions/{v}``             edit a *draft* (published = 409)
* ``POST /agents/{name}/versions/{v}/validate``    dry-run publish validation
* ``POST /agents/{name}/versions/{v}/publish``     validate, then activate
* ``POST /agents/{name}/retire``                   no new runs; builtins refuse
* the same surface under ``/workflows``

Roles: creating, editing, publishing and pausing require an ``admin``-class
role (``AGENT_HUB_ADMIN_ROLES``, default ``admin``) *or* a role named in the
definition's own ``rbac.edit`` list. Reads require any authenticated caller.

The LLM key is not here: agent-hub reads the same source as the assistant
(Settings → AI assistant, env override wins) when the runner lands in phase 2
(D4). This service has no LLM egress in phase 1.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

from .auth import caller_sub, require_auth, require_roles
from .models import NAME_RE, AgentSpec, WorkflowSpec
from .seed import seed_builtin
from .store import Conflict, Guardrail, NotFound, RegistryStore
from .validate import tool_catalog, validate_agent_spec, validate_workflow_spec

Kind = Literal["agent", "workflow"]

_SPEC_MODEL: dict[str, type[BaseModel]] = {"agent": AgentSpec, "workflow": WorkflowSpec}


def _admin_roles() -> set[str]:
    raw = os.environ.get("AGENT_HUB_ADMIN_ROLES", "admin")
    return {r.strip() for r in raw.split(",") if r.strip()}


def _seed_enabled() -> bool:
    return (os.environ.get("AGENT_HUB_SEED") or "1").lower() not in ("0", "false", "no")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fails loudly without a Postgres DSN: the registry has no file fallback.
    app.state.store = await RegistryStore.connect()
    app.state.seeded = await seed_builtin(app.state.store) if _seed_enabled() else []
    try:
        yield
    finally:
        await app.state.store.close()


app = FastAPI(
    title="PayProbe Agent Hub",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(require_auth)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:4200,http://127.0.0.1:4200"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _store(request: Request) -> RegistryStore:
    return request.app.state.store


# -- request bodies ----------------------------------------------------------------


class CreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    owner: str = Field(default="", max_length=120)
    spec: dict[str, Any]


class SpecBody(BaseModel):
    spec: dict[str, Any]


class PauseBody(BaseModel):
    paused: bool


# -- shared handlers (agents and workflows differ only in spec model + validator) --


def _parse_spec(kind: Kind, raw: dict) -> BaseModel:
    try:
        return _SPEC_MODEL[kind].model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            422,
            {
                "problems": [
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                ]
            },
        ) from exc


async def _problems(kind: Kind, spec: BaseModel, store: RegistryStore) -> list[str]:
    if kind == "agent":
        return validate_agent_spec(spec)  # type: ignore[arg-type]

    # the validator is pure/sync; resolve every referenced agent up front
    hits: dict[str, tuple[str, int, AgentSpec] | None] = {}
    for node in spec.nodes:  # type: ignore[attr-defined]
        if node.type == "agent_task" and node.agent and node.agent not in hits:
            hit = await store.resolve("agent", node.agent)
            hits[node.agent] = (
                None if hit is None else (hit[0], hit[1], AgentSpec.model_validate(hit[2]))
            )

    return validate_workflow_spec(spec, hits.get)  # type: ignore[arg-type]


async def _edit_roles(kind: Kind, name: str, store: RegistryStore) -> set[str]:
    roles = set(_admin_roles())
    try:
        d = await store.get(kind, name)
    except NotFound:
        return roles
    spec = d.get("spec") or {}
    roles |= set((spec.get("rbac") or {}).get("edit") or [])
    return roles


async def _wrap(coro):
    try:
        return await coro
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Guardrail as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"error": str(exc), "guardrail": True}
        ) from exc


async def _list(kind: Kind, request: Request, status_: str | None) -> list[dict]:
    return await _store(request).list(kind, status_)


async def _create(kind: Kind, request: Request, body: CreateBody) -> dict:
    require_roles(request, _admin_roles())
    if not NAME_RE.fullmatch(body.name):
        raise HTTPException(422, "name must be a slug: [a-z0-9][a-z0-9-]{1,63}")
    spec = _parse_spec(kind, body.spec)
    return await _wrap(
        _store(request).create(kind, body.name, spec.model_dump(), caller_sub(request), body.owner)
    )


async def _get(kind: Kind, request: Request, name: str) -> dict:
    return await _wrap(_store(request).get(kind, name))


async def _add_version(kind: Kind, request: Request, name: str, body: SpecBody) -> dict:
    store = _store(request)
    require_roles(request, await _edit_roles(kind, name, store))
    spec = _parse_spec(kind, body.spec)
    return await _wrap(store.add_version(kind, name, spec.model_dump(), caller_sub(request)))


async def _get_version(kind: Kind, request: Request, name: str, version: int) -> dict:
    return await _wrap(_store(request).get_version(kind, name, version))


async def _update_draft(
    kind: Kind, request: Request, name: str, version: int, body: SpecBody
) -> dict:
    store = _store(request)
    require_roles(request, await _edit_roles(kind, name, store))
    spec = _parse_spec(kind, body.spec)
    return await _wrap(
        store.update_draft(kind, name, version, spec.model_dump(), caller_sub(request))
    )


async def _validate(kind: Kind, request: Request, name: str, version: int) -> dict:
    store = _store(request)
    v = await _wrap(store.get_version(kind, name, version))
    spec = _parse_spec(kind, v["spec"])
    problems = await _problems(kind, spec, store)
    return {"name": name, "version": version, "valid": not problems, "problems": problems}


async def _publish(kind: Kind, request: Request, name: str, version: int) -> dict:
    store = _store(request)
    require_roles(request, await _edit_roles(kind, name, store))
    v = await _wrap(store.get_version(kind, name, version))
    if v["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{kind} '{name}' v{version} is already {v['status']}"
        )
    spec = _parse_spec(kind, v["spec"])
    problems = await _problems(kind, spec, store)
    if problems:
        raise HTTPException(422, {"problems": problems})
    return await _wrap(store.publish(kind, name, version, caller_sub(request)))


async def _retire(kind: Kind, request: Request, name: str) -> dict:
    store = _store(request)
    require_roles(request, await _edit_roles(kind, name, store))
    return await _wrap(store.retire(kind, name, caller_sub(request)))


# -- routes ---------------------------------------------------------------------------


@app.get("/health")
async def health(request: Request) -> dict:
    store: RegistryStore | None = getattr(request.app.state, "store", None)
    if store is None:
        return {"status": "ok", "service": "agent-hub", "paused": None}
    return {
        "status": "ok",
        "service": "agent-hub",
        "paused": (await store.paused())["paused"],
        "schema_version": await store.schema_version(),
    }


@app.get("/catalog")
def catalog() -> dict:
    return {
        "tools": tool_catalog(),
        "modes": ["advisor", "plan", "full"],
        "node_types": ["agent_task", "tool", "condition", "approval", "parallel", "join"],
        "trigger_kinds": ["manual", "schedule", "event", "mcp"],
    }


@app.get("/pause")
async def get_pause(request: Request) -> dict:
    return await _store(request).paused()


@app.put("/pause")
async def put_pause(request: Request, body: PauseBody) -> dict:
    require_roles(request, _admin_roles())
    return await _store(request).set_paused(body.paused, caller_sub(request))


def _mount(kind: Kind, prefix: str) -> None:
    @app.get(prefix, name=f"list_{kind}s")
    async def list_(
        request: Request, status_: str | None = Query(default=None, alias="status")
    ) -> list[dict]:
        return await _list(kind, request, status_)

    @app.post(prefix, status_code=201, name=f"create_{kind}")
    async def create(request: Request, body: CreateBody) -> dict:
        return await _create(kind, request, body)

    @app.get(prefix + "/{name}", name=f"get_{kind}")
    async def get(request: Request, name: str) -> dict:
        return await _get(kind, request, name)

    @app.post(prefix + "/{name}/versions", status_code=201, name=f"add_{kind}_version")
    async def add_version(request: Request, name: str, body: SpecBody) -> dict:
        return await _add_version(kind, request, name, body)

    @app.get(prefix + "/{name}/versions/{version}", name=f"get_{kind}_version")
    async def get_version(request: Request, name: str, version: int) -> dict:
        return await _get_version(kind, request, name, version)

    @app.put(prefix + "/{name}/versions/{version}", name=f"update_{kind}_draft")
    async def update_draft(request: Request, name: str, version: int, body: SpecBody) -> dict:
        return await _update_draft(kind, request, name, version, body)

    @app.post(prefix + "/{name}/versions/{version}/validate", name=f"validate_{kind}")
    async def validate(request: Request, name: str, version: int) -> dict:
        return await _validate(kind, request, name, version)

    @app.post(prefix + "/{name}/versions/{version}/publish", name=f"publish_{kind}")
    async def publish(request: Request, name: str, version: int) -> dict:
        return await _publish(kind, request, name, version)

    @app.post(prefix + "/{name}/retire", name=f"retire_{kind}")
    async def retire(request: Request, name: str) -> dict:
        return await _retire(kind, request, name)


_mount("agent", "/agents")
_mount("workflow", "/workflows")
