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
* ``POST /agents/{name}/wake``, ``GET /heartbeats[/{id}]``, cancel, revert   (phase 2)
* ``POST /workflows/{name}/run``, ``GET /runs[/{id}]``, ``POST /runs/{id}/cancel``,
  ``GET /approvals[/{id}]``, ``POST /approvals/{id}/decide``, ``GET /plans[/{id}]``
  (phase 3: the workflow engine, see :mod:`agent_hub.engine`)

Roles: creating, editing, publishing and pausing require an ``admin``-class
role (``AGENT_HUB_ADMIN_ROLES``, default ``admin``) *or* a role named in the
definition's own ``rbac.edit`` list. Reads require any authenticated caller.

The LLM key is not here: agent-hub reads the same source as the assistant
(Settings → AI assistant, env override wins) when the runner lands in phase 2
(D4). This service has no LLM egress in phase 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import egress, rest
from .alerts import Alerter, verify
from .auth import caller_sub, require_auth, require_roles
from .engine import Engine
from .llm import ProviderLLMBackend, resolve_llm
from .models import NAME_RE, AgentSpec, WorkflowSpec
from .principal import mint_obo
from .runner import Outcome, run_heartbeat
from .seed import seed_builtin
from .store import Conflict, Guardrail, NotFound, RegistryStore
from .triggers import WakeSources
from .validate import tool_catalog, validate_agent_spec, validate_workflow_spec

log = logging.getLogger(__name__)

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
    # Phase-2 seams (tests replace them): how a heartbeat reaches the LLM and
    # the platform. Production: the platform-managed provider config (D4) and
    # the shared REST backend bound to the heartbeat's on-behalf-of token.
    app.state.llm_factory = _default_llm_factory
    app.state.backend_factory = _default_backend_factory
    app.state.tasks = set()
    app.state.alerts = Alerter.from_env()
    # Restart watchdog: a row still `running` from a previous process can never
    # finish, and while it stands every wake of that agent coalesces onto it.
    for hb in await app.state.store.reconcile_running():
        app.state.alerts.emit_for(hb, mode=None)
    # Phase 3: the workflow engine resumes every active run from its row, then
    # expires timed-out approvals on a slow tick.
    app.state.engine = Engine(
        app.state.store,
        app.state.alerts,
        launch_heartbeat=lambda **kw: launch_heartbeat(app, **kw),
        tool_backend=lambda run: app.state.backend_factory(_run_token(run)),
    )
    await app.state.engine.reconcile()
    tick_s = float(os.environ.get("AGENT_HUB_ENGINE_TICK_S") or 30)
    app.state.engine.start_ticker(tick_s)
    # Phase 4: platform events (POST /events) and schedule triggers wake agents
    # through the same heartbeat code path. AGENT_HUB_SCHEDULER=0 keeps the
    # schedule ticker off (events still work).
    app.state.wakes = WakeSources(app.state.store, launch=lambda **kw: launch_heartbeat(app, **kw))
    if (os.environ.get("AGENT_HUB_SCHEDULER") or "1").lower() not in ("0", "false", "no"):
        app.state.wakes.start_ticker(tick_s)
    try:
        # D2: the folded-in assistant keeps its own session/chat stores; a
        # mounted app's lifespan does not run by itself, so run it from here.
        async with AsyncExitStack() as stack:
            sub = getattr(app.state, "assistant", None)
            if sub is not None:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield
    finally:
        app.state.wakes.stop()
        app.state.engine.stop()
        for t in list(app.state.tasks):
            t.cancel()
        app.state.alerts.cancel()
        await app.state.store.close()


def _run_token(run: dict) -> str:
    """OBO token for a workflow's ``tool`` node: the invoking user, acting
    through the run (``act`` names the workflow and run, never ``svc``)."""
    return mint_obo(
        run.get("principal") or {}, f"workflow:{run['workflow']}", run["version"], run["id"], 600
    )


def _default_llm_factory(spec: AgentSpec):
    llm = resolve_llm()
    if not llm.get("enabled"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no LLM provider configured: set it under Settings → AI assistant "
            "(or ASSIST_LLM_* env)",
        )
    return ProviderLLMBackend(llm, model=spec.model, temperature=spec.temperature)


def _default_backend_factory(token: str):
    from payprobe_common.rest_backend import RestBackend

    return RestBackend(rest.request_as(token), rest.SCENARIO_API, rest.RUN_API, rest.INSIGHT_API)


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
    alerts: Alerter | None = getattr(request.app.state, "alerts", None)
    return {
        "status": "ok",
        "service": "agent-hub",
        "paused": (await store.paused())["paused"],
        "schema_version": await store.schema_version(),
        "alerts": alerts.stats() if alerts else None,
        "egress": sorted(egress.allowed_hosts()),
    }


@app.get("/catalog")
def catalog() -> dict:
    return {
        "tools": tool_catalog(),
        "modes": ["advisor", "plan", "full"],
        "node_types": ["agent_task", "tool", "condition", "approval", "parallel", "join"],
        "trigger_kinds": ["manual", "schedule", "event", "mcp", "webhook"],
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


# -- heartbeats (phase 2) ------------------------------------------------------------------


class WakeBody(BaseModel):
    input: str = Field(default="", max_length=20_000)
    version: int | None = None
    wake: Literal["manual", "schedule", "event", "mcp"] = "manual"


def _caller(request: Request) -> dict:
    return getattr(request.state, "auth", None) or {}


async def launch_heartbeat(
    app_: FastAPI,
    *,
    name: str,
    version: int,
    spec: AgentSpec,
    spec_sha256: str,
    caller: dict,
    input_text: str,
    wake: str,
    on_done: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """The one heartbeat code path, shared by ``POST /agents/{name}/wake`` and
    the workflow engine's ``agent_task`` nodes.

    Returns the heartbeat row. A refusal (``paused`` / ``budget_exceeded``) is
    recorded and returned without running; a wake on an agent that already has
    a running heartbeat returns that row with ``coalesced: true``; otherwise the
    row is ``running`` and ``on_done`` (if given) is awaited with the finished
    record. ``spec`` may carry a narrowed mode (a workflow node may narrow,
    never escalate); ``spec_sha256`` stays the registered version's hash.
    """
    store: RegistryStore = app_.state.store
    hb = {
        "id": uuid.uuid4().hex,
        "agent": name,
        "version": version,
        "spec_sha256": spec_sha256,
        "wake": wake,
        "invoked_by": str(caller.get("sub") or "anonymous"),
        "principal": {"sub": caller.get("sub"), "roles": caller.get("roles") or []},
        "input": input_text,
    }
    if (await store.paused())["paused"]:
        return await store.refuse_heartbeat(hb, "paused", "agents are paused")
    budget = spec.budget.daily_tokens
    if budget and await store.tokens_today(name) >= budget:
        refused = await store.refuse_heartbeat(
            hb, "budget_exceeded", f"daily token budget ({budget}) spent; hard stop"
        )
        app_.state.alerts.emit_for(refused, mode=spec.mode)
        return refused
    running = await store.running_heartbeat(name)
    if running:
        return {**running, "coalesced": True}

    llm = app_.state.llm_factory(spec)  # 503 when not configured
    hb["model"] = llm.model
    token = mint_obo(caller, name, version, hb["id"], spec.limits.wall_clock_s + 60)
    backend = app_.state.backend_factory(token)
    row = await store.start_heartbeat(hb)
    loop = asyncio.get_running_loop()

    def flags() -> dict:
        async def read() -> dict:
            return {
                "paused": (await store.paused())["paused"],
                "cancel": await store.cancel_requested(hb["id"]),
            }

        return asyncio.run_coroutine_threadsafe(read(), loop).result(timeout=10)

    async def execute() -> None:
        try:
            out: Outcome = await loop.run_in_executor(
                None, lambda: run_heartbeat(spec, input_text, backend, llm, flags)
            )
        except Exception as exc:  # noqa: BLE001 — never leave a row 'running'
            out = Outcome(status="failed", error=f"{type(exc).__name__}: {exc}")
        finished = await store.record_heartbeat(
            hb["id"],
            out.status,
            steps=out.steps,
            proposed=out.proposed,
            journal=out.journal,
            tokens=out.tokens,
            result=out.result,
            error=out.error,
        )
        app_.state.alerts.emit_for(finished, mode=spec.mode)
        if on_done is not None:
            try:
                await on_done(finished)
            except Exception:  # noqa: BLE001 — a consumer's failure never leaks into the row
                log.exception("heartbeat %s on_done failed", hb["id"])

    task = asyncio.create_task(execute())
    app_.state.tasks.add(task)
    task.add_done_callback(app_.state.tasks.discard)
    return row


@app.post("/agents/{name}/wake", status_code=202, response_model=None)
async def wake_agent(request: Request, name: str, body: WakeBody) -> dict | JSONResponse:
    """Start one heartbeat of ``name`` (its active version, or ``version``).

    Refused, and recorded as such, when agents are paused or the agent's daily
    token budget is spent. Coalesced (200, ``coalesced: true``) when the agent
    already has a running heartbeat. Otherwise 202 with the ``running`` row;
    poll ``GET /heartbeats/{id}`` for the outcome.
    """
    store = _store(request)
    ref = f"{name}@{body.version}" if body.version else name
    hit = await store.resolve("agent", ref)
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no runnable version for '{ref}'")
    _, version, raw = hit
    spec = AgentSpec.model_validate(raw)
    require_roles(request, set(spec.rbac.invoke) | _admin_roles())
    ver = await store.get_version("agent", name, version)
    row = await launch_heartbeat(
        request.app,
        name=name,
        version=version,
        spec=spec,
        spec_sha256=ver["spec_sha256"],
        caller=_caller(request),
        input_text=body.input,
        wake=body.wake,
    )
    # Refusals and coalescing are 200 with the record: nothing was accepted
    # for processing. Only a freshly started heartbeat is 202.
    if row.get("coalesced") or row["status"] != "running":
        return JSONResponse(row)
    return row


@app.get("/heartbeats")
async def list_heartbeats(
    request: Request, agent: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    return await _store(request).list_heartbeats(agent, limit)


@app.get("/heartbeats/{hb_id}")
async def get_heartbeat(request: Request, hb_id: str) -> dict:
    return await _wrap(_store(request).get_heartbeat(hb_id))


@app.post("/heartbeats/{hb_id}/cancel")
async def cancel_heartbeat(request: Request, hb_id: str) -> dict:
    store = _store(request)
    hb = await _wrap(store.get_heartbeat(hb_id))
    hit = await store.resolve("agent", f"{hb['agent']}@{hb['version']}")
    invoke = set(AgentSpec.model_validate(hit[2]).rbac.invoke) if hit else set()
    require_roles(request, invoke | _admin_roles())
    return await _wrap(store.request_cancel(hb_id))


@app.post("/heartbeats/{hb_id}/revert")
async def revert_heartbeat(request: Request, hb_id: str) -> dict:
    """Undo every journalled write of a finished heartbeat (newest first),
    using the caller's own credential — reverting is the human's act."""
    store = _store(request)
    hb = await _wrap(store.get_heartbeat(hb_id))
    require_roles(request, _admin_roles())
    if hb["status"] == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "heartbeat is still running")
    if not hb["journal"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "nothing to revert")
    from payprobe_common import agent_toolkit as tk

    bearer = (request.headers.get("authorization") or "")[len("Bearer ") :].strip()
    backend = request.app.state.backend_factory(bearer or "dev")
    ctx = tk.ToolContext(backend=backend)
    n = await asyncio.get_running_loop().run_in_executor(
        None, lambda: tk.restore_journal(ctx, hb["journal"])
    )
    row = await store.mark_reverted(hb_id)
    return {**row, "reverted": n}


# -- workflow runs and approvals (phase 3) ------------------------------------------------


class RunBody(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None


class DecideBody(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2_000)


def _engine(request: Request) -> Engine:
    return request.app.state.engine


@app.post("/workflows/{name}/run", status_code=202)
async def run_workflow(request: Request, name: str, body: RunBody) -> dict:
    """Start a run of ``name`` (active version, or ``version``). Returns the run
    after its first advance: already ``waiting`` if the first node is an
    approval, ``running`` while agent tasks are in flight. Admin-class roles
    only for now: workflows carry no rbac of their own yet."""
    require_roles(request, _admin_roles())
    store = _store(request)
    ref = f"{name}@{body.version}" if body.version else name
    hit = await store.resolve("workflow", ref)
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no runnable version for '{ref}'")
    _, version, raw = hit
    spec = WorkflowSpec.model_validate(raw)
    missing = [k for k in spec.inputs if k not in body.inputs]
    if missing:
        raise HTTPException(422, {"problems": [f"missing input '{k}'" for k in missing]})
    ver = await store.get_version("workflow", name, version)
    return await _engine(request).start(
        name, version, spec, ver["spec_sha256"], inputs=body.inputs, caller=_caller(request)
    )


@app.get("/runs")
async def list_runs(
    request: Request,
    workflow: str | None = None,
    status_: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return await _store(request).list_runs(workflow, status_, limit)


@app.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict:
    return await _wrap(_store(request).get_run(run_id))


@app.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> dict:
    require_roles(request, _admin_roles())
    return await _wrap(_engine(request).cancel(run_id))


@app.get("/approvals")
async def list_approvals(
    request: Request,
    status_: str | None = Query(default="pending", alias="status"),
    run: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """The approvals inbox. ``status=all`` lists every decision too."""
    st = None if status_ in (None, "", "all") else status_
    return await _store(request).list_approvals(st, run, limit)


@app.get("/approvals/{ap_id}")
async def get_approval(request: Request, ap_id: str) -> dict:
    return await _wrap(_store(request).get_approval(ap_id))


@app.post("/approvals/{ap_id}/decide")
async def decide_approval(request: Request, ap_id: str, body: DecideBody) -> dict:
    """A human decision. Needs one of the approval node's ``roles`` (or admin);
    the decider is recorded and the run moves on (or ends ``rejected``)."""
    store = _store(request)
    ap = await _wrap(store.get_approval(ap_id))
    require_roles(request, set(ap["roles"]) | _admin_roles())
    return await _wrap(
        _engine(request).decide(ap_id, body.decision, caller_sub(request), body.note)
    )


@app.get("/plans")
async def list_plans(
    request: Request, run: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict]:
    return await _store(request).list_plans(run, limit)


@app.get("/plans/{plan_id}")
async def get_plan(request: Request, plan_id: str) -> dict:
    return await _wrap(_store(request).get_plan(plan_id))


# -- platform events (phase 4) --------------------------------------------------------------


class EventBody(BaseModel):
    event: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    subject: dict[str, Any] = Field(default_factory=dict)
    at: str | None = None


@app.post("/events", status_code=202)
async def post_event(request: Request, body: EventBody) -> dict:
    """A run-lifecycle event from the platform (the orchestrator's service
    token, or an admin). Every active agent with a matching ``event`` trigger
    gets one heartbeat with the event as input; coalescing and refusals apply
    as for any wake. Unknown events wake nobody and are still 202."""
    require_roles(request, _admin_roles())
    payload = {
        "event": body.event,
        "at": body.at or datetime.now(UTC).isoformat(),
        **body.subject,
    }
    woken = await request.app.state.wakes.on_event(body.event, payload)
    return {"event": body.event, "woken": woken}


# -- inbound webhooks (phase 4) --------------------------------------------------------------
#
# For systems that hold no PayProbe token (CI, an external scheduler, a
# monitoring tool): the body is signed with AGENT_HUB_WEBHOOK_SECRET using the
# same ``t=<ts>,v1=<HMAC-SHA256("<ts>.<body>")>`` scheme the outbound alerts
# use, so one secret and one verifier serve both directions. The bearer gate
# skips ``/webhooks/``; these routes verify the signature themselves and
# answer 503 while no secret is configured.

_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


async def _verified_webhook_body(request: Request) -> bytes:
    secret = (os.environ.get("AGENT_HUB_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "inbound webhooks are not configured (set AGENT_HUB_WEBHOOK_SECRET)",
        )
    raw = await request.body()
    if len(raw) > 64_000:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "webhook body over 64 KB")
    if not verify(
        secret, raw.decode("utf-8", "replace"), request.headers.get("X-PayProbe-Signature", "")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing X-PayProbe-Signature")
    return raw


@app.post("/webhooks/events/{event}", status_code=202)
async def webhook_event(request: Request, event: str) -> dict:
    """A signed external event: wakes every agent with a matching ``event``
    trigger, exactly like ``POST /events``; the JSON body is the subject."""
    raw = await _verified_webhook_body(request)
    if not _EVENT_RE.fullmatch(event) or len(event) > 64:
        raise HTTPException(422, "event must match [a-z][a-z0-9_.-]* (max 64)")
    subject: Any = {}
    if raw.strip():
        try:
            subject = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(422, "webhook body must be JSON") from exc
        if not isinstance(subject, dict):
            raise HTTPException(422, "webhook body must be a JSON object")
    payload = {"event": event, "at": datetime.now(UTC).isoformat(), "source": "webhook", **subject}
    woken = await request.app.state.wakes.on_event(event, payload)
    return {"event": event, "woken": woken}


@app.post("/webhooks/agents/{name}", status_code=202, response_model=None)
async def webhook_agent(request: Request, name: str) -> dict | JSONResponse:
    """A signed direct wake of one agent; the body (any text, JSON preferred)
    is the heartbeat input. Opt-in: the agent's active spec must declare a
    ``webhook`` trigger, otherwise 409. Same 202 / 200 semantics as wake."""
    raw = await _verified_webhook_body(request)
    store = _store(request)
    hit = await store.resolve("agent", name)
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no runnable version for '{name}'")
    _, version, spec_raw = hit
    spec = AgentSpec.model_validate(spec_raw)
    if not any(t.kind == "webhook" for t in spec.triggers):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"agent '{name}' does not declare a webhook trigger"
        )
    ver = await store.get_version("agent", name, version)
    row = await launch_heartbeat(
        request.app,
        name=name,
        version=version,
        spec=spec,
        spec_sha256=ver["spec_sha256"],
        caller={"sub": f"webhook:{name}", "roles": []},
        input_text=raw.decode("utf-8", "replace")[:20_000],
        wake="webhook",
    )
    if row.get("coalesced") or row["status"] != "running":
        return JSONResponse(row)
    return row


# -- D2: the standalone :8400 assistant, folded in --------------------------------------
#
# The assistant's FastAPI app is mounted under ``/assistant`` unchanged (its own
# auth gate, session and chat stores; its lifespan runs from ours). nginx and the
# orchestrator probe point here; the ``assistant`` compose service stays one
# release as a deprecated alias, then goes. ``/assistant/health`` is answered by
# agent-hub itself, registered before the mount so it stays public (the mounted
# gate sees the full path and would demand a token).


@app.get("/assistant/health")
async def assistant_health(request: Request) -> dict:
    mounted = getattr(request.app.state, "assistant", None) is not None
    return {
        "status": "ok" if mounted else "unavailable",
        "service": "assistant",
        "via": "agent-hub",
        "mounted": mounted,
    }


def _mount_assistant() -> Any | None:
    try:
        from assistant_service.main import app as assistant_app
    except ImportError as exc:  # the image always ships it; dev checkouts may not
        log.warning("assistant_service not importable (%s): /assistant not mounted", exc)
        return None
    app.mount("/assistant", assistant_app)
    return assistant_app


app.state.assistant = _mount_assistant()
