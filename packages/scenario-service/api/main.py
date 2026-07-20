"""PayProbe Scenario Service — CRUD + validation + versioning.

Run locally (SQLite, zero config):
    uvicorn api.main:app --reload --port 8000

Run against PostgreSQL (production / docker compose):
    DATABASE_URL=postgresql://payprobe:payprobe@postgres:5432/payprobe \\
        uvicorn api.main:app --port 8000

Environment:
    DATABASE_URL       postgres DSN or SQLite path (default: scenarios.db)
    SCENARIO_SEED_DIR  example scenarios imported on first start
    CORS_ORIGINS       comma-separated allowed origins
    API_TOKEN          if set, all /scenarios* endpoints require
                       ``Authorization: Bearer <token>``
    MAX_BODY_BYTES     request size limit (default 1 MiB)

The portal regenerates its typed client from this app's OpenAPI schema
(`npm run generate:api` in packages/portal).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from models import (
    STEP_CATALOG,
    Project,
    ProjectDraft,
    Scenario,
    ScenarioDraft,
    ScenarioSet,
    ScenarioSetDraft,
    ScenarioSummary,
    ScenarioVersionInfo,
    SearchResults,
    TargetSpec,
    TestClass,
    ValidationIssue,
    ValidationResult,
    validate_scenario,
)
from models.message_format import MessageFormat, MessageFormatDraft
from models.pack import Pack, get_pack, list_packs
from models.starter_flow import StarterFlow, StarterFlowDraft
from models.iso8583_analyzer import (
    DEFAULT_FIELDS as ISO8583_DEFAULT_FIELDS,
    analyze_message as iso8583_analyze_message,
    build_message as iso8583_build_message,
    build_tlv as iso8583_build_tlv,
    diff_messages as iso8583_diff_messages,
)
from .auth import require_auth
from .assist_store import AssistConfigDraft, AssistConfigStore
from .catalog_store import CatalogStore
from .connection_store import Connection, ConnectionDraft, ConnectionStore
from .flow_store import StarterFlowStore
from .participant_flow_store import ParticipantFlowStore
from models.participant_flow import (
    ParticipantFlow,
    ParticipantFlowDraft,
    ParticipantFlowVersionInfo,
)
from .network_flow_store import NetworkFlowStore
from models.network_flow import (
    NetworkFlow,
    NetworkFlowDraft,
    NetworkFlowPlan,
    plan_network_flow,
    validate_network_flow,
)
from .participant_group_store import ParticipantGroupStore
from models.participant_group import ParticipantGroup, ParticipantGroupDraft
from .topology_store import TopologyStore  # legacy: only seeds network flows
from .format_store import FormatStore
from .table_store import DataTable, DataTableDraft, TableStore
from .test_data_store import (
    BinRange,
    BinRangeDraft,
    CardPool,
    CardPoolDraft,
    KeyDraft,
    KeyView,
    TerminalPool,
    TerminalPoolDraft,
    TestDataStore,
)
from .variable_store import VariableStore
from .environment_store import Environment, EnvironmentDraft, EnvironmentStore
from .nats_server_store import NatsServer, NatsServerDraft, NatsServerStore
from .env_migration import (
    apply_backfill,
    apply_seed_defaults,
    apply_slim,
    find_collisions,
    plan_backfill,
    plan_seed_defaults,
    plan_slim,
)
from .store import (
    ForeignScenario,
    ProjectNotEmpty,
    ProjectNotFound,
    ScenarioNotFound,
    SetNotFound,
    VersionConflict,
    create_store,
)

log = structlog.get_logger("scenario-service")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", os.environ.get("SCENARIO_DB", "scenarios.db")
)
def _default_seed_dir() -> str:
    # Repo layout: packages/scenario-service/api/main.py -> repo root is parents[3].
    # In a container the app is shallower (e.g. /app/api/main.py), so guard the
    # index and fall back to a local ./examples/scenarios.
    root = Path(__file__).resolve()
    base = root.parents[3] if len(root.parents) > 3 else root.parent
    return str(base / "examples" / "scenarios")


# NB: don't pass the default as the 2nd arg to os.environ.get — Python evaluates
# it eagerly even when SCENARIO_SEED_DIR is set, which crashed in containers.
SEED_DIR = os.environ.get("SCENARIO_SEED_DIR") or _default_seed_dir()
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 1024 * 1024))


def _default_catalog_file() -> str:
    """Persist the catalog next to a SQLite scenarios db; else in-memory."""
    explicit = os.environ.get("CATALOG_FILE")
    if explicit is not None:
        return explicit
    # DATABASE_URL pointing at a sqlite file -> drop catalog.json beside it.
    if DATABASE_URL and "://" not in DATABASE_URL and DATABASE_URL != ":memory:":
        return str(Path(DATABASE_URL).resolve().parent / "catalog.json")
    return ":memory:"


CATALOG_FILE = _default_catalog_file()


def _sibling_file(name: str) -> str:
    if os.environ.get(name.upper()):
        return os.environ[name.upper()]
    if DATABASE_URL and "://" not in DATABASE_URL and DATABASE_URL != ":memory:":
        return str(Path(DATABASE_URL).resolve().parent / f"{name}.json")
    return ":memory:"


FORMATS_FILE = _sibling_file("formats")
VARIABLES_FILE = _sibling_file("variables")
TABLES_FILE = _sibling_file("tables")
CONNECTIONS_FILE = _sibling_file("connections")
FLOWS_FILE = _sibling_file("flows")
PARTICIPANT_FLOWS_FILE = _sibling_file("participant_flows")
PARTICIPANT_GROUPS_FILE = _sibling_file("participant_groups")
TOPOLOGIES_FILE = _sibling_file("topologies")
NETWORK_FLOWS_FILE = _sibling_file("network_flows")
ASSIST_FILE = _sibling_file("assist")
TEST_DATA_FILE = _sibling_file("test_data")
ENVIRONMENTS_FILE = _sibling_file("environments")
NATS_SERVERS_FILE = _sibling_file("nats_servers")
# Bundled environments to seed the registry with on first start (so the editor
# shows mock/grpc/… as editable). Sibling of the scenario seed dir.
ENV_SEED_DIR = os.environ.get("ENV_SEED_DIR") or str(Path(SEED_DIR).parent / "environments")
# Bundled connection examples to seed the registry with on first start (the
# modern shape — adapter instances are connections, not env-inline adapters).
CONN_SEED_DIR = os.environ.get("CONN_SEED_DIR") or str(Path(SEED_DIR).parent / "connections")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = await create_store(DATABASE_URL)
    app.state.store = store
    app.state.catalog = CatalogStore(CATALOG_FILE)
    app.state.formats = FormatStore(FORMATS_FILE)
    app.state.variables = VariableStore(VARIABLES_FILE)
    app.state.tables = TableStore(TABLES_FILE)
    app.state.connections = ConnectionStore(CONNECTIONS_FILE)
    app.state.connections.seed_from_dir(CONN_SEED_DIR)
    app.state.flows = StarterFlowStore(FLOWS_FILE)
    app.state.participant_flows = ParticipantFlowStore(PARTICIPANT_FLOWS_FILE)
    app.state.participant_groups = ParticipantGroupStore(PARTICIPANT_GROUPS_FILE)
    app.state.topologies = TopologyStore(TOPOLOGIES_FILE)
    app.state.network_flows = NetworkFlowStore(NETWORK_FLOWS_FILE)
    migrated = app.state.network_flows.migrate_topologies(app.state.topologies.list())
    if migrated:
        log.info("network_flows_migrated", count=len(migrated), ids=migrated)
    app.state.assist = AssistConfigStore(ASSIST_FILE)
    app.state.test_data = TestDataStore(TEST_DATA_FILE)
    app.state.environments = EnvironmentStore(ENVIRONMENTS_FILE)
    app.state.environments.seed_from_dir(ENV_SEED_DIR)
    app.state.nats_servers = NatsServerStore(NATS_SERVERS_FILE)
    # Seed one default cluster so the portal's NATS page connects out of the box
    # against the bundled compose broker. Override the URLs for a host-run stack
    # via NATS_SEED_SERVERS / NATS_SEED_MONITORING (comma-separated).
    app.state.nats_servers.seed_default(
        [s.strip() for s in os.environ.get(
            "NATS_SEED_SERVERS",
            "nats://nats-1:4222,nats://nats-2:4222,nats://nats-3:4222",
        ).split(",") if s.strip()],
        [s.strip() for s in os.environ.get(
            "NATS_SEED_MONITORING",
            "http://nats-1:8222,http://nats-2:8222,http://nats-3:8222",
        ).split(",") if s.strip()],
    )
    from .agent_session import build_session_store
    app.state.assist_sessions = build_session_store()
    if await store.count() == 0 and Path(SEED_DIR).is_dir():
        imported = await store.seed_from_dir(Path(SEED_DIR))
        log.info("seeded_examples", count=imported, source=SEED_DIR)
    log.info("startup", backend=type(store).__name__, catalog=CATALOG_FILE)
    yield
    await store.close()


app = FastAPI(title="PayProbe Scenario Service", version="0.2.0", lifespan=lifespan)


def _scalar_reference(title: str, spec_url: str = "/openapi.json") -> HTMLResponse:
    """Render an interactive API reference (Scalar) for an OpenAPI spec.

    Modern, try-it-out reference with a built-in request client and code
    samples — served alongside FastAPI's classic /docs (Swagger) and /redoc.
    Loaded from a CDN so there's no extra Python dependency.
    """
    html = f"""<!doctype html>
<html>
  <head>
    <title>{title}</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>body {{ margin: 0 }}</style>
  </head>
  <body>
    <script id="api-reference" data-url="{spec_url}"></script>
    <script>
      var c = document.getElementById('api-reference');
      c.dataset.configuration = JSON.stringify({{ theme: 'purple', layout: 'modern' }});
    </script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
  </body>
</html>"""
    return HTMLResponse(html)


@app.get("/reference", include_in_schema=False)
async def api_reference() -> HTMLResponse:
    """Interactive API reference (Scalar)."""
    return _scalar_reference("PayProbe Scenario Service — API Reference")


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:4200,http://127.0.0.1:4200"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body exceeds {MAX_BODY_BYTES} bytes"},
        )
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    return response


# -- Prometheus metrics --------------------------------------------------------

from .metrics import (  # noqa: E402
    Counter as _Counter, Histogram as _Histogram,
    render as _render_metrics, CONTENT_TYPE as _METRICS_CT,
)

_HTTP_REQUESTS = _Counter(
    "payprobe_http_requests_total", "HTTP requests by route and status.",
    ["method", "route", "code"],
)
_HTTP_LATENCY = _Histogram(
    "payprobe_http_request_duration_seconds", "HTTP request latency (s).", ["route"],
)


@app.middleware("http")
async def metrics_mw(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "other"
        _HTTP_LATENCY.observe(time.perf_counter() - start, route=route)
        _HTTP_REQUESTS.inc(method=request.method, route=route, code=str(status))


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(_render_metrics(), media_type=_METRICS_CT)


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: the data store answers."""
    try:
        await request.app.state.store.count()
        return JSONResponse({"ready": True, "checks": {"store": "ok"}})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ready": False, "checks": {"store": f"error: {exc}"}}, status_code=503
        )


class SaveRequest(BaseModel):
    scenario: ScenarioDraft
    comment: str = ""
    base_version: int | None = None
    """Version the client edited. If set and stale, the save is rejected (409)."""
    project_id: str | None = None
    """On create: target project (default project if omitted).
    On update: moves the scenario when it differs from the current project."""


def get_store(request: Request):
    return request.app.state.store


router = APIRouter(dependencies=[Depends(require_auth)])


@app.get("/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "scenarios": await request.app.state.store.count()}


def get_catalog_store(request: Request) -> CatalogStore:
    return request.app.state.catalog


@router.get("/catalog", response_model=list[TargetSpec], operation_id="getCatalog")
async def get_catalog(catalog: CatalogStore = Depends(get_catalog_store)) -> list[TargetSpec]:
    """The palette catalog: built-ins merged with custom entries/overrides."""
    return catalog.merged()


@router.get("/catalog/manage", operation_id="manageCatalog")
async def manage_catalog(catalog: CatalogStore = Depends(get_catalog_store)) -> dict:
    """Every target with provenance flags (builtin/custom/overridden/hidden)."""
    return catalog.manage_view()


@router.put("/catalog/targets/{target}", response_model=TargetSpec, operation_id="saveCatalogTarget")
async def save_catalog_target(
    target: str, spec: TargetSpec, catalog: CatalogStore = Depends(get_catalog_store),
) -> TargetSpec:
    if spec.target != target:
        raise HTTPException(400, "target id in body must match the URL")
    if not spec.actions:
        raise HTTPException(422, "a target must have at least one action")
    return catalog.upsert(spec)


@router.delete("/catalog/targets/{target}", status_code=204, response_class=Response,
               operation_id="deleteCatalogTarget")
async def delete_catalog_target(
    target: str, catalog: CatalogStore = Depends(get_catalog_store),
) -> Response:
    """Delete a custom target, or hide a built-in one."""
    try:
        catalog.delete(target)
    except KeyError:
        raise HTTPException(404, f"no catalog target '{target}'")
    return Response(status_code=204)


@router.post("/catalog/targets/{target}/restore", response_model=list[TargetSpec],
             operation_id="restoreCatalogTarget")
async def restore_catalog_target(
    target: str, catalog: CatalogStore = Depends(get_catalog_store),
) -> list[TargetSpec]:
    """Drop an override and/or unhide — return a built-in to its default."""
    catalog.restore(target)
    return catalog.merged()


# -- message format registry (versioned ISO8583 / ISO20022 specs) -------------

def get_format_store(request: Request) -> FormatStore:
    return request.app.state.formats


class CloneFormatRequest(BaseModel):
    name: str | None = None
    version: str | None = None


@router.get("/formats", response_model=list[MessageFormat], operation_id="listFormats")
async def list_formats(
    protocol: str | None = None, formats: FormatStore = Depends(get_format_store),
) -> list[MessageFormat]:
    return formats.list(protocol)


@router.get("/formats/{fid}", response_model=MessageFormat, operation_id="getFormat")
async def get_format(fid: str, formats: FormatStore = Depends(get_format_store)) -> MessageFormat:
    fmt = formats.get(fid)
    if fmt is None:
        raise HTTPException(404, f"no message format '{fid}'")
    return fmt


@router.post("/formats", response_model=MessageFormat, status_code=201, operation_id="createFormat")
async def create_format(
    draft: MessageFormatDraft, formats: FormatStore = Depends(get_format_store),
) -> MessageFormat:
    return formats.create(draft)


@router.put("/formats/{fid}", response_model=MessageFormat, operation_id="updateFormat")
async def update_format(
    fid: str, draft: MessageFormatDraft, formats: FormatStore = Depends(get_format_store),
) -> MessageFormat:
    return formats.update(fid, draft)


@router.post("/formats/{fid}/clone", response_model=MessageFormat, status_code=201,
             operation_id="cloneFormat")
async def clone_format(
    fid: str, req: CloneFormatRequest, formats: FormatStore = Depends(get_format_store),
) -> MessageFormat:
    try:
        return formats.clone(fid, req.name, req.version)
    except KeyError:
        raise HTTPException(404, f"no message format '{fid}'")


@router.delete("/formats/{fid}", status_code=204, response_class=Response,
               operation_id="deleteFormat")
async def delete_format(fid: str, formats: FormatStore = Depends(get_format_store)) -> Response:
    try:
        formats.delete(fid)
    except KeyError:
        raise HTTPException(404, f"no message format '{fid}'")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(status_code=204)


# -- scoped variables (global / project / set / scenario) ---------------------

def get_var_store(request: Request) -> VariableStore:
    return request.app.state.variables


class VariablesBody(BaseModel):
    variables: dict[str, Any] = {}
    secrets: list[str] = []          # names whose values are secret


class ResolveVariablesRequest(BaseModel):
    project_id: str | None = None
    set_ids: list[str] = []
    variables: dict[str, Any] = {}   # scenario-scoped (for unsaved scenarios)
    secret_vars: list[str] = []      # scenario-scoped secret names


@router.get("/variables/global")
async def get_global_variables(vs: VariableStore = Depends(get_var_store)) -> dict:
    return {"variables": vs.get_global(), "secrets": vs.get_global_secrets()}


@router.put("/variables/global")
async def set_global_variables(
    body: VariablesBody, vs: VariableStore = Depends(get_var_store),
) -> dict:
    vs.set_global(body.variables, body.secrets)
    return {"variables": vs.get_global(), "secrets": vs.get_global_secrets()}


@router.get("/variables/project/{project_id}")
async def get_project_variables(
    project_id: str, vs: VariableStore = Depends(get_var_store),
) -> dict:
    return {"variables": vs.get_project(project_id), "secrets": vs.get_project_secrets(project_id)}


@router.put("/variables/project/{project_id}")
async def set_project_variables(
    project_id: str, body: VariablesBody, vs: VariableStore = Depends(get_var_store),
) -> dict:
    vs.set_project(project_id, body.variables, body.secrets)
    return {"variables": vs.get_project(project_id), "secrets": vs.get_project_secrets(project_id)}


@router.get("/variables/set/{set_id}")
async def get_set_variables(
    set_id: str, vs: VariableStore = Depends(get_var_store),
) -> dict:
    return {"variables": vs.get_set(set_id), "secrets": vs.get_set_secrets(set_id)}


@router.put("/variables/set/{set_id}")
async def set_set_variables(
    set_id: str, body: VariablesBody, vs: VariableStore = Depends(get_var_store),
) -> dict:
    vs.set_set(set_id, body.variables, body.secrets)
    return {"variables": vs.get_set(set_id), "secrets": vs.get_set_secrets(set_id)}


@router.post("/variables/resolve")
async def resolve_variables(
    req: ResolveVariablesRequest, vs: VariableStore = Depends(get_var_store),
) -> dict:
    """Merge scopes for an (often unsaved) scenario — used by the editor preview."""
    return {
        "effective": vs.effective(req.project_id, req.set_ids, req.variables),
        "breakdown": vs.breakdown(req.project_id, req.set_ids, req.variables),
        "secret_values": vs.secret_values(
            req.project_id, req.set_ids, req.variables, req.secret_vars,
        ),
    }


# -- global named tables ------------------------------------------------------

def get_table_store(request: Request) -> TableStore:
    return request.app.state.tables


@router.get("/tables", response_model=list[DataTable], operation_id="listTables")
async def list_tables(tables: TableStore = Depends(get_table_store)) -> list[DataTable]:
    return tables.list()


@router.get("/tables/runtime", operation_id="tablesRuntime")
async def tables_runtime(tables: TableStore = Depends(get_table_store)) -> dict:
    """The injectable {table_name: rows} map (used by the editor preview)."""
    return tables.runtime()


@router.get("/tables/{name}", response_model=DataTable, operation_id="getTable")
async def get_table(name: str, tables: TableStore = Depends(get_table_store)) -> DataTable:
    table = tables.get(name)
    if table is None:
        raise HTTPException(404, f"no table '{name}'")
    return table


@router.put("/tables/{name}", response_model=DataTable, operation_id="saveTable")
async def save_table(
    name: str, draft: DataTableDraft, tables: TableStore = Depends(get_table_store),
) -> DataTable:
    return tables.upsert(name, draft)


@router.delete("/tables/{name}", status_code=204, response_class=Response,
               operation_id="deleteTable")
async def delete_table(name: str, tables: TableStore = Depends(get_table_store)) -> Response:
    try:
        tables.delete(name)
    except KeyError:
        raise HTTPException(404, f"no table '{name}'")
    return Response(status_code=204)


# -- adapter connections (named, reusable target connections) -----------------

def get_connection_store(request: Request) -> ConnectionStore:
    return request.app.state.connections


@router.get("/connections", response_model=list[Connection], operation_id="listConnections")
async def list_connections(
    conns: ConnectionStore = Depends(get_connection_store),
) -> list[Connection]:
    return conns.list()


@router.get("/connections/{name}", response_model=Connection, operation_id="getConnection")
async def get_connection(
    name: str, conns: ConnectionStore = Depends(get_connection_store),
) -> Connection:
    conn = conns.get(name)
    if conn is None:
        raise HTTPException(404, f"no connection '{name}'")
    return conn


@router.put("/connections/{name}", response_model=Connection, operation_id="saveConnection")
async def save_connection(
    name: str, draft: ConnectionDraft,
    conns: ConnectionStore = Depends(get_connection_store),
) -> Connection:
    return conns.upsert(name, draft)


@router.delete("/connections/{name}", status_code=204, response_class=Response,
               operation_id="deleteConnection")
async def delete_connection(
    name: str, conns: ConnectionStore = Depends(get_connection_store),
) -> Response:
    try:
        conns.delete(name)
    except KeyError:
        raise HTTPException(404, f"no connection '{name}'")
    return Response(status_code=204)


# -- participant flows (listening, reactive stand-ins) ------------------------

def get_participant_flow_store(request: Request) -> ParticipantFlowStore:
    return request.app.state.participant_flows


@router.get("/participant-flows", response_model=list[ParticipantFlow],
            operation_id="listParticipantFlows")
async def list_participant_flows(
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> list[ParticipantFlow]:
    return flows.list()


@router.get("/participant-flows/{fid}", response_model=ParticipantFlow,
            operation_id="getParticipantFlow")
async def get_participant_flow(
    fid: str, version: int | None = None,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> ParticipantFlow:
    flow = flows.get(fid, version=version)
    if flow is None:
        detail = (f"no participant flow '{fid}'" if version is None
                  else f"no version {version} of participant flow '{fid}'")
        raise HTTPException(404, detail)
    return flow


@router.get("/participant-flows/{fid}/versions",
            response_model=list[ParticipantFlowVersionInfo],
            operation_id="listParticipantFlowVersions")
async def list_participant_flow_versions(
    fid: str, flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> list[ParticipantFlowVersionInfo]:
    rows = flows.versions(fid)
    if rows is None:
        raise HTTPException(404, f"no participant flow '{fid}'")
    return rows


@router.post("/participant-flows", response_model=ParticipantFlow, status_code=201,
             operation_id="createParticipantFlow")
async def create_participant_flow(
    draft: ParticipantFlowDraft,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> ParticipantFlow:
    return flows.create(draft)


@router.put("/participant-flows/{fid}", response_model=ParticipantFlow,
            operation_id="saveParticipantFlow")
async def save_participant_flow(
    fid: str, draft: ParticipantFlowDraft,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> ParticipantFlow:
    return flows.upsert(fid, draft)


@router.delete("/participant-flows/{fid}", status_code=204, response_class=Response,
               operation_id="deleteParticipantFlow")
async def delete_participant_flow(
    fid: str, flows: ParticipantFlowStore = Depends(get_participant_flow_store),
) -> Response:
    try:
        flows.delete(fid)
    except KeyError:
        raise HTTPException(404, f"no participant flow '{fid}'")
    return Response(status_code=204)




# -- participant groups (fleets: several connections as one participant) -------

def get_participant_group_store(request: Request) -> ParticipantGroupStore:
    return request.app.state.participant_groups


def _connection_family(conn: Connection) -> str:
    """Adapter family used to type a group: grpc / http / (tcp) iso8583 /
    header_echo. Members of a group must all share one family."""
    adapter = (conn.adapter or "tcp").lower()
    if adapter in ("grpc", "http"):
        return adapter
    return (conn.protocol or "iso8583").lower()


def _type_participant_group(
    draft: ParticipantGroupDraft, conns: ConnectionStore
) -> ParticipantGroupDraft:
    """A participant group is *typed*: every member must resolve to the same
    adapter family (you can't mix, say, an HSM and an ISO 8583 host — the same
    action/payload can't serve both). Validates and stamps ``adapter_type``."""
    families: dict[str, str] = {}
    for m in draft.members:
        conn = conns.get(m.connection)
        if conn is None:
            raise HTTPException(
                400, f"group member connection '{m.connection}' does not exist")
        families[m.connection] = _connection_family(conn)
    distinct = set(families.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{c} ({f})" for c, f in sorted(families.items()))
        raise HTTPException(
            400,
            "a participant group must contain a single adapter type — found "
            f"mixed members: {detail}",
        )
    if distinct:
        fam = next(iter(distinct))
        if draft.adapter_type and draft.adapter_type != fam:
            raise HTTPException(
                400,
                f"group adapter_type '{draft.adapter_type}' does not match its "
                f"members' type '{fam}'",
            )
        draft.adapter_type = fam
    return draft


@router.get("/participant-groups", response_model=list[ParticipantGroup],
            operation_id="listParticipantGroups")
async def list_participant_groups(
    groups: ParticipantGroupStore = Depends(get_participant_group_store),
) -> list[ParticipantGroup]:
    return groups.list()


@router.get("/participant-groups/{gid}", response_model=ParticipantGroup,
            operation_id="getParticipantGroup")
async def get_participant_group(
    gid: str, groups: ParticipantGroupStore = Depends(get_participant_group_store),
) -> ParticipantGroup:
    group = groups.get(gid)
    if group is None:
        raise HTTPException(404, f"no participant group '{gid}'")
    return group


@router.post("/participant-groups", response_model=ParticipantGroup, status_code=201,
             operation_id="createParticipantGroup")
async def create_participant_group(
    draft: ParticipantGroupDraft,
    groups: ParticipantGroupStore = Depends(get_participant_group_store),
    conns: ConnectionStore = Depends(get_connection_store),
) -> ParticipantGroup:
    return groups.create(_type_participant_group(draft, conns))


@router.put("/participant-groups/{gid}", response_model=ParticipantGroup,
            operation_id="saveParticipantGroup")
async def save_participant_group(
    gid: str, draft: ParticipantGroupDraft,
    groups: ParticipantGroupStore = Depends(get_participant_group_store),
    conns: ConnectionStore = Depends(get_connection_store),
) -> ParticipantGroup:
    return groups.upsert(gid, _type_participant_group(draft, conns))


@router.delete("/participant-groups/{gid}", status_code=204, response_class=Response,
               operation_id="deleteParticipantGroup")
async def delete_participant_group(
    gid: str, groups: ParticipantGroupStore = Depends(get_participant_group_store),
) -> Response:
    try:
        groups.delete(gid)
    except KeyError:
        raise HTTPException(404, f"no participant group '{gid}'")
    return Response(status_code=204)


# NOTE: the legacy /topologies API was removed (ADR-0004) — topologies were
# absorbed by /network-flows. The TopologyStore is still instantiated in
# ``lifespan`` solely so a pre-migration ``topologies.json`` can seed the
# network-flow registry on first start.


# -- network flows (canvas-authored composites; ADR-0004, absorbs topologies) --

def get_network_flow_store(request: Request) -> NetworkFlowStore:
    return request.app.state.network_flows


@router.get("/network-flows", response_model=list[NetworkFlow],
            operation_id="listNetworkFlows")
async def list_network_flows(
    nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> list[NetworkFlow]:
    return nets.list()


@router.post("/network-flows/validate", operation_id="validateNetworkFlow")
async def validate_network_flow_draft(
    draft: NetworkFlowDraft,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
    groups: ParticipantGroupStore = Depends(get_participant_group_store),
    store=Depends(get_store),
) -> dict:
    """Shape-level validation plus referential checks against the participant
    -flow / scenario / group registries. Saved-simulator ids live in the
    orchestrator, so ``simulator`` nodes are only shape-checked here."""
    result = validate_network_flow(draft)
    issues = list(result.issues)
    for node in draft.nodes:
        cfg = node.config or {}
        if node.kind == "participant":
            fid = str(cfg.get("flow_id", "")).strip()
            if fid and flows.get(fid) is None:
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message=f"participant flow '{fid}' does not exist"))
        elif node.kind == "scenario":
            sid = str(cfg.get("scenario_id", "")).strip()
            if sid:
                try:
                    await store.get(sid)
                except ScenarioNotFound:
                    issues.append(ValidationIssue(
                        severity="error", step_id=node.id,
                        message=f"scenario '{sid}' does not exist"))
        elif node.kind == "group":
            gid = str(cfg.get("group_id", "")).strip()
            if gid and groups.get(gid) is None:
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message=f"participant group '{gid}' does not exist"))
    return {
        "valid": not any(i.severity == "error" for i in issues),
        "issues": [i.model_dump() for i in issues],
    }


@router.post("/network-flows/infer-wiring", operation_id="inferNetworkFlowWiring")
async def infer_network_flow_wiring(
    draft: NetworkFlowDraft,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
    groups: ParticipantGroupStore = Depends(get_participant_group_store),
    conns: ConnectionStore = Depends(get_connection_store),
) -> dict:
    """Derive the wiring from what the flows already declare, so the canvas can
    auto-wire instead of asking the user to draw every edge.

    For each ``participant`` node, its flow's outbound nodes
    (action/relay/proxy) name a connection or a group:

    - a connection is resolved to (port) and matched against the *listen* port
      of another participant node's trigger connection → participant edge;
    - a group targeted by a flow yields an edge into the canvas's matching
      ``group`` node (if placed) **plus** group → member-listener edges; with
      no group node placed, direct participant → listener edges are proposed.

    Returns only edges not already in the draft (``{edges, notes}``).
    Simulator listen ports live in the orchestrator, so simulator nodes are
    never auto-wired (noted).
    """
    # listener map: trigger-connection port → participant node id
    listener_by_port: dict[int, str] = {}
    flow_docs: dict[str, Any] = {}
    for node in draft.nodes:
        if node.kind != "participant":
            continue
        fid = str((node.config or {}).get("flow_id", "")).strip()
        flow = flows.get(fid) if fid else None
        if flow is None:
            continue
        flow_docs[node.id] = flow
        conn = conns.get(flow.trigger.connection) if flow.trigger.connection else None
        port = getattr(conn, "port", None) if conn else None
        if port:
            listener_by_port[int(port)] = node.id

    # group lookup: by id AND name → (canvas node id | None, group doc)
    group_nodes: dict[str, str] = {}   # group_id → canvas node id
    for node in draft.nodes:
        if node.kind == "group":
            gid = str((node.config or {}).get("group_id", "")).strip()
            if gid:
                group_nodes[gid] = node.id
    groups_by_key: dict[str, Any] = {}
    for g in groups.list():
        groups_by_key[g.id] = g
        groups_by_key[g.name] = g

    existing = {(e.source, e.target) for e in draft.edges}
    proposed: list[dict] = []
    notes: list[str] = []

    def _propose(src: str, dst: str) -> None:
        if src != dst and (src, dst) not in existing:
            existing.add((src, dst))
            proposed.append({"source": src, "target": dst})

    def _listener_for(conn_name: str) -> str | None:
        conn = conns.get(conn_name)
        port = getattr(conn, "port", None) if conn else None
        return listener_by_port.get(int(port)) if port else None

    for node_id, flow in flow_docs.items():
        targets: list[str] = []
        for step in flow.nodes:
            if step.kind in ("action", "relay", "proxy"):
                name = step.target or str((step.config or {}).get("connection", ""))
                if name:
                    targets.append(name)
        for name in dict.fromkeys(targets):
            group = groups_by_key.get(name)
            if group is not None:
                gnode = group_nodes.get(group.id)
                member_listeners = [
                    hit for m in group.members
                    if (hit := _listener_for(m.connection)) is not None
                ]
                if gnode is not None:
                    _propose(node_id, gnode)
                    for hit in member_listeners:
                        _propose(gnode, hit)
                else:
                    for hit in member_listeners:
                        _propose(node_id, hit)
                continue
            hit = _listener_for(name)
            if hit is not None:
                _propose(node_id, hit)
            else:
                notes.append(
                    f"'{node_id}': outbound '{name}' does not resolve to a "
                    "listener on this canvas")

    if any(n.kind == "simulator" for n in draft.nodes):
        notes.append(
            "simulator nodes are not auto-wired (their listen ports live in "
            "the orchestrator) — wire them manually if a flow calls one")

    return {"edges": proposed, "notes": notes}


@router.get("/network-flows/{nid}", response_model=NetworkFlow,
            operation_id="getNetworkFlow")
async def get_network_flow(
    nid: str, nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> NetworkFlow:
    net = nets.get(nid)
    if net is None:
        raise HTTPException(404, f"no network flow '{nid}'")
    return net


@router.get("/network-flows/{nid}/plan", response_model=NetworkFlowPlan,
            operation_id="planNetworkFlow")
async def get_network_flow_plan(
    nid: str, nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> NetworkFlowPlan:
    """Resolved launch plan: participants callees-first (topological sort of
    the wiring; node list order when unwired), then initiators. The
    orchestrator consumes this to start the network."""
    net = nets.get(nid)
    if net is None:
        raise HTTPException(404, f"no network flow '{nid}'")
    return plan_network_flow(net)


@router.post("/network-flows", response_model=NetworkFlow, status_code=201,
             operation_id="createNetworkFlow")
async def create_network_flow(
    draft: NetworkFlowDraft, nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> NetworkFlow:
    _require_valid_network_flow(draft)
    return nets.create(draft)


@router.put("/network-flows/{nid}", response_model=NetworkFlow,
            operation_id="saveNetworkFlow")
async def save_network_flow(
    nid: str, draft: NetworkFlowDraft,
    nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> NetworkFlow:
    _require_valid_network_flow(draft)
    return nets.upsert(nid, draft)


@router.delete("/network-flows/{nid}", status_code=204, response_class=Response,
               operation_id="deleteNetworkFlow")
async def delete_network_flow(
    nid: str, nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> Response:
    try:
        nets.delete(nid)
    except KeyError:
        raise HTTPException(404, f"no network flow '{nid}'")
    return Response(status_code=204)


def _require_valid_network_flow(draft: NetworkFlowDraft) -> None:
    """Saves reject structural errors (drafts with warnings are fine)."""
    result = validate_network_flow(draft)
    if not result.valid:
        errors = "; ".join(
            i.message for i in result.issues if i.severity == "error")
        raise HTTPException(422, f"invalid network flow: {errors}")


class DuplicateFlowRequest(BaseModel):
    #: Name for the copy (defaults to the source flow's name).
    name: str | None = None
    #: ``True`` = rename: repoint every network flow that referenced the source
    #: at the new flow, then delete the source. ``False`` = plain duplicate
    #: (source kept).
    replace: bool = False


@router.post("/participant-flows/{fid}/duplicate", operation_id="duplicateParticipantFlow")
async def duplicate_participant_flow(
    fid: str, req: DuplicateFlowRequest,
    flows: ParticipantFlowStore = Depends(get_participant_flow_store),
    nets: NetworkFlowStore = Depends(get_network_flow_store),
) -> dict:
    """Clone a flow's graph into a NEW flow (fresh unique id) under a new name —
    the editor's Save As / rename. With ``replace`` it becomes a true rename:
    every network flow whose ``participant`` nodes point at the source is
    repointed at the copy and the source is deleted, so nothing dangles. The id
    is a stable handle, so there is no in-place rename — this is the supported
    path."""
    src = flows.get(fid)
    if src is None:
        raise HTTPException(404, f"no participant flow '{fid}'")
    data = src.model_dump(exclude={"id", "version", "updated_at"})
    data["name"] = req.name or src.name
    new = flows.create(ParticipantFlowDraft(**data))

    repointed: list[str] = []
    if req.replace and new.id != fid:
        for net in nets.list():
            hit = False
            for node in net.nodes:
                if (node.kind == "participant"
                        and (node.config or {}).get("flow_id") == fid):
                    node.config = {**node.config, "flow_id": new.id}
                    hit = True
            if hit:
                nets.upsert(net.id, NetworkFlowDraft(
                    name=net.name, description=net.description,
                    nodes=net.nodes, edges=net.edges, layout=net.layout,
                ))
                repointed.append(net.id)
        flows.delete(fid)
    return {
        "flow": new.model_dump(),
        "repointed_network_flows": repointed,
        "deleted_source": req.replace and new.id != fid,
    }


# -- test data: card pools / BIN ranges / terminal pools / keys ---------------

def get_test_data_store(request: Request) -> TestDataStore:
    return request.app.state.test_data


@router.get("/test-data/card-pools", response_model=list[CardPool],
            operation_id="listCardPools")
async def list_card_pools(td: TestDataStore = Depends(get_test_data_store)) -> list[CardPool]:
    return td.list_card_pools()


@router.get("/test-data/card-pools/{name}", response_model=CardPool,
            operation_id="getCardPool")
async def get_card_pool(name: str, td: TestDataStore = Depends(get_test_data_store)) -> CardPool:
    pool = td.get_card_pool(name)
    if pool is None:
        raise HTTPException(404, f"no card pool '{name}'")
    return pool


@router.put("/test-data/card-pools/{name}", response_model=CardPool,
            operation_id="saveCardPool")
async def save_card_pool(name: str, draft: CardPoolDraft,
                         td: TestDataStore = Depends(get_test_data_store)) -> CardPool:
    return td.upsert_card_pool(name, draft)


@router.delete("/test-data/card-pools/{name}", status_code=204, response_class=Response,
               operation_id="deleteCardPool")
async def delete_card_pool(name: str, td: TestDataStore = Depends(get_test_data_store)) -> Response:
    try:
        td.delete_card_pool(name)
    except KeyError:
        raise HTTPException(404, f"no card pool '{name}'")
    return Response(status_code=204)


@router.get("/test-data/bin-ranges", response_model=list[BinRange],
            operation_id="listBinRanges")
async def list_bin_ranges(td: TestDataStore = Depends(get_test_data_store)) -> list[BinRange]:
    return td.list_bin_ranges()


@router.get("/test-data/bin-ranges/{name}", response_model=BinRange,
            operation_id="getBinRange")
async def get_bin_range(name: str, td: TestDataStore = Depends(get_test_data_store)) -> BinRange:
    br = td.get_bin_range(name)
    if br is None:
        raise HTTPException(404, f"no BIN range '{name}'")
    return br


@router.put("/test-data/bin-ranges/{name}", response_model=BinRange,
            operation_id="saveBinRange")
async def save_bin_range(name: str, draft: BinRangeDraft,
                         td: TestDataStore = Depends(get_test_data_store)) -> BinRange:
    return td.upsert_bin_range(name, draft)


@router.delete("/test-data/bin-ranges/{name}", status_code=204, response_class=Response,
               operation_id="deleteBinRange")
async def delete_bin_range(name: str, td: TestDataStore = Depends(get_test_data_store)) -> Response:
    try:
        td.delete_bin_range(name)
    except KeyError:
        raise HTTPException(404, f"no BIN range '{name}'")
    return Response(status_code=204)


@router.post("/test-data/bin-ranges/{name}/generate", operation_id="generateBinPans")
async def generate_bin_pans(
    name: str, count: int = 10, as_pool: str | None = None, seed: str | None = None,
    td: TestDataStore = Depends(get_test_data_store),
) -> dict:
    if count < 1 or count > 10000:
        raise HTTPException(422, "count must be between 1 and 10000")
    try:
        pans = td.generate_pans(name, count, seed=seed, as_pool=as_pool)
    except KeyError:
        raise HTTPException(404, f"no BIN range '{name}'")
    return {"pans": pans, "saved_pool": as_pool}


@router.get("/test-data/terminal-pools", response_model=list[TerminalPool],
            operation_id="listTerminalPools")
async def list_terminal_pools(td: TestDataStore = Depends(get_test_data_store)) -> list[TerminalPool]:
    return td.list_terminal_pools()


@router.get("/test-data/terminal-pools/{name}", response_model=TerminalPool,
            operation_id="getTerminalPool")
async def get_terminal_pool(name: str, td: TestDataStore = Depends(get_test_data_store)) -> TerminalPool:
    pool = td.get_terminal_pool(name)
    if pool is None:
        raise HTTPException(404, f"no terminal pool '{name}'")
    return pool


@router.put("/test-data/terminal-pools/{name}", response_model=TerminalPool,
            operation_id="saveTerminalPool")
async def save_terminal_pool(name: str, draft: TerminalPoolDraft,
                             td: TestDataStore = Depends(get_test_data_store)) -> TerminalPool:
    return td.upsert_terminal_pool(name, draft)


@router.delete("/test-data/terminal-pools/{name}", status_code=204, response_class=Response,
               operation_id="deleteTerminalPool")
async def delete_terminal_pool(name: str, td: TestDataStore = Depends(get_test_data_store)) -> Response:
    try:
        td.delete_terminal_pool(name)
    except KeyError:
        raise HTTPException(404, f"no terminal pool '{name}'")
    return Response(status_code=204)


@router.get("/test-data/keys", response_model=list[KeyView], operation_id="listTestKeys")
async def list_test_keys(td: TestDataStore = Depends(get_test_data_store)) -> list[KeyView]:
    return td.list_keys()


@router.get("/test-data/keys/{name}", response_model=KeyView, operation_id="getTestKey")
async def get_test_key(name: str, td: TestDataStore = Depends(get_test_data_store)) -> KeyView:
    view = td.get_key_view(name)
    if view is None:
        raise HTTPException(404, f"no key '{name}'")
    return view


@router.put("/test-data/keys/{name}", response_model=KeyView, operation_id="saveTestKey")
async def save_test_key(name: str, draft: KeyDraft,
                        td: TestDataStore = Depends(get_test_data_store)) -> KeyView:
    return td.upsert_key(name, draft)


@router.delete("/test-data/keys/{name}", status_code=204, response_class=Response,
               operation_id="deleteTestKey")
async def delete_test_key(name: str, td: TestDataStore = Depends(get_test_data_store)) -> Response:
    try:
        td.delete_key(name)
    except KeyError:
        raise HTTPException(404, f"no key '{name}'")
    return Response(status_code=204)


def _require_service_caller(request: Request) -> None:
    """Key material may only leave over service-to-service calls.

    The auth gate stashes verified claims on ``request.state.auth``: a static
    bearer yields ``{"static": True}``, a minted service JWT carries ``svc``,
    and open dev mode yields ``{"dev": True}`` — a *user* token (auth-service
    issued, sub = username) carries none of those, so the browser can never
    pull material even with a valid login. This keeps the vault invariant
    ("reads mask") intact for people while enabling the internal resolver the
    store was designed for (``${key.NAME}`` in scenarios, resolved by the
    orchestrator at run build).
    """
    claims = getattr(request.state, "auth", None) or {}
    if claims.get("dev") or claims.get("static") or claims.get("svc"):
        return
    raise HTTPException(
        status_code=403,
        detail="key material is service-to-service only — the API masks key "
               "reads for user credentials",
    )


@router.get("/test-data/keys/{name}/material", include_in_schema=False,
            operation_id="getTestKeyMaterial")
async def get_test_key_material(
    name: str, request: Request, td: TestDataStore = Depends(get_test_data_store)
) -> dict:
    """Plaintext key material for the orchestrator's ``${key.NAME}`` resolver.
    Service credentials only (403 for user tokens); hidden from the schema so
    it never shows up in /reference or generated clients."""
    _require_service_caller(request)
    value = td.get_key_value(name)
    if value is None:
        raise HTTPException(404, f"no key '{name}'")
    return {"name": name, "value": value}


# -- secrets vault: masked inventory across all secret stores -----------------

@router.get("/secrets", operation_id="listSecrets")
async def list_secrets(request: Request) -> dict:
    """A masked inventory of every secret PayProbe holds + encryption status.

    Aggregates secret references from connections, scoped variables,
    test-data keys, NATS cluster auth and the assistant's LLM key. **Never
    returns plaintext** — only owner/field/fingerprint.
    ``encrypted_at_rest`` reflects whether ``SecretBox`` has a key configured; a
    ``false`` means those secrets sit on disk as plaintext.
    """
    from .crypto import default_box

    app = request.app
    enabled = default_box.enabled
    entries: list[dict] = []
    for ref in app.state.connections.secret_refs():
        entries.append({"source": "connection", "owner": ref["owner"],
                        "field": ref["field"], "fingerprint": ref["fingerprint"],
                        "encrypted": enabled})
    for ref in app.state.variables.secret_refs():
        owner = ref["scope"] if not ref["scope_id"] else f"{ref['scope']}:{ref['scope_id']}"
        entries.append({"source": "variable", "owner": owner, "field": ref["name"],
                        "fingerprint": ref["fingerprint"], "encrypted": enabled})
    for view in app.state.test_data.list_keys():
        entries.append({"source": "key", "owner": view.name, "field": "(material)",
                        "fingerprint": view.fingerprint, "encrypted": enabled})
    for ref in app.state.nats_servers.secret_refs():
        entries.append({"source": "nats", "owner": ref["owner"],
                        "field": ref["field"], "fingerprint": ref["fingerprint"],
                        "encrypted": enabled})
    for ref in app.state.assist.secret_refs():
        entries.append({"source": "assistant", "owner": ref["owner"],
                        "field": ref["field"], "fingerprint": ref["fingerprint"],
                        "encrypted": enabled})
    return {
        "status": {"encrypted_at_rest": enabled, "algorithm": "fernet" if enabled else None},
        "entries": entries,
    }


# -- environments: named adapter-target profiles (dev/staging/prod) -----------

def get_environment_store(request: Request) -> EnvironmentStore:
    return request.app.state.environments


@router.get("/environments", response_model=list[Environment], operation_id="listEnvironments")
async def list_environments(
    envs: EnvironmentStore = Depends(get_environment_store),
) -> list[Environment]:
    return envs.list()


@router.get("/environments/{name}", response_model=Environment, operation_id="getEnvironment")
async def get_environment(
    name: str, envs: EnvironmentStore = Depends(get_environment_store),
) -> Environment:
    env = envs.get(name)
    if env is None:
        raise HTTPException(404, f"no environment '{name}'")
    return env


@router.put("/environments/{name}", response_model=Environment, operation_id="saveEnvironment")
async def save_environment(
    name: str, draft: EnvironmentDraft,
    envs: EnvironmentStore = Depends(get_environment_store),
) -> Environment:
    return envs.upsert(name, draft)


@router.delete("/environments/{name}", status_code=204, response_class=Response,
               operation_id="deleteEnvironment")
async def delete_environment(
    name: str, envs: EnvironmentStore = Depends(get_environment_store),
) -> Response:
    try:
        envs.delete(name)
    except KeyError:
        raise HTTPException(404, f"no environment '{name}'")
    return Response(status_code=204)


# -- NATS servers: named broker clusters (ADR-0006) ---------------------------

def get_nats_server_store(request: Request) -> NatsServerStore:
    return request.app.state.nats_servers


@router.get("/nats-servers", response_model=list[NatsServer], operation_id="listNatsServers")
async def list_nats_servers(
    store: NatsServerStore = Depends(get_nats_server_store),
) -> list[NatsServer]:
    return store.list()


@router.get("/nats-servers/{name}", response_model=NatsServer, operation_id="getNatsServer")
async def get_nats_server(
    name: str, store: NatsServerStore = Depends(get_nats_server_store),
) -> NatsServer:
    srv = store.get(name)
    if srv is None:
        raise HTTPException(404, f"no nats server '{name}'")
    return srv


@router.put("/nats-servers/{name}", response_model=NatsServer, operation_id="saveNatsServer")
async def save_nats_server(
    name: str, draft: NatsServerDraft,
    store: NatsServerStore = Depends(get_nats_server_store),
) -> NatsServer:
    return store.upsert(name, draft)


@router.delete("/nats-servers/{name}", status_code=204, response_class=Response,
               operation_id="deleteNatsServer")
async def delete_nats_server(
    name: str, store: NatsServerStore = Depends(get_nats_server_store),
) -> Response:
    try:
        store.delete(name)
    except KeyError:
        raise HTTPException(404, f"no nats server '{name}'")
    return Response(status_code=204)


@router.post("/admin/migrate/connection-overrides", operation_id="migrateConnectionOverrides")
async def migrate_connection_overrides(
    apply: bool = False,
    conns: ConnectionStore = Depends(get_connection_store),
    envs: EnvironmentStore = Depends(get_environment_store),
) -> dict:
    """Phase 1 backfill: move per-environment values out of
    ``environments[].adapters`` into each connection's ``environment_overrides``.

    Dry-run by default (``apply=false``) — returns the plan it *would* write so it
    can be reviewed. ``apply=true`` writes the overrides into the connection store.
    Additive and idempotent: the environment store is never modified, and a second
    run produces an empty plan. See ``docs/history/CONNECTION-ENV-MIGRATION-PLAN.md``.
    """
    environments = [(e.key, e.adapters or {}) for e in envs.list()]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    plan = plan_backfill(environments, connections)
    written = apply_backfill(plan, conns) if apply else 0
    return {"applied": apply, "connections_written": written, **plan.summary()}


@router.get("/admin/migrate/collisions", operation_id="migrationCollisions")
async def migration_collisions(
    conns: ConnectionStore = Depends(get_connection_store),
    envs: EnvironmentStore = Depends(get_environment_store),
) -> dict:
    """Phase 3 safety gate: list every connection-backed env adapter whose value
    would CHANGE when precedence flips (Phase 4). ``safe_to_flip`` is true only
    when there are none — i.e. the backfill is complete and the flip preserves
    behaviour. See ``docs/history/CONNECTION-ENV-MIGRATION-PLAN.md``.
    """
    environments = [(e.key, e.adapters or {}) for e in envs.list()]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    collisions = find_collisions(environments, connections)
    return {
        "safe_to_flip": not collisions,
        "count": len(collisions),
        "collisions": [c.as_dict() for c in collisions],
    }


@router.post("/admin/migrate/slim-environments", operation_id="migrateSlimEnvironments")
async def migrate_slim_environments(
    apply: bool = False,
    conns: ConnectionStore = Depends(get_connection_store),
    envs: EnvironmentStore = Depends(get_environment_store),
    catalog: CatalogStore = Depends(get_catalog_store),
) -> dict:
    """Remove env adapter entries a connection now owns (collision-clean). Covers
    both name-matched connections and — for catalog-target adapters whose type has
    a **default connection** — the default-connection model (Phase E). Dry-run by
    default; entries that still differ are reported under ``blocked``. Run only
    after the backfill/flip (and, for the default case, after seeding defaults +
    enabling default resolution). See the migration / default-connection specs.
    """
    environments = [(e.key, e.adapters or {}) for e in envs.list()]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    catalog_targets = {t.target for t in catalog.merged()}
    plan = plan_slim(environments, connections, catalog_targets)
    changed = apply_slim(plan, envs) if apply else 0
    return {"applied": apply, "environments_changed": changed, **plan.summary()}


@router.post("/admin/migrate/seed-default-connections", operation_id="migrateSeedDefaults")
async def migrate_seed_default_connections(
    apply: bool = False,
    conns: ConnectionStore = Depends(get_connection_store),
    envs: EnvironmentStore = Depends(get_environment_store),
    catalog: CatalogStore = Depends(get_catalog_store),
) -> dict:
    """Default-connection model, Phase B: create one default connection per adapter
    type from each environment's catalog-target-named adapters (folding per-env
    differences into the matrix). Dry-run by default. Skips a type that already has
    a default; reports adapters that can't be a connection under ``failed``. See
    ``docs/history/DEFAULT-CONNECTION-MODEL-SPEC.md``.
    """
    environments = [(e.key, e.adapters or {}) for e in envs.list()]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    catalog_targets = {t.target for t in catalog.merged()}
    plan = plan_seed_defaults(environments, connections, catalog_targets)
    created: list[str] = []
    failed: list[dict] = []
    if apply:
        created, failed = apply_seed_defaults(plan, conns)
    return {"applied": apply, "created": created, "failed": failed, **plan.summary()}


# -- ISO 8583 inspector: analyze a wire message / validate-and-build ----------

#: wire encoding: "ascii" (default), "binary", or a {bitmap,numeric,text,binary,
#: length} dict for fine control. Binary messages are exchanged as hex strings.
Iso8583Encoding = str | dict | None


class Iso8583AnalyzeRequest(BaseModel):
    message: str
    #: optional DE table ({de: {name,len_type,length,type}}); defaults to 1987
    fields: dict[str, dict] | None = None
    encoding: Iso8583Encoding = None


class Iso8583BuildRequest(BaseModel):
    mti: str
    values: dict[str, Any] = {}
    fields: dict[str, dict] | None = None
    encoding: Iso8583Encoding = None


@router.post("/iso8583/analyze", operation_id="analyzeIso8583")
async def iso8583_analyze(req: Iso8583AnalyzeRequest) -> dict:
    """Decode a wire message into MTI + bitmap + validated DEs (+ EMV TLV)."""
    return iso8583_analyze_message(
        req.message, req.fields or ISO8583_DEFAULT_FIELDS, req.encoding)


@router.post("/iso8583/build", operation_id="buildIso8583")
async def iso8583_build(req: Iso8583BuildRequest) -> dict:
    """Validate a {DE: value} map against the field table, then pack it."""
    return iso8583_build_message(
        req.mti, req.values, req.fields or ISO8583_DEFAULT_FIELDS, req.encoding)


class Iso8583DiffRequest(BaseModel):
    a: str
    b: str
    fields: dict[str, dict] | None = None
    encoding: Iso8583Encoding = None


class Iso8583TlvBuildRequest(BaseModel):
    #: nodes: [{tag, value(hex)} | {tag, children:[...]}]
    nodes: list[dict]


class AssistRequest(BaseModel):
    prompt: str
    #: current scenario steps ({id,target,action}) — enables edit/extend mode
    scenario: list[dict] = []


def _resolve_llm(request: Request) -> dict:
    """LLM settings: the saved config (provider defaults filled) wins, else env."""
    cfg = request.app.state.assist.raw()   # provider/base_url/model already defaulted
    env_key = os.environ.get("ASSIST_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
    api_key = (cfg.get("api_key") if cfg.get("enabled") else None) or env_key or ""
    return {
        "provider": cfg.get("provider", "openai"),
        "enabled": bool(api_key) and (cfg.get("enabled") or bool(env_key)),
        "api_key": api_key,
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
    }


@router.post("/scenarios/assist", operation_id="assistScenario")
async def assist_scenario(req: AssistRequest, request: Request) -> dict:
    """Turn a natural-language prompt into an insertable Starter-Flow chain.

    When ``scenario`` (the current steps) is supplied and the prompt reads like
    an edit ("add a settlement check"), new steps are wired to the existing
    nodes instead of starting fresh.
    """
    from .assist import assist as _assist
    return _assist(req.prompt, req.scenario, llm=_resolve_llm(request))


@router.get("/assist/config", operation_id="getAssistConfig")
async def get_assist_config(request: Request) -> dict:
    """AI-assistant LLM settings (API key never returned — only a masked hint)."""
    return request.app.state.assist.public()


@router.put("/assist/config", operation_id="saveAssistConfig")
async def save_assist_config(draft: AssistConfigDraft, request: Request) -> dict:
    return request.app.state.assist.save(draft)


@router.post("/assist/test", operation_id="testAssistConfig")
async def test_assist_config(request: Request) -> dict:
    """Make a real call to the configured LLM and report success/failure.

    Tests the saved key even when the provider toggle is off, so you can verify
    credentials before enabling it.
    """
    from .assist import test_llm
    cfg = request.app.state.assist.raw()
    env_key = os.environ.get("ASSIST_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
    llm = {
        "provider": cfg.get("provider", "openai"),
        "enabled": bool(cfg.get("enabled")),
        "api_key": cfg.get("api_key") or env_key or "",
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
    }
    return test_llm(llm)


@router.get("/assist/config/material", include_in_schema=False,
            operation_id="getAssistConfigMaterial")
async def get_assist_config_material(request: Request) -> dict:
    """Full LLM settings *including the API key*, for the standalone
    payprobe-assistant (the LLM egress boundary): when it has no
    ``ASSIST_LLM_*`` env of its own it pulls the Settings-stored config from
    here, so the key is managed in ONE place (Settings → AI assistant).
    Service credentials only — user tokens get a 403 and keep seeing the
    masked view (same rule as ``/test-data/keys/{name}/material``); hidden
    from the schema so it never shows in /reference or generated clients."""
    _require_service_caller(request)
    return _resolve_llm(request)


# -- general config assistant (conversational, tool-calling agent) -------------
#
# DEPRECATED SHIM (2026-07-07): the portal now talks to the standalone
# payprobe-assistant (:8400 / nginx /api/assistant — ATLAS roadmap #5). These
# /agent/* routes stay DELIBERATELY (decision recorded in ATLAS roadmap #5):
# the agent test suites (test_agent_loop/session/scenarios) drive the shared
# toolkit, the sync→async scenario-store bridge, and session persistence
# through them against the in-process StoresBackend — the parity proof for
# the two-backend architecture. A Settings → Endpoints override can also
# still fall back here. Don't add features here; the standalone service is
# the live surface. Delete only if the StoresBackend path itself is dropped.

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AgentChatRequest(BaseModel):
    messages: list[ChatMessage]
    #: "full" exposes read+write tools (autonomous edits); "advisor" is read-only.
    mode: Literal["full", "advisor"] = "full"
    #: continue an existing session so its change journal accumulates for revert;
    #: omitted on the first turn — the server mints one and returns it.
    session_id: str | None = None


class AgentRevertRequest(BaseModel):
    session_id: str


async def _referenced_connection_names(store, conns) -> set[str]:
    """Connection names a scenario currently references (best-effort), so the
    delete guardrail can protect in-use connections. Matches the quoted name
    anywhere in the serialized scenario (target, config.connection, …)."""
    names = {c.name for c in conns.list()}
    if not names:
        return set()
    referenced: set[str] = set()
    try:
        for summ in await store.list():
            sc = await store.get(summ.id)
            blob = json.dumps(sc.model_dump(), default=str)
            for n in names:
                if f'"{n}"' in blob:
                    referenced.add(n)
    except Exception:  # never let the guardrail scan break a chat turn
        pass
    return referenced


@router.post("/agent/chat", operation_id="agentChat")
async def agent_chat(req: AgentChatRequest, request: Request) -> dict:
    """Multi-turn config assistant. The model reads and (in ``full`` mode) writes
    config by calling tools; every write is journalled and reversible. Returns
    the reply, the tool calls executed, and the change-journal entries."""
    from fastapi.concurrency import run_in_threadpool

    from .agent import llm_ready, not_configured_reply, run_agent
    from .agent_tools import ChangeJournal, ToolContext

    import asyncio

    llm = _resolve_llm(request)
    if not llm_ready(llm):
        return not_configured_reply()
    app = request.app
    referenced = await _referenced_connection_names(app.state.store, app.state.connections)
    # bridge sync tool handlers to the async scenario store on this event loop
    loop = asyncio.get_running_loop()
    run_async = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result()
    ctx = ToolContext(stores=app.state, journal=ChangeJournal(),
                      referenced_connections=referenced, run_async=run_async)
    tiers = ("read",) if req.mode == "advisor" else ("read", "write", "execute")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    # the loop does blocking provider HTTP — keep the event loop free
    result = await run_in_threadpool(run_agent, msgs, ctx, llm, tiers=tiers)
    # persist this turn's reversible writes under a session so revert works later
    session_id = req.session_id or uuid.uuid4().hex
    await app.state.assist_sessions.append(session_id, ctx.journal.dump())
    result["session_id"] = session_id
    return result


@router.post("/agent/chat/stream", operation_id="agentChatStream")
async def agent_chat_stream(req: AgentChatRequest, request: Request) -> StreamingResponse:
    """Streaming variant of /agent/chat. Emits Server-Sent Events: a ``tool``
    event per tool step (live status) then a single ``final`` event with the
    reply, tool calls, journal and session id. Falls through to a ``final`` with
    ``needs_config`` when no LLM is set up."""
    import asyncio
    import threading

    from .agent import iter_agent, llm_ready, not_configured_reply
    from .agent_tools import ChangeJournal, ToolContext

    llm = _resolve_llm(request)
    app = request.app
    loop = asyncio.get_running_loop()

    async def gen():
        if not llm_ready(llm):
            yield f'data: {json.dumps({"type": "final", **not_configured_reply()})}\n\n'
            return
        referenced = await _referenced_connection_names(
            app.state.store, app.state.connections)
        run_async = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result()
        ctx = ToolContext(stores=app.state, journal=ChangeJournal(),
                          referenced_connections=referenced, run_async=run_async)
        tiers = ("read",) if req.mode == "advisor" else ("read", "write", "execute")
        msgs = [{"role": m.role, "content": m.content} for m in req.messages]
        q: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for ev in iter_agent(msgs, ctx, llm, tiers=tiers):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        session_id = req.session_id or uuid.uuid4().hex
        while True:
            ev = await q.get()
            if ev is None:
                break
            if ev.get("type") == "final":
                await app.state.assist_sessions.append(session_id, ctx.journal.dump())
                ev["session_id"] = session_id
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent/revert", operation_id="agentRevert")
async def agent_revert(req: AgentRevertRequest, request: Request) -> dict:
    """Undo every change the assistant made in a session, newest first."""
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    from .agent_tools import ToolContext, restore_journal

    app = request.app
    records = await app.state.assist_sessions.get(req.session_id)
    if not records:
        return {"reverted": 0, "session_id": req.session_id}
    loop = asyncio.get_running_loop()
    run_async = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result()
    ctx = ToolContext(stores=app.state, run_async=run_async)
    n = await run_in_threadpool(restore_journal, ctx, records)
    await app.state.assist_sessions.clear(req.session_id)
    return {"reverted": n, "session_id": req.session_id}


@router.post("/iso8583/diff", operation_id="diffIso8583")
async def iso8583_diff(req: Iso8583DiffRequest) -> dict:
    """Field-level diff of two ISO 8583 messages (added / removed / changed)."""
    return iso8583_diff_messages(
        req.a, req.b, req.fields or ISO8583_DEFAULT_FIELDS, req.encoding)


@router.post("/iso8583/tlv/build", operation_id="buildTlv")
async def iso8583_tlv_build(req: Iso8583TlvBuildRequest) -> dict:
    """Encode tag/value nodes into a BER-TLV hex string (e.g. for DE 55)."""
    try:
        return {"hex": iso8583_build_tlv(req.nodes)}
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, f"invalid TLV node: {exc}")


# -- test-case packs (curated, installable regression suites) -----------------

class InstallPackResult(BaseModel):
    pack: str
    project_id: str
    project_name: str
    imported: int
    scenario_names: list[str]


@router.get("/packs", operation_id="listPacks")
async def list_packs_route() -> list[dict]:
    """Available packs (summary: scheme, label, case count)."""
    return [
        {"id": p.id, "scheme": p.scheme, "label": p.label,
         "description": p.description, "version": p.version, "cases": len(p.cases)}
        for p in list_packs()
    ]


@router.get("/packs/{pack_id}", response_model=Pack, operation_id="getPack")
async def get_pack_route(pack_id: str) -> Pack:
    pack = get_pack(pack_id)
    if pack is None:
        raise HTTPException(404, f"no pack '{pack_id}'")
    return pack


@router.post("/packs/{pack_id}/install", response_model=InstallPackResult,
             operation_id="installPack")
async def install_pack(pack_id: str, store=Depends(get_store)) -> InstallPackResult:
    """Import a pack's scenarios into a new project so they run as a suite."""
    pack = get_pack(pack_id)
    if pack is None:
        raise HTTPException(404, f"no pack '{pack_id}'")
    from models import ProjectDraft, ScenarioDraft
    project = await store.create_project(
        ProjectDraft(name=f"{pack.label} ({pack.id})", description=pack.description))
    names: list[str] = []
    for case in pack.cases:
        doc = {k: v for k, v in case.scenario.items() if k != "id"}
        draft = ScenarioDraft(**doc)
        await store.create(draft, project_id=project.id, comment=f"from pack {pack.id}")
        names.append(draft.name)
    return InstallPackResult(pack=pack.id, project_id=project.id,
                             project_name=project.name, imported=len(names),
                             scenario_names=names)


# -- starter flows (one-click pre-wired chains for the editor palette) ---------

def get_flow_store(request: Request) -> StarterFlowStore:
    return request.app.state.flows


@router.get("/starter-flows", response_model=list[StarterFlow], operation_id="listStarterFlows")
async def list_starter_flows(
    flows: StarterFlowStore = Depends(get_flow_store),
) -> list[StarterFlow]:
    return flows.list()


@router.get("/starter-flows/{fid}", response_model=StarterFlow, operation_id="getStarterFlow")
async def get_starter_flow(
    fid: str, flows: StarterFlowStore = Depends(get_flow_store),
) -> StarterFlow:
    flow = flows.get(fid)
    if flow is None:
        raise HTTPException(404, f"no starter flow '{fid}'")
    return flow


@router.post("/starter-flows", response_model=StarterFlow, status_code=201,
             operation_id="createStarterFlow")
async def create_starter_flow(
    draft: StarterFlowDraft, flows: StarterFlowStore = Depends(get_flow_store),
) -> StarterFlow:
    return flows.create(draft)


@router.put("/starter-flows/{fid}", response_model=StarterFlow, operation_id="updateStarterFlow")
async def update_starter_flow(
    fid: str, draft: StarterFlowDraft, flows: StarterFlowStore = Depends(get_flow_store),
) -> StarterFlow:
    return flows.update(fid, draft)


@router.delete("/starter-flows/{fid}", status_code=204, response_class=Response,
               operation_id="deleteStarterFlow")
async def delete_starter_flow(
    fid: str, flows: StarterFlowStore = Depends(get_flow_store),
) -> Response:
    try:
        flows.delete(fid)
    except KeyError:
        raise HTTPException(404, f"no starter flow '{fid}'")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(status_code=204)


@router.get("/scenarios/{scenario_id}/variables", operation_id="scenarioVariables")
async def scenario_variables(
    scenario_id: str,
    store=Depends(get_store),
    vs: VariableStore = Depends(get_var_store),
) -> dict:
    """Effective variables for a stored scenario (merged across all scopes)."""
    try:
        sc = await store.get(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(404, f"scenario '{scenario_id}' not found")
    sets = await store.list_sets(sc.project_id)
    set_ids = [s.id for s in sets if scenario_id in s.scenario_ids]
    return {
        "effective": vs.effective(sc.project_id, set_ids, sc.variables),
        "breakdown": vs.breakdown(sc.project_id, set_ids, sc.variables),
        "secret_values": vs.secret_values(
            sc.project_id, set_ids, sc.variables, sc.secret_vars,
        ),
        "set_ids": set_ids,
    }


@router.get("/scenarios", response_model=list[ScenarioSummary], operation_id="listScenarios")
async def list_scenarios(
    project_id: str | None = None,
    set_id: str | None = None,
    test_class: TestClass | None = None,
    q: str | None = None,
    store=Depends(get_store),
) -> list[ScenarioSummary]:
    """List scenarios, optionally narrowed by project, named set, class, or text."""
    summaries = await store.list(project_id)
    if set_id is not None:
        try:
            members = set((await store.get_set(set_id)).scenario_ids)
        except SetNotFound:
            raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")
        summaries = [s for s in summaries if s.id in members]
    if test_class is not None:
        summaries = [s for s in summaries if s.test_class == test_class]
    if q:
        needle = q.lower()
        summaries = [
            s
            for s in summaries
            if needle in s.name.lower()
            or needle in s.description.lower()
            or any(needle in t.lower() for t in s.tags)
        ]
    return summaries


@router.post(
    "/scenarios", response_model=Scenario, status_code=201, operation_id="createScenario"
)
async def create_scenario(req: SaveRequest, store=Depends(get_store)) -> Scenario:
    result = validate_scenario(req.scenario)
    if not result.valid:
        raise HTTPException(status_code=422, detail=result.model_dump()["issues"])
    try:
        return await store.create(
            req.scenario, comment=req.comment, project_id=req.project_id
        )
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project '{exc.args[0]}' not found")


@router.get("/scenarios/{scenario_id}", response_model=Scenario, operation_id="getScenario")
async def get_scenario(scenario_id: str, store=Depends(get_store)) -> Scenario:
    try:
        return await store.get(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")


@router.put("/scenarios/{scenario_id}", response_model=Scenario, operation_id="updateScenario")
async def update_scenario(
    scenario_id: str, req: SaveRequest, store=Depends(get_store)
) -> Scenario:
    result = validate_scenario(req.scenario)
    if not result.valid:
        raise HTTPException(status_code=422, detail=result.model_dump()["issues"])
    try:
        scenario = await store.update(
            scenario_id, req.scenario, comment=req.comment, base_version=req.base_version
        )
        if req.project_id is not None and req.project_id != scenario.project_id:
            try:
                await store.move_scenario(scenario_id, req.project_id)
            except ProjectNotFound:
                raise HTTPException(
                    status_code=404, detail=f"project '{req.project_id}' not found"
                )
            scenario = await store.get(scenario_id)
        return scenario
    except VersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "scenario was modified by someone else",
                "current_version": exc.current_version,
            },
        )
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")


@router.delete(
    "/scenarios/{scenario_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteScenario",
)
async def delete_scenario(scenario_id: str, store=Depends(get_store)):
    try:
        await store.delete(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")


@router.get(
    "/scenarios/{scenario_id}/versions",
    response_model=list[ScenarioVersionInfo],
    operation_id="listVersions",
)
async def list_versions(
    scenario_id: str, store=Depends(get_store)
) -> list[ScenarioVersionInfo]:
    try:
        return await store.versions(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")


@router.get(
    "/scenarios/{scenario_id}/versions/{version}",
    response_model=Scenario,
    operation_id="getVersion",
)
async def get_version(scenario_id: str, version: int, store=Depends(get_store)) -> Scenario:
    try:
        return await store.get(scenario_id, version)
    except ScenarioNotFound:
        raise HTTPException(
            status_code=404, detail=f"scenario '{scenario_id}' v{version} not found"
        )


@router.post(
    "/scenarios/{scenario_id}/versions/{version}/restore",
    response_model=Scenario,
    operation_id="restoreVersion",
)
async def restore_version(
    scenario_id: str, version: int, store=Depends(get_store)
) -> Scenario:
    try:
        return await store.restore(scenario_id, version)
    except ScenarioNotFound:
        raise HTTPException(
            status_code=404, detail=f"scenario '{scenario_id}' v{version} not found"
        )


@router.post("/validate", response_model=ValidationResult, operation_id="validateScenario")
async def validate(draft: ScenarioDraft = Body(...)) -> ValidationResult:
    return validate_scenario(draft)


# -- import / export ---------------------------------------------------------

#: Server-managed fields stripped on import so exported files re-import cleanly.
_SERVER_FIELDS = ("id", "version", "created_at", "updated_at")

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, ext: str) -> str:
    stem = _FILENAME_SAFE_RE.sub("_", name).strip("._") or "scenario"
    return f"{stem}.{ext}"


@router.get("/scenarios/{scenario_id}/export", operation_id="exportScenario")
async def export_scenario(
    scenario_id: str,
    format: Literal["json", "yaml"] = "yaml",
    store=Depends(get_store),
) -> Response:
    """Download a scenario as a portable document (no server metadata)."""
    try:
        scenario = await store.get(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")
    doc = ScenarioDraft(**scenario.model_dump()).model_dump()
    if format == "yaml":
        body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        media_type = "application/yaml"
    else:
        body = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        media_type = "application/json"
    filename = _safe_filename(scenario.name, format)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/scenarios/import",
    response_model=Scenario,
    status_code=201,
    operation_id="importScenario",
)
async def import_scenario(
    request: Request, project_id: str | None = None, store=Depends(get_store)
) -> Scenario:
    """Create a scenario from a raw JSON or YAML document (request body).

    Accepts the files produced by the export endpoint as well as hand-written
    documents. Server-managed fields are ignored; a fresh id is assigned.
    ``?project_id=`` places the imported scenario in that project.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not raw.strip():
        raise HTTPException(status_code=422, detail="request body is empty")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=422, detail=f"body is neither valid JSON nor YAML: {exc}"
            )
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="document must be a JSON/YAML object")
    for field in _SERVER_FIELDS:
        data.pop(field, None)
    try:
        draft = ScenarioDraft(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid scenario document: {exc}")
    result = validate_scenario(draft)
    if not result.valid:
        raise HTTPException(status_code=422, detail=result.model_dump()["issues"])
    try:
        return await store.create(draft, comment="imported", project_id=project_id)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project '{exc.args[0]}' not found")


class MoveRequest(BaseModel):
    project_id: str


@router.post(
    "/scenarios/{scenario_id}/move",
    response_model=Scenario,
    operation_id="moveScenario",
)
async def move_scenario(
    scenario_id: str, req: MoveRequest, store=Depends(get_store)
) -> Scenario:
    """Move a scenario to another project (no new version is created).

    Memberships in the old project's sets are dropped.
    """
    try:
        await store.move_scenario(scenario_id, req.project_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")
    except ProjectNotFound:
        raise HTTPException(
            status_code=404, detail=f"project '{req.project_id}' not found"
        )
    return await store.get(scenario_id)


@router.post(
    "/scenarios/{scenario_id}/duplicate",
    response_model=Scenario,
    status_code=201,
    operation_id="duplicateScenario",
)
async def duplicate_scenario(scenario_id: str, store=Depends(get_store)) -> Scenario:
    """Copy a scenario within its project, including its set memberships."""
    try:
        original = await store.get(scenario_id)
    except ScenarioNotFound:
        raise HTTPException(status_code=404, detail=f"scenario '{scenario_id}' not found")
    draft = ScenarioDraft(**original.model_dump())
    draft.name = f"{original.name} (copy)"[:200]
    copy = await store.create(
        draft,
        comment=f"duplicated from {original.name}",
        project_id=original.project_id,
    )
    for s in await store.list_sets(original.project_id):
        if scenario_id in s.scenario_ids:
            await store.add_to_set(s.id, copy.id)
    return copy


# -- projects -----------------------------------------------------------------


@router.get("/projects", response_model=list[Project], operation_id="listProjects")
async def list_projects(store=Depends(get_store)) -> list[Project]:
    return await store.list_projects()


@router.post(
    "/projects", response_model=Project, status_code=201, operation_id="createProject"
)
async def create_project(draft: ProjectDraft, store=Depends(get_store)) -> Project:
    return await store.create_project(draft)


@router.get("/projects/{project_id}", response_model=Project, operation_id="getProject")
async def get_project(project_id: str, store=Depends(get_store)) -> Project:
    try:
        return await store.get_project(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")


@router.put(
    "/projects/{project_id}", response_model=Project, operation_id="updateProject"
)
async def update_project(
    project_id: str, draft: ProjectDraft, store=Depends(get_store)
) -> Project:
    try:
        return await store.update_project(project_id, draft)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")


@router.delete(
    "/projects/{project_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteProject",
)
async def delete_project(project_id: str, store=Depends(get_store)):
    try:
        await store.delete_project(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    except ProjectNotEmpty:
        raise HTTPException(
            status_code=409,
            detail="project still contains test cases — move or delete them first",
        )


# -- named sets -----------------------------------------------------------------


@router.get(
    "/projects/{project_id}/sets",
    response_model=list[ScenarioSet],
    operation_id="listSets",
)
async def list_sets(project_id: str, store=Depends(get_store)) -> list[ScenarioSet]:
    try:
        return await store.list_sets(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")


@router.post(
    "/projects/{project_id}/sets",
    response_model=ScenarioSet,
    status_code=201,
    operation_id="createSet",
)
async def create_set(
    project_id: str, draft: ScenarioSetDraft, store=Depends(get_store)
) -> ScenarioSet:
    try:
        return await store.create_set(project_id, draft)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    except ScenarioNotFound as exc:
        raise HTTPException(
            status_code=422, detail=f"scenario '{exc.args[0]}' not found"
        )
    except ForeignScenario as exc:
        raise HTTPException(
            status_code=422,
            detail=f"scenario '{exc.args[0]}' belongs to a different project",
        )


@router.get("/sets/{set_id}", response_model=ScenarioSet, operation_id="getSet")
async def get_set(set_id: str, store=Depends(get_store)) -> ScenarioSet:
    try:
        return await store.get_set(set_id)
    except SetNotFound:
        raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")


@router.put("/sets/{set_id}", response_model=ScenarioSet, operation_id="updateSet")
async def update_set(
    set_id: str, draft: ScenarioSetDraft, store=Depends(get_store)
) -> ScenarioSet:
    try:
        return await store.update_set(set_id, draft)
    except SetNotFound:
        raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")
    except ScenarioNotFound as exc:
        raise HTTPException(
            status_code=422, detail=f"scenario '{exc.args[0]}' not found"
        )
    except ForeignScenario as exc:
        raise HTTPException(
            status_code=422,
            detail=f"scenario '{exc.args[0]}' belongs to a different project",
        )


@router.delete(
    "/sets/{set_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteSet",
)
async def delete_set(set_id: str, store=Depends(get_store)):
    try:
        await store.delete_set(set_id)
    except SetNotFound:
        raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")


@router.put(
    "/sets/{set_id}/scenarios/{scenario_id}",
    response_model=ScenarioSet,
    operation_id="addToSet",
)
async def add_to_set(
    set_id: str, scenario_id: str, store=Depends(get_store)
) -> ScenarioSet:
    try:
        return await store.add_to_set(set_id, scenario_id)
    except SetNotFound:
        raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")
    except ScenarioNotFound:
        raise HTTPException(
            status_code=404, detail=f"scenario '{scenario_id}' not found"
        )
    except ForeignScenario:
        raise HTTPException(
            status_code=422,
            detail=f"scenario '{scenario_id}' belongs to a different project",
        )


@router.delete(
    "/sets/{set_id}/scenarios/{scenario_id}",
    response_model=ScenarioSet,
    operation_id="removeFromSet",
)
async def remove_from_set(
    set_id: str, scenario_id: str, store=Depends(get_store)
) -> ScenarioSet:
    try:
        return await store.remove_from_set(set_id, scenario_id)
    except SetNotFound:
        raise HTTPException(status_code=404, detail=f"set '{set_id}' not found")


# -- global search --------------------------------------------------------------

_SEARCH_LIMIT = 20


@router.get("/search", response_model=SearchResults, operation_id="search")
async def search(
    q: str, project_id: str | None = None, store=Depends(get_store)
) -> SearchResults:
    """Case-insensitive substring search across projects, sets and test cases.

    ``?project_id=`` narrows sets and test cases to one project (projects are
    always searched globally so the box can be used to switch projects).
    """
    needle = q.strip().lower()
    if not needle:
        return SearchResults(query=q, projects=[], sets=[], scenarios=[])

    def matches(*fields: str) -> bool:
        return any(needle in f.lower() for f in fields if f)

    projects = [
        p for p in await store.list_projects() if matches(p.name, p.description)
    ]
    try:
        all_sets = await store.list_sets(project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail=f"project '{project_id}' not found")
    sets = [s for s in all_sets if matches(s.name, s.description)]
    scenarios = [
        s
        for s in await store.list(project_id)
        if matches(s.name, s.description, *s.tags)
    ]
    return SearchResults(
        query=q,
        projects=projects[:_SEARCH_LIMIT],
        sets=sets[:_SEARCH_LIMIT],
        scenarios=scenarios[:_SEARCH_LIMIT],
    )


app.include_router(router)
