"""PayProbe Run Orchestrator.

Owns run lifecycle and the live event transport. Events flow:

    WorkerEngine ── StreamSink ──▶ StreamBackbone ──▶ WebSocket clients
                   (in-memory or Redis Streams)        (resume via last_event_id)

WebSocket transport (spec §4.2 ``GET /runs/{id}/stream``):
- the server replays everything after the client's ``last_event_id`` (query
  param), then tails live, so a reconnecting client never misses events.
- ``cancel`` is a plain REST POST — the WS channel is observe-only, which keeps
  the stream a clean one-way feed even though the protocol is bidirectional.

Backbone selection: set ``REDIS_URL`` to use Redis Streams; otherwise an
in-process backbone is used (fine for the mock end-to-end path and CI).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("orchestrator")

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from worker.engine.metrics import render as render_metrics, CONTENT_TYPE as METRICS_CT
from .observability import (
    metrics_middleware, init_tracing,
    RUNS_INFLIGHT, RUNS_TOTAL,
    SIMULATOR_UP, SIMULATOR_MESSAGES, SIMULATOR_RPS, SIMULATOR_FAULTS,
)

from .auth import require_auth, authorize_websocket, auth_enforced
from .playground import PlaygroundHistory, build_playground_scenario
from .resilience import (
    STAGE_BASELINE,
    STAGE_RECOVERY,
    STAGE_STORM,
    score_resilience,
)

from worker.engine import (
    WorkerEngine, StreamSink, InMemoryStreamBackbone, RedisStreamBackbone,
    RunEvent, DEBUG_PAUSED, DEBUG_RESUMED, RUN_COMPLETED, RUN_STARTED,
    FlowRunner,
)
from worker.adapters.registry import AdapterRegistry
from worker.engine.variables import GEN_KEY
from worker.adapters.socket_registry import snapshot as adapter_socket_snapshot
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.chaos import MALFORMED_MODES as CHAOS_MALFORMED_MODES
from worker.adapters.tcp.responder import TcpResponder
from worker.adapters.tcp.flow_responder import FlowResponder
# NB: HttpFlowResponder is imported lazily in _build_flow_responder — it pulls in
# aiohttp, which the slim orchestrator image does not install unless HTTP
# participant listeners are actually used. A top-level import here would crash
# orchestrator startup on images without aiohttp.
from worker.adapters.hsm.payshield import PayShieldSimulator

from worker.engine.load import LoadProfile, InMemoryLoadBus, RedisLoadBus
from worker.load_worker import run_worker as _in_proc_load_worker

# Report/certification format logic lives in the report-service library; the
# orchestrator owns the run data and delegates rendering to it.
from report_service.generators import (
    certify as _certify,
    certification_html as _certification_html,
    signoff_html as _signoff_html,
    junit_xml as _junit_xml,
    html_report as _html_report,
    index_steps as _index_steps,
)
from report_service.diagnose import diagnose as _diagnose
from report_service.gates import GatePolicy as _GatePolicy, evaluate_gates as _evaluate_gates
from report_service.provenance import provenance as _provenance, content_hash as _content_hash

# Shared encryption-at-rest (no-op passthrough unless PAYPROBE_SECRET_KEY is set).
from payprobe_common.crypto import SecretBox as _SecretBox

from .run_store import RunStore
from .schedule_store import ScheduleStore
from .simulator_store import SimulatorStore
from .signoff_store import SignoffStore
from .load_coordinator import LoadCoordinator
from .run_control import make_run_control
from .worker_provisioner import make_provisioner, manual_command

REPO = Path(__file__).resolve().parents[3]
# In a container the repo tree may not be present; allow an explicit override.
EXAMPLES_DIR = Path(os.environ.get("PAYPROBE_EXAMPLES_DIR", str(REPO / "examples")))
# Where to fetch real scenarios from when a run is launched by id/project/set.
SCENARIO_API_URL = os.environ.get("SCENARIO_API_URL", "http://scenario-service:8000")
# Auth-service base, used by the /status aggregator.
AUTH_API_URL = os.environ.get("AUTH_API_URL", "http://auth-service:8300")
# MCP server base (Streamable HTTP transport). Optional: only probed by /status when
# set, since the MCP server is commonly run over stdio with no HTTP surface.
MCP_API_URL = os.environ.get("MCP_API_URL", "")
# Config-assistant base, used by the /status aggregator. Optional: only probed
# when set (the assistant may not be deployed in every environment).
ASSIST_API_URL = os.environ.get("ASSIST_API_URL", "")
# Insight-service base (ADR-0005, advisory ML). Optional: only probed when set.
INSIGHT_API_URL = os.environ.get("INSIGHT_API_URL", "")

app = FastAPI(
    title="PayProbe Run Orchestrator",
    version="0.1.0",
    # Fail-closed auth on every HTTP route. Public paths (/health, /metrics,
    # /ready, /status, /reference, ...) are allow-listed inside the dependency.
    # WebSocket routes are guarded separately via authorize_websocket().
    dependencies=[Depends(require_auth)],
)

# Allow the portal (and any configured origin) to call the API from the browser.
# Same default origins as the other services; override with CORS_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:8080,http://localhost:4200"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Record request rate / latency for every route (Prometheus, see /metrics).
app.middleware("http")(metrics_middleware)
# Optional OTLP tracing — no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
init_tracing("payprobe-orchestrator")


@app.get("/reference", include_in_schema=False)
async def api_reference() -> HTMLResponse:
    """Interactive API reference (Scalar) for the run orchestrator."""
    html = """<!doctype html>
<html>
  <head>
    <title>PayProbe Orchestrator — API Reference</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>body { margin: 0 }</style>
  </head>
  <body>
    <script id="api-reference" data-url="/openapi.json"></script>
    <script>
      var c = document.getElementById('api-reference');
      c.dataset.configuration = JSON.stringify({ theme: 'purple', layout: 'modern' });
    </script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
  </body>
</html>"""
    return HTMLResponse(html)


# -- backbone -----------------------------------------------------------------

def _make_backbone():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis.asyncio as aioredis  # imported only when configured
        return RedisStreamBackbone(aioredis.from_url(url, decode_responses=True))
    return InMemoryStreamBackbone()


backbone = _make_backbone()


# -- load-test bus + coordinator ----------------------------------------------

def _make_load_bus():
    """Redis bus ⇒ external worker fleet; in-memory bus ⇒ single-node fallback."""
    url = os.environ.get("REDIS_URL")
    if url:
        import redis.asyncio as aioredis
        return RedisLoadBus(aioredis.from_url(url, decode_responses=True))
    return InMemoryLoadBus()


load_bus = _make_load_bus()

# Fleet coordinator for network-flow listeners (ADR-0001). Fleet hosting is
# explicit opt-in: NETWORK_FLEET=1 *and* at least one worker.flow_host
# heartbeating; otherwise networks start in-process exactly as before.
from .flow_fleet import FlowFleet, PlacementError  # noqa: E402

flow_fleet = FlowFleet(lambda: load_bus)  # lazy: tests may rebind load_bus


def _fleet_enabled() -> bool:
    return os.environ.get("NETWORK_FLEET") == "1"


# -- fleet endpoint discovery (ADR-0001 item 4) --------------------------------
# A fleet-hosted listener binds its planned port on *some worker host*, so any
# config that statically points at that port (an outbound connection, a group
# member, a relay upstream) must be re-pointed at the advertised host. The
# match key is the planned listen port — deterministic because the network's
# port planner already guarantees one instance per (host, port).

def _fleet_endpoint_index() -> dict[int, str]:
    """port → advertised host, across every live fleet run's registry."""
    idx: dict[int, str] = {}
    for rows in flow_fleet.snapshots().values():
        for i in rows:
            if i.get("status") == "up" and i.get("port") and i.get("host"):
                idx[int(i["port"])] = str(i["host"])
    return idx


def _rewrite_cfg_endpoint(cfg: dict, idx: dict[int, str]) -> bool:
    """Re-point one adapter-ish config at advertised fleet hosts (host/port,
    gRPC ``target``, HTTP ``base_url``/``url``). Returns True if changed."""
    import re as _re

    changed = False
    try:
        port = int(cfg.get("port")) if cfg.get("port") is not None else None
    except (TypeError, ValueError):
        port = None
    if port and port in idx and cfg.get("host") != idx[port]:
        cfg["host"] = idx[port]
        changed = True
    target = cfg.get("target")
    if isinstance(target, str) and ":" in target:
        h, _, p = target.rpartition(":")
        if p.isdigit() and int(p) in idx and h != idx[int(p)]:
            cfg["target"] = f"{idx[int(p)]}:{p}"
            changed = True
    for key in ("base_url", "url"):
        u = cfg.get(key)
        if isinstance(u, str):
            m = _re.search(r"://([^/:]+):(\d+)", u)
            if m and int(m.group(2)) in idx and m.group(1) != idx[int(m.group(2))]:
                cfg[key] = u.replace(
                    f"://{m.group(1)}:{m.group(2)}",
                    f"://{idx[int(m.group(2))]}:{m.group(2)}", 1)
                changed = True
    return changed


def _rewrite_adapters_map(adapters: dict, idx: dict[int, str]) -> int:
    """Re-point every adapter config in a worker adapters map (plain configs +
    inlined group members). Returns how many configs changed."""
    hits = 0
    for cfg in (adapters or {}).values():
        if not isinstance(cfg, dict):
            continue
        if _rewrite_cfg_endpoint(cfg, idx):
            hits += 1
        for m in cfg.get("members") or []:
            mc = m.get("config") if isinstance(m, dict) else None
            if isinstance(mc, dict) and _rewrite_cfg_endpoint(mc, idx):
                hits += 1
    return hits


def _attach_fleet_endpoints(env: dict) -> None:
    """Run-build discovery: point a run's adapters at fleet-advertised
    endpoints. No-op without fleet mode or live advertisements, so the
    in-process path is untouched."""
    if not _fleet_enabled():
        return
    idx = _fleet_endpoint_index()
    if not idx:
        return
    hits = _rewrite_adapters_map((env or {}).get("adapters") or {}, idx)
    if hits:
        log.info("fleet discovery re-pointed %d adapter config(s)", hits)


def _rewrite_fleet_refs(placement: dict, idx: dict[int, str]) -> None:
    """Point a placement's outbound references (downstream adapters, relay
    proxy upstreams) at endpoints already advertised by earlier waves — this
    is what lets a fleet-hosted switch on host B reach issuers on host A."""
    _rewrite_adapters_map(placement.get("downstream") or {}, idx)
    pc = placement.get("proxy_config")
    if isinstance(pc, dict):
        for up in pc.get("upstreams") or []:
            if isinstance(up, dict):
                _rewrite_cfg_endpoint(up, idx)
        if isinstance(pc.get("upstream"), dict):
            _rewrite_cfg_endpoint(pc["upstream"], idx)

def _default_run_db() -> str:
    """Where to persist the run registry.

    Explicit ``RUN_DB`` always wins. Otherwise we fail safe: dev/test/CI keep the
    zero-config in-memory store, but any other environment defaults to a file on
    disk so a restart doesn't drop the ability to list/inspect runs (and so two
    replicas can share one DB path on a volume).
    """
    explicit = os.environ.get("RUN_DB")
    if explicit:
        return explicit
    dev = os.environ.get("PAYPROBE_ENV", "").lower() in {"dev", "development", "test", "local"}
    return ":memory:" if dev else os.environ.get("RUN_DB_PATH", "/data/runs.db")


# Durable run registry. Set RUN_DB to a file (on a volume) or leave it unset —
# non-dev environments persist to disk by default (see _default_run_db).
run_store = RunStore(_default_run_db())

# Durable schedule registry (file-backed; in-memory by default).
schedule_store = ScheduleStore(os.environ.get("SCHEDULES_FILE", ":memory:"))

# Durable saved-simulator registry (file-backed; in-memory by default). Enabled
# simulators are auto-started on boot (see _autostart_simulators).
simulator_store = SimulatorStore(os.environ.get("SIMULATORS_FILE", ":memory:"))

# Immutable Go/No-Go sign-off snapshots + approval trail + baseline pointers
# (file-backed; in-memory by default). See ADR-0003 and signoff_store.py. The
# shared payprobe_common SecretBox encrypts secret-named fields at rest (no-op
# unless PAYPROBE_SECRET_KEY is set).
signoff_store = SignoffStore(os.environ.get("SIGNOFFS_FILE", ":memory:"),
                             box=_SecretBox())

# Durable desired-state for participant listeners + topology runs. Running
# participants (PARTICIPANTS) and topology runs (TOPOLOGY_RUNS) are in-memory, so
# they vanish when the orchestrator container is recreated. We persist a small
# desired-state file (standalone flow ids + running topology ids) and re-launch
# it on boot (see _autostart_runtime). ":memory:" disables persistence (dev/tests).
RUNTIME_STATE_FILE = os.environ.get("ORCH_RUNTIME_FILE", ":memory:")

# Load coordinator. In-memory bus ⇒ run workers in-process. Redis bus ⇒ external
# `python -m worker.load_worker` processes are expected, but the coordinator
# falls back to in-process workers if none claim the shards (so a single-node
# deployment with Redis still produces load). Set PAYPROBE_LOAD_EXTERNAL_WORKERS=1
# to disable the fallback on a real distributed fleet.
_external_only = os.environ.get("PAYPROBE_LOAD_EXTERNAL_WORKERS", "").lower() in ("1", "true", "yes")
# Worker provisioner: lets a launched run bring its own fleet up/down and lets
# the portal scale workers live (the "dynamic" path). PAYPROBE_WORKER_PROVISIONER
# = auto (default) | local | compose | none. When none/unavailable the fleet is
# still managed the "custom" way (operator starts `load_worker` themselves).
worker_provisioner = make_provisioner(redis_url=os.environ.get("REDIS_URL"))
load_coordinator = LoadCoordinator(
    load_bus, backbone, run_store,
    in_proc_worker=None if _external_only else _in_proc_load_worker,
    provisioner=worker_provisioner,
    inproc_max_tps=float(os.environ.get("PAYPROBE_INPROC_MAX_TPS", "2000")),
    inproc_max_connections=int(os.environ.get("PAYPROBE_INPROC_MAX_CONNECTIONS", "5000")),
    drain_ceiling_s=float(os.environ.get("PAYPROBE_LOAD_DRAIN_CEILING_S", "600")),
)


# -- live run tracking (task handles for cancel; detail lives in run_store) ----

class DebugSession:
    """Per-run debug state: breakpoints + a resume gate the run task blocks on."""

    def __init__(self, breakpoints: list[str], step_mode: bool) -> None:
        self.breakpoints: set[str] = set(breakpoints or [])
        self.step_mode = step_mode          # pause at every node
        self.pause_requested = False        # break in at the next node
        self.paused = False
        self.paused_node: str | None = None
        self.resume = asyncio.Event()


class RunRecord:
    def __init__(self, run_id: str, env: dict, scenarios: list[dict]):
        self.id = run_id
        self.env = env
        self.scenarios = scenarios
        self.status = "pending"
        self.summary: dict | None = None
        self.task: asyncio.Task | None = None
        self.debug: DebugSession | None = None


RUNS: dict[str, RunRecord] = {}


# Cross-replica run control: lets any orchestrator replica observe and cancel an
# in-flight run, even one whose asyncio task lives in a different process. Backed
# by Redis when REDIS_URL is set; an in-memory registry otherwise.
run_control = make_run_control(os.environ.get("REDIS_URL"))
# give the load coordinator the same control plane so any replica can stop a
# load run it doesn't own (functional runs already route through run_control).
load_coordinator.run_control = run_control


async def _cancel_local_run(run_id: str) -> None:
    """Stop a run this replica owns (the control handler for cross-replica
    cancel). Handles both functional runs (cancel the task) and load runs
    (signal the coordinator). Run ids are unique across both kinds."""
    rec = RUNS.get(run_id)
    if rec and rec.task and not rec.task.done():
        rec.task.cancel()
        return
    # load run owned here → stop it via the coordinator
    if load_coordinator.get(run_id) is not None:
        await load_coordinator.stop(run_id)


run_control.set_cancel_handler(_cancel_local_run)


# -- request/response models --------------------------------------------------

class CreateRunRequest(BaseModel):
    environment: dict[str, Any] | None = None
    environment_name: str | None = None             # pick a bundled env by name
    scenarios: list[dict[str, Any]] | None = None   # inline scenario docs
    scenario_ids: list[str] | None = None           # fetch these from scenario-service
    project_id: str | None = None                   # run every case in a project
    set_id: str | None = None                       # run every case in a named set
    label: str | None = None                        # human label for the registry
    debug: bool = False                             # start a step-through debug run
    breakpoints: list[str] = []                     # node ids to pause at
    data_table: str | None = None                   # data-driven: a global table name
    dataset: list[dict[str, Any]] | None = None     # data-driven: inline rows
    requires_topology: str | None = None            # gate: this topology must be up


class CreateRunResponse(BaseModel):
    run_id: str
    status: str


class FlowDebugRunRequest(BaseModel):
    """Test-fire a participant flow from the editor: run it once against a
    sample trigger request, optionally paused/stepped like a scenario debug run.

    The flow doc may be supplied inline (``flow`` — so unsaved editor state is
    debuggable) or by id (``flow_id`` — fetched from scenario-service). The
    sample ``request`` is what an inbound message would decode to; it is seeded
    into the flow context as ``${request.*}``.
    """

    flow: dict[str, Any] | None = None     # inline flow doc (unsaved edits ok)
    flow_id: str | None = None             # or: fetch this saved flow
    request: dict[str, Any] = {}           # sample decoded inbound message
    debug: bool = True                     # pause/step (False = plain test-fire)
    breakpoints: list[str] = []            # node ids to pause at
    state: dict[str, Any] = {}             # optional initial ${state.*}
    label: str | None = None


def _load_defaults() -> tuple[dict, list[dict]]:
    env = json.loads((EXAMPLES_DIR / "environments/mock.json").read_text())
    scenarios = [
        json.loads(p.read_text())
        for p in sorted((EXAMPLES_DIR / "scenarios").glob("*.json"))
    ]
    return env, scenarios


def _load_env_file(name: str) -> dict | None:
    """A bundled environment by file stem (path-traversal safe), or None."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", name)
    path = EXAMPLES_DIR / "environments" / f"{safe}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


async def _load_env_by_name(name: str) -> dict:
    """Resolve an environment by name: the editable registry wins, else bundled.

    Environments are now first-class records in scenario-service (created/edited
    in the portal). We prefer a stored one; if the service is unreachable or has
    no such name, fall back to the bundled JSON file shipped with the repo.
    """
    try:
        doc = await _http_get_json(f"{SCENARIO_API_URL}/environments/{quote(name, safe='')}")
        if isinstance(doc, dict):
            doc.pop("key", None)  # registry-internal field, not run config
            return doc
    except Exception:  # noqa: BLE001 — fall back to bundled files
        pass
    bundled = _load_env_file(name)
    if bundled is None:
        raise HTTPException(400, f"unknown environment '{name}'")
    return bundled


# -- fetching scenarios from the scenario-service (module-level for testing) ---

def _service_auth_header() -> dict[str, str]:
    """Credential the orchestrator presents to scenario-service.

    scenario-service fails closed just like we do, so unauthenticated calls get
    a 401. We mirror the credential kinds its gate accepts, in order:

    1. **static bearer** — ``SCENARIO_API_TOKEN`` (or the shared ``API_TOKEN``).
       The simplest service-to-service mechanism; both services compare the same
       string.
    2. **minted service JWT** — if only ``AUTH_JWT_SECRET`` (HS256) is set we
       mint a short-lived token the peer verifies (signature + ``exp``), so a
       JWT-only deployment works without a static shared secret.
    3. **none** — dev/local with auth off; send nothing.
    """
    token = os.environ.get("SCENARIO_API_TOKEN") or os.environ.get("API_TOKEN")
    if not token:
        secret = os.environ.get("AUTH_JWT_SECRET")
        if secret:
            try:
                import jwt  # PyJWT
                now = int(time.time())
                claims: dict[str, Any] = {
                    "sub": "orchestrator", "svc": "orchestrator",
                    "iat": now, "exp": now + 60,
                }
                if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
                    claims["aud"] = aud
                if iss := os.environ.get("AUTH_JWT_ISSUER"):
                    claims["iss"] = iss
                token = jwt.encode(claims, secret, algorithm="HS256")
            except Exception:  # noqa: BLE001 — fall through to no header
                token = None
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _http_get_json(url: str) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_service_auth_header())
        resp.raise_for_status()
        return resp.json()


async def _http_post_json(url: str, body: dict) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=body, headers=_service_auth_header())
        resp.raise_for_status()
        return resp.json()


async def _http_json(method: str, url: str, body: dict | None = None) -> Any:
    """Generic authenticated JSON request to a sibling service (PUT/DELETE)."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(method, url, json=body,
                                    headers=_service_auth_header())
        resp.raise_for_status()
        return resp.json() if resp.content else None


async def _fetch_scenarios_by_ids(ids: list[str]) -> list[dict]:
    import httpx

    out: list[dict] = []
    for sid in ids:
        try:
            sc = await _http_get_json(f"{SCENARIO_API_URL}/scenarios/{sid}")
        except httpx.HTTPStatusError as exc:
            # An unknown/stale scenario id makes scenario-service answer 404.
            # Surface it as a clean 404 (the portal shows the detail) instead of
            # letting the raw httpx error become an unhandled 500.
            if exc.response.status_code == 404:
                raise HTTPException(404, f"scenario '{sid}' not found") from exc
            raise HTTPException(
                502,
                f"scenario-service returned HTTP {exc.response.status_code} "
                f"for scenario '{sid}'",
            ) from exc
        except httpx.RequestError as exc:
            # scenario-service unreachable (down, wrong URL, network): a 502 with
            # the reason beats a bare "Internal Server Error".
            raise HTTPException(
                502, f"scenario-service unreachable: {exc}"
            ) from exc
        # Replace scenario-scoped vars with the full effective merge
        # (global < project < set < scenario) so steps see ${vars.NAME}.
        try:
            v = await _http_get_json(f"{SCENARIO_API_URL}/scenarios/{sid}/variables")
            sc["variables"] = v.get("effective", sc.get("variables", {}))
            sc["__secrets__"] = v.get("secret_values", [])
        except Exception as exc:  # noqa: BLE001 - variables are best-effort
            log.debug("variable merge for scenario %s failed: %s", sid, exc)
        out.append(sc)
    return out


async def _fetch_scenario_ids(
    project_id: str | None = None, set_id: str | None = None
) -> list[str]:
    params = []
    if project_id:
        params.append(f"project_id={project_id}")
    if set_id:
        params.append(f"set_id={set_id}")
    qs = ("?" + "&".join(params)) if params else ""
    summaries = await _http_get_json(f"{SCENARIO_API_URL}/scenarios{qs}")
    return [s["id"] for s in summaries]


async def _attach_tables(scenarios: list[dict]) -> None:
    """Inject the global named tables into each scenario (best-effort)."""
    try:
        tables = await _http_get_json(f"{SCENARIO_API_URL}/tables/runtime")
    except Exception:  # noqa: BLE001
        return
    for sc in scenarios:
        sc.setdefault("tables", tables)


#: Connection/env migration — now COMPLETE (see docs/history/CONNECTION-ENV-MIGRATION-PLAN.md).
#: A connection's resolved config (base ⊕ environment_overrides) WINS over a
#: same-named adapter the environment defines inline. This is the default: the
#: connection's override matrix is the single source of per-environment values.
#: Escape hatch — set ``PAYPROBE_CONNECTION_OVERRIDE_WINS=0`` (or false/no) to
#: restore the legacy "inline env adapter wins" behaviour if ever needed.
_CONNECTION_OVERRIDE_WINS = os.environ.get(
    "PAYPROBE_CONNECTION_OVERRIDE_WINS", "1"
).lower() not in ("0", "false", "no")

#: Default-connection model — now the DEFAULT (docs/history/DEFAULT-CONNECTION-MODEL-SPEC.md).
#: An action step that names NO connection resolves to the **default connection**
#: for its adapter type. If no default exists for the type, the step is left as-is
#: and falls back to the environment's inline adapter — this fallback is retained
#: on purpose so a not-yet-seeded type degrades gracefully instead of failing, and
#: it becomes inert once every type has a default and the env adapters are slimmed.
#: Escape hatch — set ``PAYPROBE_DEFAULT_CONNECTION_RESOLUTION=0`` (or false/no) to
#: turn default resolution off entirely (bare targets hit the env adapter, legacy).
_DEFAULT_CONNECTION_RESOLUTION = os.environ.get(
    "PAYPROBE_DEFAULT_CONNECTION_RESOLUTION", "1"
).lower() not in ("0", "false", "no")

#: Connection metadata keys that are not worker adapter config.
_NON_ADAPTER_KEYS = {"name", "environments", "environment_overrides", "default"}


def _conn_type_key(adapter: str | None, protocol: str | None) -> str:
    """Adapter-type grouping key (mirrors scenario-service ``type_key``): the TCP
    adapter's two wire protocols are distinct types; every other adapter is one."""
    a = (adapter or "tcp").lower()
    if a == "tcp":
        return f"tcp/{(protocol or 'iso8583').lower()}"
    return a


def _target_type_key(target: str | None) -> str:
    """The adapter type a bare catalog target resolves to (mirrors the worker
    registry families + the portal's ``targetFamily``)."""
    t = (target or "").lower()
    if t in ("hsm", "hsm_tcp"):
        return "tcp/header_echo"
    if t == "grpc":
        return "grpc"
    if t in ("http", "rest_pay"):
        return "http"
    if t.startswith("db_probe"):
        return t
    if t == "payshield":
        return "payshield"
    return "tcp/iso8583"  # tcp_iso8583, switch, acquirer, tcp, …


def _connection_effective(doc: dict, env_name: str | None) -> dict:
    """A connection's worker-shaped config under an environment: base (metadata
    stripped) ⊕ ``environment_overrides[env_name]``."""
    base = {k: v for k, v in doc.items() if k not in _NON_ADAPTER_KEYS}
    overrides = (doc.get("environment_overrides") or {}).get(env_name or "", {})
    if isinstance(overrides, dict) and overrides:
        base = {**base, **overrides}
    return base


async def _attach_connections(
    env: dict, scenarios: list[dict], env_name: str | None = None
) -> None:
    """Wire saved connections into a run (best-effort).

    1. Merge each stored connection (worker-shaped adapter config, keyed by
       name) into ``env['adapters']``. Precedence between a connection and an
       inline env adapter of the same name is controlled by
       ``_CONNECTION_OVERRIDE_WINS``: legacy (default) lets the inline env adapter
       win; with the flag on the connection's resolved config wins (the migrated
       model — the connection's override matrix is the single source of per-env
       values). Either way, when the run has a named environment, that
       connection's ``environment_overrides`` for this environment are
       shallow-merged over its base config first, so the same connection can point
       at a different host/port/etc. per environment. Adapters that are NOT
       connections (hsm, db_probe, …) are never touched here.
    2. Re-point any action step that selected a connection (the editor stores
       the choice in ``step.config.connection``) at that connection by setting
       ``step.target`` to the connection name. The action is unchanged — the
       universal TCP adapter handles it for whichever named instance resolves.
    """
    # only the connections a step actually selects — never pull every saved
    # connection into the run (that would make warmup try to reach every host).
    referenced = {
        (step.get("config") or {}).get("connection")
        for sc in scenarios
        for step in sc.get("steps", [])
        if step.get("kind", "action") == "action" and (step.get("config") or {}).get("connection")
    }
    # With default-connection resolution on we must run even when no step names a
    # connection (unbound steps still resolve to a type default).
    if not referenced and not _DEFAULT_CONNECTION_RESOLUTION:
        return
    try:
        conns = await _http_get_json(f"{SCENARIO_API_URL}/connections")
    except Exception:  # noqa: BLE001
        conns = []
    adapters = env.setdefault("adapters", {})
    for doc in conns:
        name = doc.get("name")
        if not (name and name in referenced):
            continue
        base = _connection_effective(doc, env_name)
        if _CONNECTION_OVERRIDE_WINS:
            adapters[name] = base  # migrated model: connection wins over inline env
        else:
            adapters.setdefault(name, base)  # legacy: explicit env adapter wins
    for sc in scenarios:
        for step in sc.get("steps", []):
            if step.get("kind", "action") != "action":
                continue
            chosen = (step.get("config") or {}).get("connection")
            if chosen:
                # keep the catalog target so the report can show both the step
                # type and the connection it ran against
                step.setdefault("config", {})["_catalog_target"] = step.get("target")
                step["target"] = chosen

    # Phase C: an unbound action step (no chosen connection) resolves to the
    # DEFAULT connection for its adapter type. No default for the type → leave the
    # step as-is, so it still hits the environment's inline adapter (fallback).
    if _DEFAULT_CONNECTION_RESOLUTION:
        defaults_by_type: dict[str, dict] = {}
        for doc in conns:
            if doc.get("default") and doc.get("name"):
                defaults_by_type[_conn_type_key(doc.get("adapter"), doc.get("protocol"))] = doc
        if defaults_by_type:
            for sc in scenarios:
                for step in sc.get("steps", []):
                    if step.get("kind", "action") != "action":
                        continue
                    if (step.get("config") or {}).get("connection"):
                        continue  # already bound above
                    doc = defaults_by_type.get(_target_type_key(step.get("target")))
                    if not doc:
                        continue
                    name = doc["name"]
                    adapters[name] = _connection_effective(doc, env_name)
                    step.setdefault("config", {})["_catalog_target"] = step.get("target")
                    step["target"] = name


# Connection-doc keys that are registry metadata, not worker adapter config.
_SKIP_CONN_KEYS = {"name", "mode", "listen_port", "environments",
                   "environment_overrides", "description", "disabled", "default"}


async def _load_groups_index() -> dict[str, dict]:
    """Participant groups keyed by both name and id (best-effort, empty on error)."""
    try:
        groups = await _http_get_json(f"{SCENARIO_API_URL}/participant-groups")
    except Exception:  # noqa: BLE001
        return {}
    idx: dict[str, dict] = {}
    if isinstance(groups, list):
        for g in groups:
            if g.get("name"):
                idx[g["name"]] = g
            if g.get("id"):
                idx[g["id"]] = g
    return idx


async def _group_adapter_config(group: dict) -> dict | None:
    """Inline a group into a worker ``{"adapter": "group", members, selection}``
    config, resolving each member's connection. ``None`` if no member resolves."""
    members = []
    for m in group.get("members", []):
        cname = m.get("connection")
        if not cname:
            continue
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{cname}")
        except Exception:  # noqa: BLE001
            doc = None
        if isinstance(doc, dict):
            if doc.get("disabled"):
                continue  # administratively disabled — out of rotation
            cfg = {k: v for k, v in doc.items() if k not in _SKIP_CONN_KEYS}
            members.append({"connection": cname, "config": cfg,
                            "weight": m.get("weight", 1)})
    if not members:
        return None
    return {
        "adapter": "group",
        "members": members,
        "selection": group.get("selection") or {"policy": "round_robin"},
    }


async def _attach_groups(env: dict, scenarios: list[dict]) -> None:
    """Resolve participant groups a run references into the env (best-effort).

    A step can target a *group* name (via ``step.config.connection``) instead of
    a single connection. Each group is inlined as a ``{"adapter": "group",
    "members": [...], "selection": {...}}`` entry whose members carry their
    resolved connection configs, so the worker's registry builds a GroupAdapter
    that selects a member per dispatch. Runs after ``_attach_connections`` (which
    already re-points the step's target at the chosen name)."""
    referenced = {
        (step.get("config") or {}).get("connection")
        for sc in scenarios
        for step in sc.get("steps", [])
        if step.get("kind", "action") == "action" and (step.get("config") or {}).get("connection")
    }
    if not referenced:
        return
    by_key = await _load_groups_index()
    if not by_key:
        return

    adapters = env.setdefault("adapters", {})
    for ref in referenced:
        g = by_key.get(ref)
        if not g:
            continue
        cfg = await _group_adapter_config(g)
        if cfg:
            adapters[ref] = cfg
    # ensure any step that chose a group is pointed at it (idempotent with
    # _attach_connections' re-point).
    for sc in scenarios:
        for step in sc.get("steps", []):
            if step.get("kind", "action") != "action":
                continue
            chosen = (step.get("config") or {}).get("connection")
            if chosen and isinstance(adapters.get(chosen), dict) \
                    and adapters[chosen].get("adapter") == "group":
                step.setdefault("config", {}).setdefault("_catalog_target", step.get("target"))
                step["target"] = chosen


async def _attach_step_environments(env: dict, scenarios: list[dict]) -> None:
    """Apply per-step environment overrides into a run (best-effort).

    A step may pin ``environment_override`` to a named environment, meaning "run
    *this* step against that environment's adapter config" while the rest of the
    run uses its default environment. We follow the same model as
    ``_attach_connections``: resolve the override environment, register its
    adapter (the one matching the step's resolved ``target``) into the run's
    ``env['adapters']`` under a derived name, and re-point the step at it.

    Runs after ``_attach_connections`` so ``step['target']`` is already the
    final adapter/connection name. Defensive throughout: an unknown environment,
    an unreachable scenario-service, or a missing adapter leaves the step on its
    default target rather than failing the run.
    """
    referenced = {
        step.get("environment_override")
        for sc in scenarios
        for step in sc.get("steps", [])
        if step.get("kind", "action") == "action" and step.get("environment_override")
    }
    if not referenced:
        return

    # Resolve each referenced environment once (cache misses as None to skip).
    resolved: dict[str, dict | None] = {}
    for name in referenced:
        try:
            resolved[name] = await _load_env_by_name(name)
        except Exception:  # noqa: BLE001 — best-effort; fall back to default env
            resolved[name] = None

    adapters = env.setdefault("adapters", {})
    for sc in scenarios:
        for step in sc.get("steps", []):
            if step.get("kind", "action") != "action":
                continue
            override = step.get("environment_override")
            if not override:
                continue
            oenv = resolved.get(override)
            target = step.get("target")
            if not oenv or not target:
                continue
            override_adapter = (oenv.get("adapters") or {}).get(target)
            if override_adapter is None:
                # the override env doesn't define this adapter — leave the step
                # on its default target rather than guess.
                continue
            derived = f"{target}@{override}"
            adapters.setdefault(
                derived, {k: v for k, v in override_adapter.items() if k != "name"}
            )
            cfg = step.setdefault("config", {})
            cfg.setdefault("_catalog_target", step.get("target"))
            cfg["_environment_override"] = override
            step["target"] = derived


#: ``${key.NAME}`` — a crypto key referenced by registry name instead of pasted
#: hex (Test Data Manager phase 3). Names are registry slugs.
_KEY_TOKEN_RE = re.compile(r"\$\{key\.([A-Za-z0-9_\-]+)\}")


def _sub_key_tokens(node: Any, materials: dict[str, str]) -> Any:
    """Replace ``${key.NAME}`` tokens with resolved material, everywhere in the
    scenario JSON. Unresolved names stay as the literal token — the crypto step
    then fails loudly on bad hex instead of silently using an empty key."""
    if isinstance(node, dict):
        return {k: _sub_key_tokens(v, materials) for k, v in node.items()}
    if isinstance(node, list):
        return [_sub_key_tokens(v, materials) for v in node]
    if isinstance(node, str):
        return _KEY_TOKEN_RE.sub(
            lambda m: materials.get(m.group(1), m.group(0)), node,
        )
    return node


async def _attach_test_data(scenarios: list[dict]) -> None:
    """Resolve named test data into the inline form the worker consumes.

    A scenario may reference a saved pool by name (``card_pool`` /
    ``terminal_pool``) from the test-data registry instead of inlining values,
    and any string field may reference a stored crypto key as ``${key.NAME}``
    (crypto-node key hex, payload fields, …). Resolve both here so the worker
    needs no knowledge of the registry. Inline values already on the scenario
    win. Key material comes from the registry's service-gated material
    endpoint — it never transits a browser-facing read. Best-effort: a missing
    pool/key or unreachable service leaves the scenario unchanged (an
    unresolved ``${key.X}`` token fails loudly at the crypto step).
    """
    card_names = {sc.get("card_pool") for sc in scenarios if sc.get("card_pool")}
    term_names = {sc.get("terminal_pool") for sc in scenarios if sc.get("terminal_pool")}
    key_names = {
        name
        for sc in scenarios
        for name in _KEY_TOKEN_RE.findall(json.dumps(sc, default=str))
    }
    if not card_names and not term_names and not key_names:
        return

    async def _fetch(kind: str, name: str, field: str) -> list | None:
        try:
            doc = await _http_get_json(
                f"{SCENARIO_API_URL}/test-data/{kind}/{name}"
            )
        except Exception:  # noqa: BLE001 — best effort
            return None
        return doc.get(field) or []

    cards_by_name = {n: await _fetch("card-pools", n, "cards") for n in card_names}
    terms_by_name = {n: await _fetch("terminal-pools", n, "terminals") for n in term_names}

    materials: dict[str, str] = {}
    for n in key_names:
        try:
            doc = await _http_get_json(
                f"{SCENARIO_API_URL}/test-data/keys/{n}/material"
            )
            if doc.get("value"):
                materials[n] = doc["value"]
        except Exception:  # noqa: BLE001 — best effort, token stays literal
            pass

    for sc in scenarios:
        cp = sc.get("card_pool")
        if cp and cards_by_name.get(cp) is not None and not sc.get("cards"):
            sc["cards"] = cards_by_name[cp]
        tp = sc.get("terminal_pool")
        if tp and terms_by_name.get(tp) is not None and not sc.get("terminals"):
            sc["terminals"] = terms_by_name[tp]
        if materials:
            resolved = _sub_key_tokens(sc, materials)
            sc.clear()
            sc.update(resolved)


_VARS_TOKEN_RE = re.compile(r"\$\{vars\.([A-Za-z0-9_]+)\}")


def _interp_str(value: str, variables: dict) -> Any:
    """Substitute ``${vars.NAME}`` in a string.

    If the whole string is exactly one token, return the variable's *typed*
    value (so a numeric ``base port`` / ``timeout`` stays a number); otherwise do
    in-string text replacement. Unknown names are left untouched.
    """
    m = _VARS_TOKEN_RE.fullmatch(value.strip())
    if m and m.group(1) in variables:
        return variables[m.group(1)]
    return _VARS_TOKEN_RE.sub(
        lambda mm: str(variables[mm.group(1)]) if mm.group(1) in variables else mm.group(0),
        value,
    )


def _interp(node: Any, variables: dict) -> Any:
    if isinstance(node, dict):
        return {k: _interp(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_interp(v, variables) for v in node]
    if isinstance(node, str):
        return _interp_str(node, variables)
    return node


async def _interpolate_env_vars(env: dict) -> dict:
    """Resolve ``${vars.NAME}`` tokens in environment (adapter) config.

    Step *payloads* are interpolated by the worker, but the environment's adapter
    config (e.g. ``http.base_url`` / ``api_key``) is static. This lets an
    environment reference global variables so connection parameters and secrets
    live in one place (the Variables registry) instead of being baked into the
    env JSON. Best-effort: only runs when a ``${vars.`` token is actually present,
    and leaves the env unchanged if the variables can't be fetched.
    """
    try:
        if "${vars." not in json.dumps(env):
            return env
    except (TypeError, ValueError):
        return env
    try:
        doc = await _http_get_json(f"{SCENARIO_API_URL}/variables/global")
        variables = doc.get("variables") or {}
    except Exception:  # noqa: BLE001 — best effort
        return env
    return _interp(env, variables)


def _call_ids(scenario: dict) -> set[str]:
    """Scenario ids referenced by this scenario's ``call`` nodes."""
    out: set[str] = set()
    for s in scenario.get("steps", []):
        if s.get("kind") == "call":
            sid = (s.get("config") or {}).get("scenario_id")
            if sid:
                out.add(sid)
    return out


async def _attach_subflows(scenarios: list[dict]) -> None:
    """Fetch the definitions referenced by ``call`` nodes (transitively) and
    attach them to each scenario as ``subflows`` so the worker can run them."""
    needed: set[str] = set()
    for sc in scenarios:
        needed |= _call_ids(sc)
    fetched: dict[str, dict] = {}
    queue = list(needed)
    while queue:
        sid = queue.pop()
        if sid in fetched:
            continue
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/scenarios/{sid}")
        except Exception:  # noqa: BLE001 - best effort
            continue
        fetched[sid] = doc
        queue.extend(nid for nid in _call_ids(doc) if nid not in fetched)
    if fetched:
        for sc in scenarios:
            merged = {**fetched, **(sc.get("subflows") or {})}
            sc["subflows"] = merged


async def _resolve_dataset(req: CreateRunRequest) -> list[dict]:
    """Rows for a data-driven run: inline ``dataset`` or a named global table."""
    if req.dataset:
        return [r for r in req.dataset if isinstance(r, dict)]
    if req.data_table:
        try:
            tables = await _http_get_json(f"{SCENARIO_API_URL}/tables/runtime")
        except Exception:  # noqa: BLE001 - best effort
            return []
        return [r for r in (tables.get(req.data_table) or []) if isinstance(r, dict)]
    return []


def _expand_dataset(scenarios: list[dict], rows: list[dict]) -> list[dict]:
    """Fan a scenario out to one instance per data row, merging the row's columns
    into the instance's ``variables`` (so steps see ``${vars.<column>}``)."""
    import copy

    out: list[dict] = []
    for sc in scenarios:
        base_name = sc.get("name", "scenario")
        base_id = sc.get("id", "sc")
        base_vars = sc.get("variables") or {}
        for i, row in enumerate(rows, 1):
            clone = copy.deepcopy(sc)
            clone["variables"] = {**base_vars, **{str(k): v for k, v in row.items()}}
            clone["name"] = f"{base_name} · row {i}"
            clone["id"] = f"{base_id}#r{i}"
            out.append(clone)
    return out


async def _resolve_run_env(environment: dict | None, environment_name: str | None) -> dict:
    """Resolve an environment from explicit dict / named bundle / defaults, with
    ``${vars.*}`` interpolation applied (shared by /runs and /load-runs)."""
    if environment is not None:
        env = environment
    elif environment_name:
        env = await _load_env_by_name(environment_name)
    else:
        env = _load_defaults()[0]
    return await _interpolate_env_vars(env)


async def _resolve_run(req: CreateRunRequest) -> tuple[dict, list[dict], str]:
    """Resolve (environment, scenarios, label) from a create-run request."""
    env = await _resolve_run_env(req.environment, req.environment_name)

    if req.scenarios is not None:
        scs = req.scenarios
        label = req.label or "ad-hoc"
    elif req.scenario_ids:
        scs = await _fetch_scenarios_by_ids(req.scenario_ids)
        label = req.label or (
            scs[0].get("name", "1 case") if len(scs) == 1 else f"{len(scs)} cases"
        )
    elif req.project_id or req.set_id:
        ids = await _fetch_scenario_ids(req.project_id, req.set_id)
        scs = await _fetch_scenarios_by_ids(ids)
        label = req.label or ("set run" if req.set_id else "project run")
    else:
        scs = _load_defaults()[1]
        label = req.label or "examples"

    rows = await _resolve_dataset(req)
    if rows:
        scs = _expand_dataset(scs, rows)
        label = f"{label} × {len(rows)} rows"

    await _attach_subflows(scs)
    await _attach_tables(scs)
    await _attach_test_data(scs)
    await _attach_connections(env, scs, req.environment_name)
    await _attach_groups(env, scs)
    await _attach_step_environments(env, scs)
    # fleet discovery (ADR-0001): listeners hosted on the worker fleet bound
    # their planned ports on worker hosts — re-point matching adapters there.
    _attach_fleet_endpoints(env)
    return env, scs, label


def _make_debug_hook(rec: RunRecord, sink: StreamSink):
    async def hook(node_id: str, context: dict) -> None:
        sess = rec.debug
        if sess is None:
            return
        if not (sess.step_mode or node_id in sess.breakpoints or sess.pause_requested):
            return
        sess.pause_requested = False
        sess.paused = True
        sess.paused_node = node_id
        # Snapshot excludes the (potentially large) tables map and the engine's
        # internal generator context, and must be JSON-safe: the Redis backbone
        # json.dumps events strictly, so any stray non-serializable value (bytes
        # in a decoded payload, a dataclass…) would kill the run at the pause.
        snap = {k: v for k, v in context.items() if k not in ("tables", GEN_KEY)}
        snap = json.loads(json.dumps(snap, default=str))
        await sink.publish(RunEvent(DEBUG_PAUSED, rec.id, {"node": node_id, "context": snap}))
        sess.resume.clear()
        await sess.resume.wait()
        sess.paused = False
        await sink.publish(RunEvent(DEBUG_RESUMED, rec.id, {"node": node_id}))

    return hook


async def _run_engine(rec: RunRecord) -> None:
    sink = StreamSink(backbone)
    engine = WorkerEngine(rec.env, sink)
    debug_hook = _make_debug_hook(rec, sink) if rec.debug else None
    rec.status = "running"
    run_store.mark_running(rec.id)
    try:
        rec.summary = await engine.run_scenario_batch(
            rec.scenarios, rec.id, debug_hook=debug_hook,
        )
        # Stamp the runtime-resolved target endpoints onto the summary so a later
        # sign-off's provenance records what was actually tested against (ADR-0003).
        if isinstance(rec.summary, dict):
            rec.summary.setdefault("endpoints", _resolved_endpoints(rec.env))
        rec.status = rec.summary.get("status", "completed")
        run_store.finish(rec.id, rec.status, rec.summary)
    except asyncio.CancelledError:
        rec.status = "cancelled"
        run_store.finish(rec.id, "cancelled", rec.summary or {"status": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001
        rec.status = "error"
        rec.summary = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        run_store.finish(rec.id, "error", rec.summary)
        # The engine raised before emitting its own run.completed — tell the live
        # stream so subscribers (the portal) don't hang on "Waiting for results".
        try:
            await sink.publish(RunEvent(RUN_COMPLETED, rec.id,
                                        {"status": "error", "error": rec.summary["error"]}))
        except Exception as exc:  # noqa: BLE001
            log.warning("run %s: failed to publish terminal event; watchers may "
                        "hang until reconnect: %s", rec.id, exc)
    finally:
        # The run is no longer in-flight — drop it from the cross-replica
        # registry so cancel routing reflects reality, and record the outcome.
        await run_control.unregister(rec.id)
        RUNS_INFLIGHT.dec()
        RUNS_TOTAL.inc(status=rec.status)


# -- REST ---------------------------------------------------------------------

@app.post("/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest) -> CreateRunResponse:
    if req.requires_topology and not _topology_is_up(req.requires_topology):
        # Gate: the run depends on a participant-flow topology being live. 424
        # (Failed Dependency) mirrors the load-run provisioning pre-check.
        raise HTTPException(
            424, f"required network '{req.requires_topology}' is not running — "
            f"start it first (POST /network-flows/{req.requires_topology}/start)")

    env, scenarios, label = await _resolve_run(req)
    if not scenarios:
        raise HTTPException(400, "no scenarios to run")

    run_id = str(uuid.uuid4())
    rec = RunRecord(run_id, env, scenarios)
    if req.debug:
        # no breakpoints ⇒ step every node; with breakpoints ⇒ run to them
        rec.debug = DebugSession(req.breakpoints, step_mode=not req.breakpoints)
    RUNS[run_id] = rec
    env_name = env.get("name") or env.get("env") or "custom"
    run_store.create(
        run_id, env_name, len(scenarios), label=label,
        project_id=req.project_id or "",
    )
    await run_control.register(run_id, "run")
    RUNS_INFLIGHT.inc()
    rec.task = asyncio.create_task(_run_engine(rec))
    return CreateRunResponse(run_id=run_id, status=rec.status)


# -- participant-flow debug run (editor test-fire / step-through) --------------

async def _run_flow_debug(
    rec: RunRecord, flow: dict, request: dict, state: dict, downstream: dict,
) -> None:
    """Execute one participant flow against a sample request as a first-class
    run: step.result events stream live, the debug hook honours breakpoints /
    step / continue exactly like a scenario debug run, and the summary lands in
    the run registry shaped like a one-scenario run (so the editor's existing
    results plumbing — and GET /runs/{id} — just work)."""
    sink = StreamSink(backbone)
    debug_hook = _make_debug_hook(rec, sink) if rec.debug else None
    # Downstream dispatch mirrors FlowTraceMixin._execute_step: the flow's
    # outbound action/relay nodes call their picked connections for real.
    registry = AdapterRegistry({"adapters": downstream})

    async def _exec(target: str, action: str, payload: dict, assertions: list):
        adapter = await registry.get(target)
        sr = await adapter.execute(action, payload)
        if assertions:
            obj = dict(sr.response_payload or {})
            obj.setdefault("duration_ms", sr.duration_ms)
            sr.assertions = adapter.assert_response(obj, assertions)
        return sr

    flow_id = flow.get("id") or flow.get("name") or "flow"
    rec.status = "running"
    run_store.mark_running(rec.id)
    try:
        await sink.publish(RunEvent(RUN_STARTED, rec.id,
                                    {"scenario_count": 1, "flow": flow_id}))
        runner = FlowRunner(sink, run_id=rec.id, debug_hook=debug_hook)
        result = await runner.run_flow(
            flow, request, execute_step=_exec, state=state,
        )
        rec.summary = {
            "run_id": rec.id,
            "kind": "flow-debug",
            "status": result.status,
            "reply": result.reply,
            "scenarios": [{
                "scenario_id": flow_id,
                "name": flow.get("name", flow_id),
                "status": result.status,
                "steps": [o.__dict__ for o in result.steps],
            }],
        }
        rec.status = result.status
        run_store.finish(rec.id, rec.status, rec.summary)
        await sink.publish(RunEvent(RUN_COMPLETED, rec.id,
                                    {"status": result.status,
                                     "reply": result.reply}))
    except asyncio.CancelledError:
        rec.status = "cancelled"
        run_store.finish(rec.id, "cancelled", rec.summary or {"status": "cancelled"})
        # a debug Stop cancels the task (possibly parked at a breakpoint) — end
        # the live stream so the editor doesn't hang on "Waiting for results"
        with contextlib.suppress(Exception):
            await sink.publish(RunEvent(RUN_COMPLETED, rec.id, {"status": "cancelled"}))
        raise
    except Exception as exc:  # noqa: BLE001
        rec.status = "error"
        rec.summary = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        run_store.finish(rec.id, "error", rec.summary)
        with contextlib.suppress(Exception):
            await sink.publish(RunEvent(RUN_COMPLETED, rec.id,
                                        {"status": "error",
                                         "error": rec.summary["error"]}))
    finally:
        with contextlib.suppress(Exception):
            await registry.disconnect_all()
        await run_control.unregister(rec.id)
        RUNS_INFLIGHT.dec()
        RUNS_TOTAL.inc(status=rec.status)


@app.post("/flows/debug-run", response_model=CreateRunResponse)
async def create_flow_debug_run(req: FlowDebugRunRequest) -> CreateRunResponse:
    """Run a participant flow once against a sample trigger request.

    With ``debug: true`` (the default) the run pauses at breakpoints — or at
    every node when none are set — and is driven by the same
    ``/runs/{id}/debug/step|continue`` controls as a scenario debug run."""
    import httpx

    flow = req.flow
    if flow is None:
        if not req.flow_id:
            raise HTTPException(400, "provide 'flow' (inline) or 'flow_id'")
        try:
            flow = await _http_get_json(
                f"{SCENARIO_API_URL}/participant-flows/{req.flow_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    404, f"participant flow '{req.flow_id}' not found") from exc
            raise HTTPException(
                502, f"scenario-service HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, f"scenario-service unreachable: {exc}") from exc

    nodes = flow.get("nodes") or flow.get("steps") or []
    if not nodes:
        raise HTTPException(400, "flow has no nodes to run")
    # Outbound nodes pick their downstream via the Connection picker — re-point
    # each node's target at that connection (mirrors _launch_participant).
    for n in nodes:
        if n.get("kind", "action") in ("action", "relay", "proxy"):
            conn = (n.get("config") or {}).get("connection")
            if conn:
                n["target"] = conn
    downstream = await _participant_downstream(flow)

    run_id = str(uuid.uuid4())
    rec = RunRecord(run_id, {}, [flow])
    if req.debug:
        # no breakpoints ⇒ step every node; with breakpoints ⇒ run to them
        rec.debug = DebugSession(req.breakpoints, step_mode=not req.breakpoints)
    RUNS[run_id] = rec
    name = flow.get("name") or flow.get("id") or "flow"
    run_store.create(
        run_id, "flow", 1,
        label=req.label or f"{'flow debug' if req.debug else 'flow test'}: {name}",
    )
    await run_control.register(run_id, "run")
    RUNS_INFLIGHT.inc()
    rec.task = asyncio.create_task(
        _run_flow_debug(rec, flow, req.request, dict(req.state), downstream))
    return CreateRunResponse(run_id=run_id, status=rec.status)


@app.get("/runs")
async def list_runs() -> list[dict]:
    """Run history registry, newest first."""
    return run_store.list()


# declared before /runs/{run_id} so "trend" isn't matched as a run id
@app.get("/runs/trend")
async def runs_trend(days: int = 30, label: str | None = None) -> list[dict]:
    """Per-day regression-health trend (run pass/fail + scenario pass-rate)."""
    return run_store.trend(days=days, label=label)


# declared before /runs/{run_id} so "flakiness" isn't matched as a run id
@app.get("/runs/flakiness")
async def runs_flakiness(
    days: int = 30, label: str | None = None, min_runs: int = 3,
) -> list[dict]:
    """Scenarios that intermittently pass and fail (flaky), ranked by score."""
    return run_store.flakiness(days=days, label=label, min_runs=min_runs)


# -- load tests ---------------------------------------------------------------

class LoadRunRequest(BaseModel):
    """A load test: a transaction (resolved like a normal run) driven by a
    profile across ``workers`` separate processes.

    Provide the transaction with the same selectors as ``/runs``
    (``scenario_ids`` / ``scenarios`` / ``project_id`` / ``set_id``) plus an
    ``environment``; the first resolved scenario is the one driven under load.
    """

    # transaction selectors (subset of CreateRunRequest)
    scenario_ids: list[str] | None = None
    scenarios: list[dict] | None = None
    environment: dict | None = None
    environment_name: str | None = None
    label: str | None = None
    heartbeat_action: str | None = None
    #: weighted traffic mix — a blend of transaction types driven per-transaction
    #: by weight (e.g. ``[{scenario_id, weight: 80}, {scenario_id, weight: 15}]``).
    #: When set it supersedes ``scenario_ids``/``scenarios`` as the driven set;
    #: the first entry is the primary (used for the soak heartbeat + back-compat).
    mix: list[dict] | None = None
    #: provisioning pre-run: a scenario run *once* before the load starts (e.g.
    #: onboard terminals / register cards on the switch). If it fails the load is
    #: aborted — there is no point hammering a switch that rejected setup.
    provision_scenario_id: str | None = None
    provision_scenario: dict | None = None

    # profile
    type: str = "steady"               # steady | ramp | spike | soak
    duration_s: float = 60.0
    workers: int = 1
    target_tps: float = 0.0
    start_tps: float = 0.0
    end_tps: float = 0.0
    ramp_s: float = 0.0
    base_tps: float = 0.0
    spike_tps: float = 0.0
    spike_every_s: float = 0.0
    spike_duration_s: float = 0.0
    connections: int = 0
    heartbeat_interval_s: float = 30.0
    open_rate_per_s: float = 0.0
    max_in_flight_per_worker: int = 2000
    #: cap on outstanding (launched-but-unfinished) transactions per worker.
    #: ``0`` ⇒ cap at ``max_in_flight_per_worker`` (closed-loop: never queue more
    #: than the concurrency limit, so ``sent`` reflects what's actually
    #: dispatched and the run reconciles quickly). ``> 0`` ⇒ explicit cap.
    #: ``< 0`` ⇒ unbounded (legacy open-loop; inflates sent/pending under load).
    max_backlog_per_worker: int = 0
    #: grace period (seconds) after the send window closes to let outstanding
    #: transactions finish before the run terminates. ``> 0`` waits up to that
    #: long then aborts the remainder; ``< 0`` waits until every in-flight
    #: transaction completes (no timeout); ``0`` cuts immediately.
    drain_s: float = 30.0


async def _resolve_load_transaction(req: LoadRunRequest) -> tuple[dict, dict, str]:
    """Resolve (env, scenario, label) for a load run by reusing /runs logic."""
    cr = CreateRunRequest(
        scenario_ids=req.scenario_ids, scenarios=req.scenarios,
        environment=req.environment, environment_name=req.environment_name,
        label=req.label,
    )
    env, scs, label = await _resolve_run(cr)
    if not scs:
        raise HTTPException(400, "no transaction scenario to drive under load")
    return env, scs[0], label


async def _resolve_load_mix(
    req: LoadRunRequest,
) -> tuple[dict, list[dict], list[float], str]:
    """Resolve a weighted mix into (env, scenarios, weights, label).

    Each mix entry references a scenario by id or inlines one, with a weight.
    The environment is resolved once from the request's env selectors. Named
    card/terminal pools are resolved on the whole set (so every type in the
    blend draws real data). Falls back to the single-scenario path when no
    ``mix`` is supplied."""
    if not req.mix:
        env, scenario, label = await _resolve_load_transaction(req)
        return env, [scenario], [1.0], label

    # resolve the environment once via the shared run-resolution machinery
    env = await _resolve_run_env(req.environment, req.environment_name)

    ids = [e.get("scenario_id") for e in req.mix if e.get("scenario_id")]
    fetched = {sc["id"]: sc for sc in (await _fetch_scenarios_by_ids(ids) if ids else [])}

    scenarios: list[dict] = []
    weights: list[float] = []
    for entry in req.mix:
        sid = entry.get("scenario_id")
        sc = fetched.get(sid) if sid else entry.get("scenario")
        if sc is None:
            raise HTTPException(400, "each mix entry needs a scenario_id or inline scenario")
        scenarios.append(dict(sc))
        weights.append(float(entry.get("weight", 1.0)))

    await _attach_test_data(scenarios)
    label = req.label or "mix:" + "/".join(
        f"{s.get('id', s.get('name', '?'))}@{w:g}" for s, w in zip(scenarios, weights)
    )
    return env, scenarios, weights, label


async def _resolve_provision_scenario(req: LoadRunRequest) -> dict | None:
    """The provisioning scenario for a load run (by id or inline), or None."""
    if req.provision_scenario is not None:
        sc = dict(req.provision_scenario)
    elif req.provision_scenario_id:
        fetched = await _fetch_scenarios_by_ids([req.provision_scenario_id])
        sc = fetched[0] if fetched else None
    else:
        return None
    if sc is not None:
        await _attach_test_data([sc])
    return sc


async def _run_provisioning(env: dict, scenario: dict) -> dict:
    """Run the provisioning scenario once, returning its run summary.

    Uses a quiet (NullSink) functional run through the standard three-phase
    engine so onboarding behaves exactly like a normal scenario — including
    adapter health checks — just without streaming to the portal."""
    from worker.engine import NullSink

    engine = WorkerEngine(env, NullSink())
    return await engine.run_scenario_batch([scenario], f"provision-{uuid.uuid4()}")


@app.post("/load-runs")
async def create_load_run(req: LoadRunRequest) -> dict:
    try:
        env, scenarios, weights, label = await _resolve_load_mix(req)
    except HTTPException:
        raise  # already a clean, structured error (bad env, 404 scenario, ...)
    except Exception as exc:  # noqa: BLE001
        # Resolving the env/scenario reaches out to scenario-service. Any error
        # we didn't anticipate must not become a naked "Internal Server Error" —
        # the portal can only show the body, leaving the operator with no clue.
        log.exception("failed to resolve transaction for load run")
        raise HTTPException(
            502, f"could not resolve transaction for load run: {exc or type(exc).__name__}"
        ) from exc
    scenario = scenarios[0]
    profile = LoadProfile(
        type=req.type, duration_s=req.duration_s, workers=req.workers,
        scenario_id=scenario.get("id"), name=label,
        target_tps=req.target_tps, start_tps=req.start_tps, end_tps=req.end_tps,
        ramp_s=req.ramp_s, base_tps=req.base_tps, spike_tps=req.spike_tps,
        spike_every_s=req.spike_every_s, spike_duration_s=req.spike_duration_s,
        connections=req.connections, heartbeat_interval_s=req.heartbeat_interval_s,
        open_rate_per_s=req.open_rate_per_s,
        max_in_flight_per_worker=req.max_in_flight_per_worker,
        max_backlog_per_worker=req.max_backlog_per_worker,
        drain_s=req.drain_s,
    )
    try:
        profile.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Provisioning pre-run: onboard terminals / register cards once before the
    # load. If it fails, abort — hammering a switch that rejected setup is noise.
    try:
        provision = await _resolve_provision_scenario(req)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502, f"could not resolve provisioning scenario: {exc or type(exc).__name__}"
        ) from exc
    if provision is not None:
        try:
            psummary = await _run_provisioning(env, provision)
        except Exception as exc:  # noqa: BLE001
            log.exception("provisioning pre-run errored")
            raise HTTPException(
                502, f"provisioning pre-run errored: {exc or type(exc).__name__}"
            ) from exc
        if psummary.get("status") != "passed":
            raise HTTPException(
                424, f"provisioning pre-run failed ({psummary.get('status')}); load not started"
            )

    run_id = str(uuid.uuid4())
    run_store.create(run_id, env.get("name") or "custom", 1, label=f"load:{label}")
    run_store.mark_running(run_id)
    try:
        await load_coordinator.start(
            run_id, profile, env, scenario, heartbeat_action=req.heartbeat_action,
            scenarios=scenarios, weights=weights,
        )
    except Exception as exc:  # noqa: BLE001
        # Starting a run dispatches shards over the bus and registers the run in
        # the cross-replica control channel — both touch Redis when configured.
        # If that infrastructure is unreachable the start fails *after* we've
        # created the run record, which would otherwise (a) bubble up as an
        # unhandled 500 with a plain-text body, so the portal can only show a
        # generic "Failed to start load run", and (b) leave an orphan run stuck
        # in "running" forever. Roll the record back and return a structured
        # error so the real cause reaches the operator.
        log.exception("failed to start load run %s", run_id)
        try:
            run_store.finish(run_id, "failed", {"error": str(exc) or type(exc).__name__})
        except Exception:  # noqa: BLE001 — never mask the original failure
            log.exception("failed to roll back load run %s", run_id)
        detail = f"could not start load run: {exc or type(exc).__name__}"
        raise HTTPException(502, detail) from exc
    return {"run_id": run_id, "status": "running", "profile": profile.to_dict()}


def _load_run_card(rec: dict, live) -> dict:
    """Compact headline metrics for a load run's history card. Live runs read the
    coordinator's rolling snapshot; finished runs read their persisted summary."""
    running = live is not None and getattr(live, "status", None) == "running"
    if running:
        try:
            s = live.snapshot()
        except Exception:  # noqa: BLE001
            s = {}
    else:
        # ``run_store.list()`` rows omit the (heavy) summary doc — only ``get()``
        # carries it — so fetch the full record to populate finished-run metrics.
        s = rec.get("summary")
        if not s:
            full = run_store.get(rec.get("id"))
            s = (full or {}).get("summary") or {}
    lat = s.get("latency_ms") or {}
    received = int(s.get("received") or 0)
    errors = int(s.get("errors") or 0)
    done = received + errors
    return {
        "profile": s.get("profile") or (rec.get("summary") or {}).get("profile"),
        "tps": s.get("tps"),
        "target_tps": s.get("target_tps"),
        "p95": lat.get("p95"),
        "errors": errors,
        "sent": s.get("sent"),
        "received": received,
        "success_rate": round(received / done * 100, 2) if done else None,
        "duration_s": s.get("elapsed_s"),
    }


def _live_load_ids() -> set[str]:
    """Load runs an in-memory coordinator still owns (won't be reconciled)."""
    return {
        rid for rid, run in load_coordinator.runs.items()
        if getattr(run, "status", None) == "running"
    }


def _reconcile_stuck_load_runs() -> list[str]:
    """Close load runs stranded as ``running`` with no live owner (typically a
    prior orchestrator crash/restart). Returns the reconciled run ids."""
    closed = run_store.reconcile_orphans(_live_load_ids(), label_prefix="load:")
    if closed:
        log.info("reconciled %d stuck load run(s): %s", len(closed), closed)
    return closed


@app.post("/load-runs/reconcile")
async def reconcile_load_runs() -> dict:
    """Mark load runs stuck in ``running`` (no live coordinator owns them — e.g.
    after a restart) as ``interrupted`` so history stops showing them as live.
    Safe to call anytime: runs the coordinator is actively driving are skipped."""
    closed = _reconcile_stuck_load_runs()
    return {"reconciled": closed, "count": len(closed)}


@app.get("/load-runs")
async def list_load_runs() -> list[dict]:
    """Load-run history (newest first) so the portal can list and reopen runs.
    Load runs are tagged with a ``load:`` label prefix at creation. Each entry
    carries compact headline metrics (tps / p95 / success / duration / profile)
    so the history list is informative without opening every run."""
    out = []
    for r in run_store.list():
        if not str(r.get("label") or "").startswith("load:"):
            continue
        live = load_coordinator.get(r["id"])
        running = live is not None and getattr(live, "status", None) == "running"
        out.append({
            "run_id": r["id"],
            "label": (r.get("label") or "")[len("load:"):] or r["id"],
            "status": "running" if running else r.get("status"),
            "environment": r.get("environment"),
            "created_at": r.get("created_at"),
            "metrics": _load_run_card(r, live),
        })
    return out


@app.get("/load-runs/{run_id}")
async def get_load_run(run_id: str) -> dict:
    run = load_coordinator.get(run_id)
    if run is None:
        rec = run_store.get(run_id)
        if rec is None:
            raise HTTPException(404, "load run not found")
        return {"run_id": run_id, "status": rec.get("status"), "summary": rec.get("summary")}
    return run.snapshot()


def _resolve_load_summary(run_id: str) -> dict | None:
    """The roll-up for a load run: the persisted final summary if it has
    finished, else the live merged snapshot. ``None`` when the run is unknown."""
    rec = run_store.get(run_id)
    if rec is not None and rec.get("summary"):
        return rec["summary"]
    live = load_coordinator.get(run_id)
    if live is not None:
        return live.snapshot()
    return None


def _load_metrics(summary: dict | None) -> dict[str, float]:
    """Flatten a load summary to the scalar metrics the comparison diffs."""
    summary = summary or {}
    lat = summary.get("latency_ms") or {}
    received = float(summary.get("received", 0) or 0)
    errors = float(summary.get("errors", 0) or 0)
    total = received + errors
    stab = summary.get("tps_stability") or {}
    return {
        "p50": float(lat.get("p50", 0.0) or 0.0),
        "p95": float(lat.get("p95", 0.0) or 0.0),
        "p99": float(lat.get("p99", 0.0) or 0.0),
        "mean": float(lat.get("mean", 0.0) or 0.0),
        "max": float(lat.get("max", 0.0) or 0.0),
        "tps": float(summary.get("tps", 0.0) or 0.0),
        "error_rate": (errors / total) if total else 0.0,
        "errors": errors,
        "received": received,
        "tps_cv": float(stab.get("cv", 0.0) or 0.0),
    }


def _delta(metric: str, cur: float, base: float | None, *,
           higher_is_worse: bool, eps: float = 0.02) -> dict:
    """One metric's current/base/delta with a direction verdict.

    ``regressed``/``improved`` are decided against a relative ``eps`` dead-band
    so sub-2% jitter reads as ``flat`` rather than a false regression."""
    out: dict[str, Any] = {"metric": metric, "current": round(cur, 3),
                           "base": None if base is None else round(base, 3)}
    if base is None:
        out.update(delta=None, pct=None, direction="new", regressed=False, improved=False)
        return out
    delta = cur - base
    pct = (delta / base) if base else (0.0 if delta == 0 else None)
    out["delta"] = round(delta, 3)
    out["pct"] = None if pct is None else round(pct * 100, 1)
    rel = abs(delta) / base if base else (0.0 if delta == 0 else 1.0)
    if rel < eps:
        direction, regressed, improved = "flat", False, False
    else:
        worse = (delta > 0) if higher_is_worse else (delta < 0)
        direction = ("up" if delta > 0 else "down")
        regressed, improved = worse, not worse
    out.update(direction=direction, regressed=regressed, improved=improved)
    return out


def _compare_load_summaries(run_id: str, base_id: str | None,
                            cur: dict, base: dict | None) -> dict:
    cm = _load_metrics(cur)
    bm = _load_metrics(base) if base else None

    def d(metric: str, key: str, higher_is_worse: bool) -> dict:
        return _delta(metric, cm[key], None if bm is None else bm[key],
                      higher_is_worse=higher_is_worse)

    deltas = {
        "latency_p50": d("latency_p50", "p50", True),
        "latency_p95": d("latency_p95", "p95", True),
        "latency_p99": d("latency_p99", "p99", True),
        "error_rate": d("error_rate", "error_rate", True),
        "tps": d("tps", "tps", False),
        "tps_cv": d("tps_cv", "tps_cv", True),
    }
    # top error categories: union of both runs, sorted by current then base.
    cur_cats = (cur or {}).get("error_categories") or {}
    base_cats = (base or {}).get("error_categories") or {} if base else {}
    cats = []
    for name in set(cur_cats) | set(base_cats):
        c = int(cur_cats.get(name, 0))
        b = int(base_cats.get(name, 0))
        cats.append({"category": name, "current": c, "base": b, "delta": c - b})
    cats.sort(key=lambda r: (r["current"], r["base"]), reverse=True)

    regressions = sum(1 for v in deltas.values() if v.get("regressed"))
    improvements = sum(1 for v in deltas.values() if v.get("improved"))
    return {
        "run_id": run_id,
        "base_run_id": base_id,
        "base_found": base is not None,
        "current": cm,
        "base": bm,
        "deltas": deltas,
        "error_categories": cats[:20],
        "summary": {"regressions": regressions, "improvements": improvements},
    }


# declared before /load-runs/{run_id} would matter for a bare param, but the
# extra ``/compare`` segment keeps this distinct; grouped with the run reads.
@app.get("/load-runs/{run_id}/compare")
async def compare_load_run(run_id: str, to: str | None = None) -> dict:
    """Diff a load run against another (default: the previous like-for-like load
    run): p50/p95/p99 latency, error-rate, achieved TPS, TPS stability (cv), and
    a per-category error breakdown. Turns load testing from 'run a test' into
    'detect performance regressions'."""
    cur = _resolve_load_summary(run_id)
    if cur is None:
        raise HTTPException(404, "load run not found")
    base_id = to or run_store.previous_load(run_id)
    base = _resolve_load_summary(base_id) if base_id else None
    return _compare_load_summaries(run_id, base_id, cur, base)


@app.post("/load-runs/{run_id}/stop")
async def stop_load_run(run_id: str) -> dict:
    # 1) owned by this replica → stop directly.
    if await load_coordinator.stop(run_id):
        return {"run_id": run_id, "status": "stopping"}
    # 2) running on another replica → route the stop via the control channel.
    if await run_control.request_cancel(run_id):
        return {"run_id": run_id, "status": "stopping"}
    # 3) persisted record: finished → report status; still-"running" with no
    #    live owner → a stranded run (crash/restart), reconcile it now.
    rec = run_store.get(run_id)
    if rec is not None:
        if rec.get("status") in ("pending", "running"):
            run_store.finish(run_id, "interrupted", {
                "reconciled": True,
                "reason": "stopped: no live run owner (service restart or crash)",
                "previous_status": rec.get("status"),
            })
            return {"run_id": run_id, "status": "interrupted"}
        return {"run_id": run_id, "status": rec.get("status", "completed")}
    raise HTTPException(404, "load run not found or already finished")


class RetuneRequest(BaseModel):
    """Adjust a running load run's target rate without restarting it.

    Only the knobs relevant to the run's profile take effect: ``target_tps``
    (steady), ``base_tps``/``spike_tps`` (spike), ``end_tps`` (ramp)."""

    target_tps: float | None = None
    base_tps: float | None = None
    spike_tps: float | None = None
    end_tps: float | None = None


@app.post("/load-runs/{run_id}/retune")
async def retune_load_run(run_id: str, req: RetuneRequest) -> dict:
    params = {k: v for k, v in req.model_dump().items() if v is not None}
    if not params:
        raise HTTPException(400, "provide at least one target to re-tune")
    if await load_coordinator.retune(run_id, params):
        return {"run_id": run_id, "status": "retuned", "params": params}
    raise HTTPException(404, "load run not found or not running")


class ScaleRequest(BaseModel):
    #: target number of worker processes for this run's fleet
    desired: int


@app.post("/load-runs/{run_id}/scale")
async def scale_load_run(run_id: str, req: ScaleRequest) -> dict:
    """Scale a live run's worker fleet (dynamic path).

    Drives the configured provisioner to bring the run's fleet to ``desired``
    workers. When no provisioner is configured the response carries the manual
    command instead, so the portal can show the custom path."""
    if req.desired < 1:
        raise HTTPException(400, "desired must be >= 1")
    try:
        return {"run_id": run_id, **await load_coordinator.scale(run_id, req.desired)}
    except KeyError:
        raise HTTPException(404, "load run not found or not running on this replica")
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to scale load run %s", run_id)
        raise HTTPException(502, f"could not scale fleet: {exc}") from exc


# -- load-worker fleet --------------------------------------------------------
# Standalone `python -m worker.load_worker` processes heartbeat into the load
# bus while they hold a shard. These endpoints expose that presence so the
# portal can show the fleet and drain individual workers (or all of them).
# Workers are ephemeral and per-run: an empty list just means none are currently
# claiming shards (in-process / no external fleet up).

@app.get("/load-workers/provisioner")
async def get_worker_provisioner() -> dict:
    """Describe the worker provisioner so the portal can offer the right control:
    a live scale input when a backend is available, or the manual command (custom
    path) when it isn't."""
    caps = worker_provisioner.capabilities()
    caps["manual_command_template"] = manual_command("<RUN_ID>", os.environ.get("REDIS_URL"))
    return caps


@app.get("/load-workers")
async def list_load_workers() -> list[dict]:
    """Live load-worker fleet: one row per worker currently holding a shard."""
    try:
        return await load_bus.list_workers()
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to list load workers")
        raise HTTPException(502, f"could not list load workers: {exc}") from exc


@app.post("/load-workers/{worker_id}/stop")
async def stop_load_worker(worker_id: str) -> dict:
    """Drain one worker: it finishes in-flight work for its shard and exits.

    This signals the worker over the bus — it does not kill the OS process; a
    well-behaved worker observes the flag, stops sourcing new transactions, and
    deregisters itself."""
    try:
        await load_bus.signal_worker_stop(worker_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to signal stop for worker %s", worker_id)
        raise HTTPException(502, f"could not signal worker: {exc}") from exc
    return {"worker_id": worker_id, "status": "draining"}


@app.post("/load-workers/stop-all")
async def stop_all_load_workers() -> dict:
    """Drain every worker currently in the fleet."""
    try:
        workers = await load_bus.list_workers()
        for w in workers:
            await load_bus.signal_worker_stop(w["worker_id"])
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to drain load worker fleet")
        raise HTTPException(502, f"could not drain fleet: {exc}") from exc
    return {"status": "draining", "count": len(workers)}


# -- scheduled regression runs ------------------------------------------------

class ScheduleDraft(BaseModel):
    label: str
    #: "run" = functional regression run (CreateRunRequest body); "load" = load
    #: run (LoadRunRequest body). A "load" schedule on an interval with a soak
    #: profile is a *cyclic soak*: launch a soak, let it run its duration, then
    #: re-launch next interval — leak-hunting unattended (trend RSS in Grafana).
    kind: str = "run"
    request: dict[str, Any] = {}          # a CreateRunRequest / LoadRunRequest body
    interval_sec: int | None = None       # every N seconds
    daily_at: str | None = None           # "HH:MM" UTC
    enabled: bool = True


async def _trigger_schedule(sched: dict) -> str:
    """Launch the run a schedule describes; returns the new run id."""
    if sched.get("kind") == "load":
        lreq = LoadRunRequest(**sched.get("request", {}))
        if not lreq.label:
            lreq.label = sched.get("label") or "scheduled-load"
        resp = await create_load_run(lreq)
        return resp["run_id"]
    req = CreateRunRequest(**sched.get("request", {}))
    if not req.label:
        req.label = sched.get("label") or "scheduled"
    resp = await create_run(req)
    return resp.run_id


@app.get("/schedules")
async def list_schedules() -> list[dict]:
    return schedule_store.list()


@app.post("/schedules", status_code=201)
async def create_schedule(draft: ScheduleDraft) -> dict:
    if not draft.interval_sec and not draft.daily_at:
        raise HTTPException(400, "provide interval_sec or daily_at")
    if draft.kind not in ("run", "load"):
        raise HTTPException(400, "kind must be 'run' or 'load'")
    return schedule_store.create(draft.model_dump())


@app.post("/schedules/{sid}/run")
async def run_schedule_now(sid: str) -> dict:
    sched = schedule_store.get(sid)
    if sched is None:
        raise HTTPException(404, f"no schedule '{sid}'")
    run_id = await _trigger_schedule(sched)
    schedule_store.mark_ran(sid)
    return {"run_id": run_id}


@app.post("/schedules/{sid}/enabled")
async def set_schedule_enabled(sid: str, enabled: bool = True) -> dict:
    out = schedule_store.set_enabled(sid, enabled)
    if out is None:
        raise HTTPException(404, f"no schedule '{sid}'")
    return out


@app.delete("/schedules/{sid}", status_code=204, response_class=Response)
async def delete_schedule(sid: str) -> Response:
    try:
        schedule_store.delete(sid)
    except KeyError:
        raise HTTPException(404, f"no schedule '{sid}'")
    return Response(status_code=204)


async def _scheduler_loop(poll_sec: float = 30.0) -> None:
    """Trigger due schedules. Best-effort; one failure never stops the loop."""
    while True:
        try:
            for sched in schedule_store.due():
                try:
                    await _trigger_schedule(sched)
                    schedule_store.mark_ran(sched["id"])
                except Exception as exc:  # noqa: BLE001
                    log_scheduler_error(sched, exc)
        except Exception:  # noqa: BLE001
            # one bad poll must not kill the loop, but it must be visible —
            # a persistent failure here means schedules silently stop firing.
            log.warning("scheduler poll iteration failed", exc_info=True)
        await asyncio.sleep(poll_sec)


def log_scheduler_error(sched: dict, exc: Exception) -> None:
    import logging
    logging.getLogger("orchestrator").warning(
        "scheduled run '%s' failed: %s", sched.get("id"), exc)


@app.on_event("startup")
async def _start_scheduler() -> None:
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        asyncio.create_task(_scheduler_loop())


@app.on_event("startup")
async def _reconcile_stuck_runs_on_boot() -> None:
    # A fresh process owns no live runs, so any DB row still marked ``running``
    # was stranded by a previous crash/restart. Close them out so history and
    # the resilience load-run picker don't show phantom "running" entries.
    try:
        _reconcile_stuck_load_runs()
    except Exception:  # noqa: BLE001 — never block boot on housekeeping
        log.exception("startup load-run reconcile failed")


@app.on_event("startup")
async def _start_run_control() -> None:
    # Begin listening for cross-replica cancel requests (no-op in single process).
    try:
        await run_control.start()
    except Exception:  # noqa: BLE001 - control is best-effort; never block boot
        import logging
        logging.getLogger("orchestrator").exception("run_control failed to start")


@app.on_event("shutdown")
async def _stop_run_control() -> None:
    await run_control.aclose()


# -- host/responder simulators (run in-process; the orchestrator imports worker) --
#
# Two layers:
#   * ``simulator_store`` — saved, file-backed definitions (label + config +
#     enabled). CRUD lives under /simulator-configs; enabled ones auto-start.
#   * ``SIMULATORS``      — the responders actually listening right now, keyed by
#     the saved id (or a random id for an ad-hoc /simulators start). A 1s sampler
#     keeps a per-simulator ring buffer of throughput samples (SIM_SAMPLES) and
#     publishes live gauges for Grafana.

SIMULATORS: dict[str, TcpResponder] = {}

#: id -> recent throughput samples ({t, rps, received, faults}); capped.
SIM_SAMPLES: dict[str, list[dict]] = {}
#: id -> last cumulative ``received`` (to derive rps between sampler ticks).
_SIM_PREV: dict[str, int] = {}
SIM_SAMPLE_CAP = 180  # ~6 min at 2s; the live charts only need a rolling window


class SimulatorDraft(BaseModel):
    label: str = "simulator"
    config: dict[str, Any]                 # a TcpResponder config


class SavedSimulatorDraft(BaseModel):
    label: str = "simulator"
    config: dict[str, Any]
    enabled: bool = False


class SavedSimulatorPatch(BaseModel):
    label: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


def _sim_info(sid: str, label: str, responder: TcpResponder) -> dict:
    buf = SIM_SAMPLES.get(sid) or []
    return {
        "id": sid,
        "label": label,
        "protocol": responder.protocol,
        "port": responder.port,
        "rules": len(responder.rules),
        "received": len(responder.received),
        "chaos": bool(getattr(responder, "global_chaos", None))
        or any("chaos" in (r.get("respond") or {}) for r in responder.rules),
        #: a timed chaos storm is currently driving this simulator's fault dial.
        "storm": sid in CHAOS_STORMS,
        "faults": getattr(responder, "faults", 0),
        "invalid": getattr(responder, "invalid", 0),
        "rps": buf[-1]["rps"] if buf else 0,
        #: whether a saved config of the same id backs this running responder.
        "saved": simulator_store.has(sid),
        #: a capture-capable transparent proxy (kind:"proxy") — drives the
        #: portal's Capture controls.
        "proxy": hasattr(responder, "capture_rows"),
        #: messages fired at this simulator from the Playground (ADR-0007) —
        #: lets reports flag stats contamination during certification runs.
        "playground_hits": PLAYGROUND_HITS.get(sid, 0),
    }


def _drop_sim_runtime(sid: str, label: str) -> None:
    """Forget a stopped simulator's samples and drop its live gauges."""
    SIM_SAMPLES.pop(sid, None)
    _SIM_PREV.pop(sid, None)
    for g in (SIMULATOR_UP, SIMULATOR_MESSAGES, SIMULATOR_RPS, SIMULATOR_FAULTS):
        try:
            g.remove(sim_id=sid, label=label)
        except Exception:  # noqa: BLE001 — series may not exist yet
            pass


def _responder_for(config: dict) -> TcpResponder:
    """Pick the responder class for a saved-simulator config. ``protocol``
    ``payshield`` (or ``kind`` ``payshield``) builds the payShield 10K HSM
    simulator; everything else is the rules-driven TcpResponder."""
    # The transparent proxy keeps ``protocol`` for the wire dialect (decode), so
    # it is selected by an explicit ``kind: "proxy"`` checked before the
    # protocol-based dispatch below.
    if str(config.get("kind", "")).lower() == "proxy":
        from worker.adapters.tcp.proxy import TcpProxy

        return TcpProxy(dict(config))
    kind = str(config.get("protocol") or config.get("kind") or "").lower()
    if kind == "payshield":
        cls: type[TcpResponder] = PayShieldSimulator
    elif kind == "visa":
        from worker.adapters.scheme.visa import VisaSimulator

        cls = VisaSimulator
    elif kind == "cybersource":
        from worker.adapters.scheme.cybersource import CyberSourceSimulator

        # HTTP gateway simulator — not a TcpResponder subclass, but exposes the
        # same duck-typed surface (start/stop/port/protocol/rules/stats/peers).
        cls = CyberSourceSimulator  # type: ignore[assignment]
    elif kind == "nats":
        from worker.adapters.nats.responder import NatsResponder

        # Broker-mediated subscriber simulator (ADR-0006) — binds no port,
        # subscribes to subjects; same duck-typed responder surface.
        cls = NatsResponder  # type: ignore[assignment]
    else:
        cls = TcpResponder
    return cls(dict(config))


#: Default registry message-format bound per simulator protocol when the config
#: doesn't pin one (and supplies no inline ``fields``). Lets scheme simulators
#: source their DE table from the Message Format registry instead of a private
#: hard-coded copy.
_DEFAULT_SIM_FORMAT: dict[str, str] = {"visa": "visa-base1"}


async def _resolve_simulator_config(config: dict) -> dict:
    """Resolve a simulator's bound dialect.

    When a config carries ``message_format_id``, fetch that Message Format from
    scenario-service and inject its DE table (``fields``) and presence matrix
    (``validate.presence``) so the simulator decodes *and validates* inbound
    messages against the same registered dialect a client packs with. Inline
    ``fields`` / ``validate`` always win; binding a format turns validation on
    in ``warn`` mode unless the config sets its own ``validate.mode``.

    Best-effort: if the format can't be resolved the original config is used.
    """
    fid = config.get("message_format_id")
    if not fid and not config.get("fields"):
        # Scheme simulators bind their canonical registry dialect by default, so
        # they decode/validate against the same format clients pack with. Inline
        # ``fields`` or an explicit ``message_format_id`` always take precedence.
        proto = str(config.get("protocol") or config.get("kind") or "").lower()
        fid = _DEFAULT_SIM_FORMAT.get(proto)
    if not fid:
        return config
    try:
        fmt = await _http_get_json(f"{SCENARIO_API_URL}/formats/{fid}")
    except Exception:  # noqa: BLE001 — never block a start on a lookup miss
        log.warning("simulator: could not resolve message format '%s'", fid, exc_info=True)
        return config
    cfg = dict(config)
    definition = (fmt or {}).get("definition") or {}
    if definition.get("fields") and not cfg.get("fields"):
        cfg["fields"] = definition["fields"]
    if presence := definition.get("presence"):
        validate = dict(cfg.get("validate") or {})
        validate.setdefault("presence", presence)
        validate.setdefault("mode", "warn")
        cfg["validate"] = validate
    return cfg


async def _start_responder(sid: str, label: str, config: dict) -> TcpResponder:
    """Boot a responder for ``config`` and register it under ``sid``."""
    config = await _resolve_simulator_config(config)
    responder = _responder_for(config)
    try:
        await responder.start()
    except Exception as exc:  # noqa: BLE001 - bad config / port in use
        raise HTTPException(400, f"could not start simulator: {exc}")
    responder._label = label  # type: ignore[attr-defined]
    SIMULATORS[sid] = responder
    _SIM_PREV[sid] = 0
    SIM_SAMPLES[sid] = []
    return responder


# -- running-instance endpoints (ad-hoc + shared lifecycle) --------------------

@app.get("/peers")
async def list_peers() -> dict:
    """Live Sessions: every inbound client connected *to* a listener we host —
    across ad-hoc/saved simulators and participant-flow responders. Each row is
    one established socket (remote address, age, frames, idle). The ``outbound``
    side is the upstream sockets a transparent proxy (``kind: "proxy"``) opens
    to the real host it relays to, plus every socket an in-process engine
    adapter holds open toward a target (TCP adapters register on connect —
    ``worker/adapters/socket_registry``). Sockets inside separate load-worker
    processes are deliberately not reported (fleet-scale socket inventories
    are a different problem)."""
    inbound: list[dict] = []
    outbound: list[dict] = []

    for sid, r in SIMULATORS.items():
        label = getattr(r, "_label", sid)
        for p in r.peers():
            inbound.append({
                **p,
                "direction": "inbound",
                "source": "simulator",
                "source_id": sid,
                "label": label,
                "protocol": r.protocol,
                "local_port": r.port,
            })
        # Proxies expose their upstream leg via upstream_peers(); other
        # simulators don't have the method (duck-typed).
        up = getattr(r, "upstream_peers", None)
        if callable(up):
            for p in up():
                outbound.append({
                    **p,
                    "direction": "outbound",
                    "source": "proxy",
                    "source_id": sid,
                    "label": label,
                    "protocol": r.protocol,
                    "local_port": r.port,
                })

    for pid, r in PARTICIPANTS.items():
        flow_id = getattr(r, "_flow_id", pid)
        topo = getattr(r, "_topology_run", None)
        for p in r.peers():
            inbound.append({
                **p,
                "direction": "inbound",
                "source": "participant",
                "source_id": pid,
                "label": flow_id,
                "topology_run": topo,
                "protocol": r.protocol,
                "local_port": r.port,
            })

    # Engine adapters (this process): sockets the run/flow engine itself holds
    # open toward hosts — TCP adapters register on connect, unregister on drop.
    # Covers normal runs and in-process participant-flow downstreams; sockets
    # inside separate load-worker processes are deliberately not reported.
    for s in adapter_socket_snapshot():
        outbound.append({
            "peer": f"{s['host']}:{s['port']}",
            "since": round(s["opened_at"], 3),
            "connected_s": s["age_sec"],
            "direction": "outbound",
            "source": "engine",
            "source_id": s.get("local") or "",
            "label": s.get("owner") or s.get("adapter") or "engine adapter",
            "protocol": s.get("adapter") or "tcp",
            "local_port": None,
        })

    inbound.sort(key=lambda x: (x.get("local_port") or 0, x.get("since") or 0))
    outbound.sort(key=lambda x: (x.get("local_port") or 0, x.get("since") or 0))
    peer_ips = {x["peer"].rsplit(":", 1)[0] for x in inbound + outbound}
    return {
        "inbound": inbound,
        "outbound": outbound,
        "counts": {
            "inbound": len(inbound),
            "outbound": len(outbound),
            "listeners": len(SIMULATORS) + len(PARTICIPANTS),
            "peer_ips": len(peer_ips),
        },
    }


@app.get("/simulators")
async def list_simulators() -> list[dict]:
    return [_sim_info(sid, getattr(r, "_label", sid), r) for sid, r in SIMULATORS.items()]


@app.post("/simulators", status_code=201)
async def start_simulator(draft: SimulatorDraft) -> dict:
    sid = uuid.uuid4().hex[:8]
    responder = await _start_responder(sid, draft.label, draft.config)
    return _sim_info(sid, draft.label, responder)


@app.get("/simulators/{sid}")
async def get_simulator(sid: str) -> dict:
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    info = _sim_info(sid, getattr(r, "_label", sid), r)
    info["log"] = r.received[-50:]  # recent decoded requests
    return info


@app.get("/simulators/{sid}/metrics")
async def simulator_metrics(sid: str) -> dict:
    """Live dashboard payload: cumulative stats, breakdowns + a throughput
    timeline (the same shape the portal's load page consumes)."""
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    info = _sim_info(sid, getattr(r, "_label", sid), r)
    info["stats"] = r.stats()
    info["samples"] = SIM_SAMPLES.get(sid) or []
    info["log"] = r.received[-50:]
    return info


@app.post("/simulators/{sid}/clear")
async def clear_simulator(sid: str) -> dict:
    """Reset a running simulator's live counters, traffic log and timeline —
    keeps it listening."""
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    r.reset_stats()
    _SIM_PREV[sid] = 0
    SIM_SAMPLES[sid] = []
    return _sim_info(sid, getattr(r, "_label", sid), r)


# -- live chaos control ---------------------------------------------------------
#
# Chaos was config-time only: set a ``chaos`` block, (re)start the simulator.
# These endpoints make it a *dial* on a RUNNING simulator — turn faults on, tune
# them, or run a timed "storm" (a phased sequence: outage, brownout, flapping)
# that restores the baseline when it ends. Nothing here touches saved configs;
# it drives the live responder via ``set_chaos()``.

#: chaos keys accepted from the API (mirrors worker.adapters.tcp.chaos schema).
_CHAOS_KEYS = {
    "seed", "drop_pct", "drop", "latency_ms",
    "malformed_pct", "malformed", "malformed_mode",
    "partial_pct", "partial", "partial_bytes",
}

#: sid -> {"task": asyncio.Task, "state": dict, "baseline": dict}
CHAOS_STORMS: dict[str, dict] = {}


class ChaosPhase(BaseModel):
    #: how long this phase runs before the next one starts.
    duration_s: float
    #: chaos block applied for the phase ({} = calm phase, e.g. the "up" half
    #: of a flap cycle).
    chaos: dict[str, Any] = {}
    #: optional label surfaced in the storm status ("outage", "brownout", …).
    label: str | None = None


class ChaosStormRequest(BaseModel):
    phases: list[ChaosPhase]
    #: run the phase list this many times (flapping = 2 phases × N cycles).
    repeat: int = 1


def _chaos_capable(sid: str):
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    if not hasattr(r, "set_chaos"):
        raise HTTPException(400, f"simulator '{sid}' does not support chaos injection")
    return r


def _validate_chaos(cfg: dict) -> dict:
    unknown = set(cfg) - _CHAOS_KEYS
    if unknown:
        raise HTTPException(400, f"unknown chaos keys: {sorted(unknown)}")
    mode = cfg.get("malformed_mode")
    if mode is not None and mode not in CHAOS_MALFORMED_MODES:
        raise HTTPException(
            400, f"malformed_mode must be one of {list(CHAOS_MALFORMED_MODES)}"
        )
    return cfg


def _storm_status(sid: str) -> dict | None:
    entry = CHAOS_STORMS.get(sid)
    if entry is None:
        return None
    state = dict(entry["state"])
    started = state.get("phase_started_at") or 0
    dur = state.get("phase_duration_s") or 0
    state["phase_remaining_s"] = max(0.0, round(dur - (time.time() - started), 1))
    return state


async def _run_storm(sid: str, phases: list[ChaosPhase], repeat: int, baseline: dict) -> None:
    """Apply each phase's chaos in sequence (× repeat), then restore baseline."""
    entry = CHAOS_STORMS.get(sid)
    try:
        for cycle in range(max(1, repeat)):
            for i, phase in enumerate(phases):
                r = SIMULATORS.get(sid)
                if r is None or entry is not CHAOS_STORMS.get(sid):
                    return  # simulator stopped or storm replaced/cancelled
                r.set_chaos(phase.chaos)
                entry["state"].update(
                    {
                        "phase_index": i,
                        "cycle": cycle + 1,
                        "phase": phase.label or (phase.chaos and "fault") or "calm",
                        "phase_duration_s": phase.duration_s,
                        "phase_started_at": time.time(),
                    }
                )
                await asyncio.sleep(max(0.0, phase.duration_s))
    except asyncio.CancelledError:
        raise
    finally:
        # restore the pre-storm chaos block (unless the sim is gone or a new
        # storm already took over this slot).
        if entry is CHAOS_STORMS.get(sid):
            CHAOS_STORMS.pop(sid, None)
            r = SIMULATORS.get(sid)
            if r is not None:
                with contextlib.suppress(Exception):
                    r.set_chaos(baseline)


def _cancel_storm(sid: str, *, restore: bool = True) -> bool:
    """Stop a running storm; optionally put the baseline chaos back."""
    entry = CHAOS_STORMS.pop(sid, None)
    if entry is None:
        return False
    entry["task"].cancel()
    if restore:
        r = SIMULATORS.get(sid)
        if r is not None:
            with contextlib.suppress(Exception):
                r.set_chaos(entry["baseline"])
    return True


@app.get("/simulators/{sid}/chaos")
async def get_simulator_chaos(sid: str) -> dict:
    """The live chaos dial: current global block, fault count, storm status."""
    r = _chaos_capable(sid)
    return {
        "id": sid,
        "label": getattr(r, "_label", sid),
        "chaos": dict(getattr(r, "global_chaos", {}) or {}),
        "faults": getattr(r, "faults", 0),
        "storm": _storm_status(sid),
    }


@app.put("/simulators/{sid}/chaos")
async def set_simulator_chaos(sid: str, chaos: dict[str, Any]) -> dict:
    """Apply a chaos block to a RUNNING simulator — effective on the next
    reply, no restart. An empty body turns chaos off. Cancels any storm (a
    manual dial move takes precedence over the scheduled sequence)."""
    r = _chaos_capable(sid)
    _validate_chaos(chaos)
    _cancel_storm(sid, restore=False)
    applied = r.set_chaos(chaos)
    return {"id": sid, "chaos": applied, "storm": None}


@app.post("/simulators/{sid}/chaos/storm", status_code=201)
async def start_chaos_storm(sid: str, req: ChaosStormRequest) -> dict:
    """Run a timed chaos sequence against a running simulator: e.g. a 30s
    full outage, a 2-minute brownout ramp, or N flap cycles. The pre-storm
    chaos block is restored when the storm ends or is cancelled."""
    r = _chaos_capable(sid)
    if not req.phases:
        raise HTTPException(400, "storm needs at least one phase")
    for p in req.phases:
        _validate_chaos(p.chaos)
        if p.duration_s <= 0:
            raise HTTPException(400, "phase duration_s must be > 0")
    _cancel_storm(sid, restore=False)
    baseline = dict(getattr(r, "global_chaos", {}) or {})
    state = {
        "active": True,
        "phases_total": len(req.phases),
        "repeat": max(1, req.repeat),
        "started_at": time.time(),
        "phase_index": -1,
        "cycle": 0,
        "phase": "",
        "phase_duration_s": 0.0,
        "phase_started_at": time.time(),
    }
    entry: dict = {"state": state, "baseline": baseline}
    CHAOS_STORMS[sid] = entry
    entry["task"] = asyncio.create_task(_run_storm(sid, req.phases, req.repeat, baseline))
    return {"id": sid, "storm": _storm_status(sid)}


@app.delete("/simulators/{sid}/chaos/storm", status_code=204, response_class=Response)
async def cancel_chaos_storm(sid: str) -> Response:
    """Cancel a running storm and restore the pre-storm chaos block."""
    if not _cancel_storm(sid, restore=True):
        raise HTTPException(404, f"no storm running on simulator '{sid}'")
    return Response(status_code=204)


# -- resilience runs (chaos + observation → pass/fail certificate) -------------
#
# A resilience run ties the chaos dial to a live load run and scores whether the
# CLIENT survived: it samples the simulator's injected faults and the load run's
# client success/latency each second across three stages — baseline (calm),
# storm (chaos injected), recovery (chaos lifted) — then grades the result with
# resilience.score_resilience(). This is the resilience analog of certification:
# "we injected faults" becomes "here is your pass/fail resilience certificate".

#: rid -> live resilience run state.
RESILIENCE_RUNS: dict[str, dict] = {}
RESILIENCE_SAMPLE_S = 1.0


class ResilienceRunRequest(BaseModel):
    #: the simulator to make misbehave (must be running + chaos-capable).
    target_sim_id: str
    #: an already-running load run whose client metrics we observe. Start a load
    #: run first (POST /load-runs), then point a resilience run at it.
    load_run_id: str
    #: chaos storm played during the storm stage (same shape as /chaos/storm).
    storm: ChaosStormRequest
    #: calm observation before the storm (to learn the client's normal rate).
    baseline_s: float = 10.0
    #: observation after the storm lifts (to check the client recovers).
    recovery_s: float = 15.0
    label: str | None = None
    #: optional scoring gate overrides (resilience.ResilienceThresholds).
    thresholds: dict[str, Any] | None = None


def _resilience_sample(sid: str, load_run_id: str, stage: str, t: float) -> dict:
    """One per-second observation: simulator faults + client outcomes."""
    r = SIMULATORS.get(sid)
    sim = r.stats() if r is not None else {}
    live = load_coordinator.get(load_run_id)
    snap = live.snapshot() if live is not None else {}
    lat = (snap.get("latency_ms") or {})
    return {
        "t": round(t, 1),
        "stage": stage,
        "sim_received": int(sim.get("received", 0) or 0),
        "sim_faults": int(sim.get("faults", 0) or 0),
        "client_received": int(snap.get("received", 0) or 0),
        "client_errors": int(snap.get("errors", 0) or 0),
        "p95_ms": float(lat.get("p95", 0.0) or 0.0),
        "tps": float(snap.get("tps", 0.0) or 0.0),
    }


async def _run_resilience(rid: str, req: ResilienceRunRequest) -> None:
    """Drive the baseline → storm → recovery lifecycle, sampling each second,
    then score and persist the report."""
    entry = RESILIENCE_RUNS[rid]
    sid = req.target_sim_id
    start = time.time()

    async def sample_for(stage: str, seconds: float) -> None:
        end = time.time() + seconds
        # always take at least the boundary samples so a stage has a first+last
        entry["samples"].append(_resilience_sample(sid, req.load_run_id, stage, time.time() - start))
        while time.time() < end:
            if entry is not RESILIENCE_RUNS.get(rid):
                return  # cancelled
            await asyncio.sleep(min(RESILIENCE_SAMPLE_S, max(0.0, end - time.time())))
            entry["samples"].append(_resilience_sample(sid, req.load_run_id, stage, time.time() - start))

    try:
        entry["stage"] = STAGE_BASELINE
        await sample_for(STAGE_BASELINE, req.baseline_s)

        entry["stage"] = STAGE_STORM
        # launch the chaos storm; storm-stage duration = the storm's own length.
        r = SIMULATORS.get(sid)
        if r is not None and hasattr(r, "set_chaos"):
            baseline_chaos = dict(getattr(r, "global_chaos", {}) or {})
            state = {
                "active": True, "phases_total": len(req.storm.phases),
                "repeat": max(1, req.storm.repeat), "started_at": time.time(),
                "phase_index": -1, "cycle": 0, "phase": "",
                "phase_duration_s": 0.0, "phase_started_at": time.time(),
            }
            storm_entry = {"state": state, "baseline": baseline_chaos}
            CHAOS_STORMS[sid] = storm_entry
            storm_entry["task"] = asyncio.create_task(
                _run_storm(sid, req.storm.phases, req.storm.repeat, baseline_chaos)
            )
        storm_s = sum(p.duration_s for p in req.storm.phases) * max(1, req.storm.repeat)
        await sample_for(STAGE_STORM, storm_s)

        # lift the storm and watch the client recover.
        _cancel_storm(sid, restore=True)
        entry["stage"] = STAGE_RECOVERY
        await sample_for(STAGE_RECOVERY, req.recovery_s)

        report = score_resilience(entry["samples"], req.thresholds)
        entry["report"] = report
        entry["status"] = "completed"
        entry["stage"] = "done"
        entry["finished_at"] = time.time()
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        _cancel_storm(sid, restore=True)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("resilience run %s failed", rid)
        entry["status"] = "failed"
        entry["error"] = str(exc) or type(exc).__name__
        _cancel_storm(sid, restore=True)


def _resilience_view(rid: str) -> dict:
    e = RESILIENCE_RUNS.get(rid)
    if e is None:
        raise HTTPException(404, f"no resilience run '{rid}'")
    return {
        "id": rid,
        "label": e.get("label"),
        "status": e.get("status"),
        "stage": e.get("stage"),
        "target_sim_id": e.get("target_sim_id"),
        "load_run_id": e.get("load_run_id"),
        "created_at": e.get("created_at"),
        "finished_at": e.get("finished_at"),
        "samples": e.get("samples", []),
        "report": e.get("report"),
        "error": e.get("error"),
    }


@app.get("/resilience/runs")
async def list_resilience_runs() -> list[dict]:
    """Resilience-run history (newest first), each with its headline verdict."""
    out = []
    for rid, e in sorted(
        RESILIENCE_RUNS.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True
    ):
        rep = e.get("report") or {}
        out.append({
            "id": rid, "label": e.get("label"), "status": e.get("status"),
            "stage": e.get("stage"), "target_sim_id": e.get("target_sim_id"),
            "created_at": e.get("created_at"),
            "score": rep.get("score"), "grade": rep.get("grade"),
            "verdict": rep.get("verdict"),
        })
    return out


@app.post("/resilience/runs", status_code=201)
async def start_resilience_run(req: ResilienceRunRequest) -> dict:
    """Start a resilience run: observe a live load run's client while a chaos
    storm hits a target simulator, then score how well the client survived."""
    r = _chaos_capable(req.target_sim_id)  # 404/400 if not running / not chaos-capable
    for p in req.storm.phases:
        _validate_chaos(p.chaos)
        if p.duration_s <= 0:
            raise HTTPException(400, "storm phase duration_s must be > 0")
    if not req.storm.phases:
        raise HTTPException(400, "storm needs at least one phase")
    if load_coordinator.get(req.load_run_id) is None:
        raise HTTPException(
            400,
            f"load run '{req.load_run_id}' is not running; start a load run first "
            "and point the resilience run at it",
        )
    rid = uuid.uuid4().hex[:8]
    label = req.label or f"resilience · {getattr(r, '_label', req.target_sim_id)}"
    RESILIENCE_RUNS[rid] = {
        "label": label, "status": "running", "stage": "starting",
        "target_sim_id": req.target_sim_id, "load_run_id": req.load_run_id,
        "created_at": time.time(), "finished_at": None,
        "samples": [], "report": None, "error": None,
    }
    RESILIENCE_RUNS[rid]["task"] = asyncio.create_task(_run_resilience(rid, req))
    return _resilience_view(rid)


@app.get("/resilience/runs/{rid}")
async def get_resilience_run(rid: str) -> dict:
    """Live/finished resilience run — samples + the scored report when done."""
    return _resilience_view(rid)


@app.delete("/resilience/runs/{rid}", status_code=204, response_class=Response)
async def cancel_resilience_run(rid: str) -> Response:
    """Cancel a running resilience run and lift any storm it started."""
    e = RESILIENCE_RUNS.get(rid)
    if e is None:
        raise HTTPException(404, f"no resilience run '{rid}'")
    task = e.get("task")
    if task is not None and not task.done():
        task.cancel()
    _cancel_storm(e.get("target_sim_id"), restore=True)
    e["status"] = "cancelled"
    return Response(status_code=204)


# -- proxy capture (ADR-0002 stage 2) -----------------------------------------

class SaveCaptureRequest(BaseModel):
    name: str
    project_id: str | None = None


def _proxy_or_404(sid: str):
    """The running simulator ``sid`` if it is a capture-capable proxy."""
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    if not hasattr(r, "capture_rows"):
        raise HTTPException(400, f"simulator '{sid}' is not a proxy")
    return r


@app.get("/simulators/{sid}/capture")
async def get_capture(sid: str) -> dict:
    """Recorded (redacted) frames a proxy has relayed."""
    r = _proxy_or_404(sid)
    rows = r.capture_rows()
    return {
        "id": sid,
        "label": getattr(r, "_label", sid),
        "enabled": r.capture.enabled,
        "mode": r.capture.mode,
        "count": len(rows),
        "dropped": r.capture.dropped,
        "skipped": getattr(r.capture, "skipped", 0),
        "rows": rows,
    }


@app.delete("/simulators/{sid}/capture", status_code=204)
async def clear_capture(sid: str) -> Response:
    r = _proxy_or_404(sid)
    r.clear_capture()
    return Response(status_code=204)


@app.post("/simulators/{sid}/capture/save-as-scenario", status_code=201)
async def save_capture_as_scenario(sid: str, req: SaveCaptureRequest) -> dict:
    """Turn a captured session into a scenario stored in scenario-service."""
    r = _proxy_or_404(sid)
    draft = r.capture_scenario_draft(req.name)
    if not draft.get("steps"):
        raise HTTPException(400, "capture has no request/response pairs to save")
    created = await _http_post_json(
        f"{SCENARIO_API_URL}/scenarios",
        {"scenario": draft, "comment": f"captured from proxy {sid}",
         "project_id": req.project_id},
    )
    return {
        "scenario_id": created.get("id"),
        "name": draft["name"],
        "steps": len(draft["steps"]),
    }


@app.delete("/simulators/{sid}", status_code=204, response_class=Response)
async def stop_simulator(sid: str) -> Response:
    """Stop a running simulator and remove it from the active list. A saved
    config of the same id is left intact (this is the "stop + remove" action)."""
    _cancel_storm(sid, restore=False)  # a stopping sim doesn't need its dial back
    r = SIMULATORS.pop(sid, None)
    if r is None:
        raise HTTPException(404, f"no simulator '{sid}'")
    await r.stop()
    _drop_sim_runtime(sid, getattr(r, "_label", sid))
    return Response(status_code=204)


@app.post("/simulators/{sid}/save", status_code=201)
async def save_running_simulator(sid: str, enabled: bool = False) -> dict:
    """Persist a *running* (ad-hoc) simulator as a saved config, so it appears in
    the saved registry / portal Simulators page and — when ``enabled`` — auto-
    starts on boot. The live responder is re-keyed to the new saved id so it
    keeps running and shows as running. Idempotent-ish: a simulator already
    backed by a saved config of the same id is returned unchanged."""
    r = SIMULATORS.get(sid)
    if r is None:
        raise HTTPException(404, f"no running simulator '{sid}'")
    if simulator_store.has(sid):
        return _saved_info(simulator_store.get(sid))
    label = getattr(r, "_label", sid)
    saved = simulator_store.create(
        {"label": label, "config": dict(getattr(r, "config", {})), "enabled": enabled}
    )
    new_id = saved["id"]
    if new_id != sid:
        # move the live responder + its runtime metrics onto the saved id so the
        # saved card shows as running (and the old ad-hoc id is forgotten).
        SIMULATORS.pop(sid, None)
        SIMULATORS[new_id] = r
        SIM_SAMPLES[new_id] = SIM_SAMPLES.pop(sid, [])
        _SIM_PREV[new_id] = _SIM_PREV.pop(sid, 0)
        _drop_sim_runtime(sid, label)
    return _saved_info(simulator_store.get(new_id))


# -- participant flows (listening, reactive stand-ins run in-process) ----------
#
# Phase 1: start/stop/list a participant flow as a listening service. The flow
# definition + its inbound connection come from the scenario-service; the worker
# FlowResponder reuses the same wire as TcpResponder but computes each reply by
# walking the flow graph (trigger -> logic -> reply).

PARTICIPANTS: dict[str, FlowResponder] = {}

#: Global trace-capture gate. The Network Trace "pause/resume capture" control
#: flips this; it is applied to every live listener AND to any launched
#: afterwards, so the state survives starting new participants. Listeners keep
#: serving — only trace RECORDING is gated.
#:
#: Default OFF: capture starts STOPPED so the trace buffer isn't churning under
#: load by default — the user hits "Resume" to begin collecting a clean window,
#: then "Pause" to freeze it for inspection.
_CAPTURE_ENABLED = False


def _build_flow_responder(config: dict, flow: dict, downstream: dict, flow_id: str):
    """Pick the participant responder matching the inbound (trigger) connection's
    adapter: HTTP listeners use :class:`HttpFlowResponder`, TCP/ISO 8583 (and
    host-command) use :class:`FlowResponder`. gRPC inbound has no server runtime
    yet, so it is rejected with a clear message rather than silently degrading."""
    adapter = str(config.get("adapter") or "").lower()
    proto = str(config.get("protocol") or "").lower()
    if adapter == "http" or proto == "http":
        try:
            from worker.adapters.http.flow_responder import HttpFlowResponder
        except ModuleNotFoundError as exc:  # aiohttp absent in the slim image
            raise HTTPException(
                500,
                "HTTP participant listeners need 'aiohttp', which isn't installed "
                "in this orchestrator image — rebuild the orchestrator (its "
                "Dockerfile now installs aiohttp) to use an HTTP trigger connection.",
            ) from exc
        return HttpFlowResponder(config, flow, downstream=downstream, run_id=flow_id)
    if adapter == "nats" or proto == "nats":
        try:
            from worker.adapters.nats.flow_responder import NatsFlowResponder
        except ModuleNotFoundError as exc:  # nats-py absent in the slim image
            raise HTTPException(
                500,
                "NATS participant listeners need 'nats-py', which isn't installed "
                "in this orchestrator image — rebuild the orchestrator (its "
                "Dockerfile now installs nats-py) to use a NATS trigger connection.",
            ) from exc
        return NatsFlowResponder(config, flow, downstream=downstream, run_id=flow_id)
    if adapter == "grpc" or proto == "grpc":
        raise HTTPException(
            400,
            "inbound gRPC participant listeners are not supported yet — a flow's "
            "trigger connection must be TCP (ISO 8583 / host-command) or HTTP. "
            "gRPC connections still work as downstream 'action' targets.",
        )
    return FlowResponder(config, flow, downstream=downstream, run_id=flow_id)


def _relay_node(flow: dict) -> dict | None:
    """The flow's relay/proxy node, if any. A flow whose relay is TERMINAL (no
    outgoing wire) is a transparent proxy — every inbound frame forwarded
    byte-exact to an upstream. A relay WITH a following node (e.g. a reply)
    runs graph-computed instead: the relay forwards one request and exposes
    the upstream's answer as ``${<relay_id>.response}``."""
    for n in flow.get("nodes", []) or []:
        if str(n.get("kind", "")).lower() in ("relay", "proxy"):
            return n
    return None


def _relay_is_terminal(flow: dict, relay: dict) -> bool:
    """True when the relay node has no outgoing wire — the whole flow is then a
    transparent byte-level proxy (see :func:`_relay_node`)."""
    rid = relay.get("id")
    return not any(e.get("source") == rid for e in flow.get("edges", []) or [])


async def _build_relay_proxy(flow: dict, listen_config: dict, relay: dict):
    """Build a :class:`TcpProxy` for a relay flow (see
    :func:`_relay_proxy_config` for the config semantics)."""
    from worker.adapters.tcp.proxy import TcpProxy

    return TcpProxy(await _relay_proxy_config(flow, listen_config, relay))


async def _relay_proxy_config(flow: dict, listen_config: dict, relay: dict) -> dict:
    """Resolve a relay flow into a ready :class:`TcpProxy` config: listen on the
    trigger connection, forward byte-exact to the relay node's upstream
    connection. Split from :func:`_build_relay_proxy` so fleet placements
    (ADR-0001) can embed the config for a remote host to instantiate.

    ``mode`` on the relay node picks the posture: ``decode`` (default) parses
    every frame for per-command visibility/capture but still forwards it
    unchanged; ``raw`` is a blind byte-exact passthrough (no parsing). Either way
    all commands are relayed — the proxy never answers locally."""
    rcfg = relay.get("config") or {}
    cname = rcfg.get("connection") or relay.get("target")
    upstreams: list[dict] = []
    selection: dict | None = None
    if cname:
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{cname}")
        except Exception:  # noqa: BLE001 — best-effort
            doc = None
        if isinstance(doc, dict):
            upstreams = [{"host": doc.get("host"), "port": doc.get("port")}]
        else:
            # Not a plain connection — it may name a participant group (fleet):
            # forward across its members, balanced by the group's selection policy.
            g = (await _load_groups_index()).get(cname)
            if g:
                selection = g.get("selection")
                for m in g.get("members", []):
                    mc = m.get("connection")
                    if not mc:
                        continue
                    try:
                        mdoc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{mc}")
                    except Exception:  # noqa: BLE001
                        mdoc = None
                    if isinstance(mdoc, dict) and not mdoc.get("disabled"):
                        upstreams.append({"host": mdoc.get("host"), "port": mdoc.get("port")})
    # explicit upstream override on the node wins
    up = rcfg.get("upstream") or {}
    if up.get("host"):
        upstreams = [up]
    upstreams = [u for u in upstreams if u.get("host") and u.get("port")]
    if not upstreams:
        raise HTTPException(
            400,
            "relay node has no usable upstream — set its connection (a connection "
            f"or a participant group), or an explicit upstream. Got {cname!r}.",
        )
    mode = str(rcfg.get("relay_mode") or rcfg.get("mode") or "decode").lower()
    proxy_config = {
        **listen_config,
        "kind": "proxy",
        "mode": "tap",                  # tap = forward-all; relay never stubs
        "decode": mode != "raw",        # raw = blind byte-exact passthrough
        "upstreams": [{"host": u["host"], "port": int(u["port"])} for u in upstreams],
        "selection": selection or {"policy": "round_robin"},
    }
    if len(upstreams) == 1:  # back-compat single-upstream key too
        proxy_config["upstream"] = proxy_config["upstreams"][0]
    # payShield is a header-echo host-command link — decode it as such (the
    # trigger connection's stale ``protocol: iso8583`` would otherwise mis-decode
    # and produce no per-command Network Trace records). Forced, not setdefault.
    if str(listen_config.get("adapter") or "").lower() == "payshield":
        proxy_config["protocol"] = "header_echo"
        proxy_config["header_bytes"] = 4
    return proxy_config


class StartParticipant(BaseModel):
    flow_id: str
    #: optional explicit listen port; otherwise the inbound connection's
    #: listen_port (or an ephemeral port) is used.
    port: int | None = None


class CaptureToggle(BaseModel):
    #: True = record traces, False = pause recording (listeners keep serving).
    enabled: bool


def _participant_info(pid: str, flow_id: str, r: FlowResponder) -> dict:
    host = (getattr(r, "config", {}) or {}).get("host", "127.0.0.1")
    port = r.port
    topo = getattr(r, "_topology_run", None)
    return {
        "id": pid,
        "flow_id": flow_id,
        "flow_name": getattr(r, "_flow_name", None) or flow_id,
        "protocol": r.protocol,
        "host": host,
        "port": port,
        #: bound listen endpoint, or None until the socket is up.
        "endpoint": f"{host}:{port}" if port else None,
        #: who owns this listener — a topology run id, or "standalone".
        "owner": topo or "standalone",
        "topology_run": topo,
        "received": len(r.received),
        "by_mti": dict(r.by_mti),
        "by_response_code": dict(r.by_rc),
    }


def _flow_in_running_topology(flow_id: str) -> str | None:
    """Run id of a live topology that already includes this flow, else None.

    Used to stop a flow being started standalone while it is already up as part
    of a topology (which would orphan a listener and risk a port collision)."""
    for r in PARTICIPANTS.values():
        if getattr(r, "_flow_id", None) == flow_id and getattr(r, "_topology_run", None):
            return r._topology_run  # type: ignore[attr-defined]
    return None


async def _participant_listen_config(flow: dict, port: int | None) -> dict:
    """Build a FlowResponder config from the flow's inbound connection (protocol
    + framing + listen port). Best-effort: an unknown/absent connection falls
    back to an ISO 8583 responder on an ephemeral port."""
    config: dict[str, Any] = {"protocol": "iso8583", "host": "127.0.0.1", "port": port or 0}
    conn_name = (flow.get("trigger") or {}).get("connection")
    if conn_name:
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{conn_name}")
        except Exception:  # noqa: BLE001 — best-effort
            doc = None
        if isinstance(doc, dict):
            skip = {"name", "mode", "listen_port", "environments", "environment_overrides",
                    "description", "host", "port"}
            config = {k: v for k, v in doc.items() if k not in skip}
            config["protocol"] = doc.get("protocol", "iso8583")
            config["host"] = doc.get("host") or "127.0.0.1"
            # One port for both directions: an inbound connection binds its
            # ``port``. ``listen_port`` is legacy (folded into port on read).
            config["port"] = port or doc.get("port") or doc.get("listen_port") or 0
            # payShield is modeled as header_echo on the wire (mirrors
            # _build_relay_proxy) — without this a payShield listener would
            # try to iso8583-decode host commands and answer garbage.
            if str(doc.get("adapter") or "").lower() in ("payshield", "hsm_client"):
                config["protocol"] = "header_echo"
                config.setdefault("header_bytes", 4)
            # NATS is broker-mediated (ADR-0006): tag the protocol so the
            # responder/dispatch and Live Sessions report it as NATS rather than
            # the ConnectionDraft's ISO 8583 default. host/port are the broker,
            # not a bind target — the responder subscribes, it never binds.
            elif str(doc.get("adapter") or "").lower() == "nats":
                config["protocol"] = "nats"
    return config


async def _participant_downstream(flow: dict) -> dict:
    """Resolve the targets a flow's outbound ``action`` nodes call into a
    worker-shaped adapters map (keyed by target name). A target is either a
    single connection or a **participant group** (inlined as an ``adapter:
    "group"`` config so the worker selects a member per dispatch). Best-effort:
    an unresolved target is skipped (the call then surfaces as a failed step)."""
    targets = {
        n.get("target")
        for n in flow.get("nodes", [])
        # in-graph relay nodes dispatch to their upstream like an action does
        if n.get("kind", "action") in ("action", "relay", "proxy") and n.get("target")
    }
    adapters: dict[str, Any] = {}
    groups_index: dict[str, dict] | None = None
    for name in targets:
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{name}")
        except Exception:  # noqa: BLE001 — best-effort
            doc = None
        if isinstance(doc, dict):
            adapters[name] = {k: v for k, v in doc.items() if k not in _SKIP_CONN_KEYS}
            continue
        # Not a plain connection — it may name a participant group (a fleet).
        if groups_index is None:
            groups_index = await _load_groups_index()
        g = groups_index.get(name)
        if g:
            cfg = await _group_adapter_config(g)
            if cfg:
                adapters[name] = cfg
    return adapters


@app.get("/participants")
async def list_participants() -> list[dict]:
    return [_participant_info(pid, getattr(r, "_flow_id", pid), r)
            for pid, r in PARTICIPANTS.items()]


async def _prepare_flow_launch(flow_id: str, flow: dict, port: int | None) -> dict:
    """Everything needed to instantiate a flow's listener, resolved once:
    targets repointed, listen ``config`` + ``downstream`` adapters, and the
    responder-vs-transparent-proxy decision. The in-process launcher builds the
    object from this; the fleet path (ADR-0001) serializes it into a placement
    so a ``worker.flow_host`` can instantiate it remotely."""
    # A flow's outbound action node picks its downstream via the Connection
    # picker (step.config.connection); re-point the node's target at that
    # connection so the worker dispatches to it (mirrors _attach_connections).
    for n in flow.get("nodes", []):
        if n.get("kind", "action") in ("action", "relay", "proxy"):
            conn = (n.get("config") or {}).get("connection")
            if conn:
                n["target"] = conn
    config = await _participant_listen_config(flow, port)
    # A flow whose relay/proxy node is TERMINAL is a transparent proxy — launch
    # a TcpProxy that forwards every inbound frame to the upstream connection.
    # A relay with a following node (e.g. reply) runs as a graph-computed
    # FlowResponder instead: the relay node forwards each request in-graph and
    # exposes the upstream's answer as ${<relay_id>.response}.
    relay = _relay_node(flow)
    if relay is not None and _relay_is_terminal(flow, relay):
        proxy_config = await _relay_proxy_config(flow, config, relay)
        # Surface the relay's upstream (a connection or a group/fleet) as this
        # participant's downstream, so the network graph draws the proxy →
        # upstream edge exactly like a flow's action node would.
        rconn = (relay.get("config") or {}).get("connection")
        downstream: dict = (
            await _participant_downstream({"nodes": [{"kind": "action", "target": rconn}]})
            if rconn else {}
        )
        return {"flow": flow, "config": config, "downstream": downstream,
                "kind": "proxy", "proxy_config": proxy_config}
    downstream = await _participant_downstream(flow)
    return {"flow": flow, "config": config, "downstream": downstream,
            "kind": "responder", "proxy_config": None}


async def _launch_participant(
    flow_id: str, port: int | None = None, flow: dict | None = None,
    topology_run: str | None = None,
) -> dict:
    """Fetch a participant flow + its connections, start listening, register it.

    ``flow`` may be supplied to reuse an already-fetched flow doc (the topology
    launcher does this so it can plan per-instance ports without re-fetching).
    ``topology_run`` tags the listener as owned by a topology run (vs standalone)."""
    import httpx

    if flow is None:
        try:
            flow = await _http_get_json(f"{SCENARIO_API_URL}/participant-flows/{flow_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(404, f"participant flow '{flow_id}' not found") from exc
            raise HTTPException(502, f"scenario-service HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, f"scenario-service unreachable: {exc}") from exc

    prep = await _prepare_flow_launch(flow_id, flow, port)
    config = prep["config"]
    downstream = prep["downstream"]
    if prep["kind"] == "proxy":
        from worker.adapters.tcp.proxy import TcpProxy

        responder = TcpProxy(prep["proxy_config"])
    else:
        responder = _build_flow_responder(config, flow, downstream, flow_id)
    #: Honour a globally-paused capture state for freshly-launched listeners.
    responder._capture = _CAPTURE_ENABLED  # type: ignore[attr-defined]
    #: Keep the resolved downstream adapters for introspection (the network-graph
    #: endpoint reads it to draw participant → target edges without re-resolving).
    responder._downstream = downstream  # type: ignore[attr-defined]
    try:
        await responder.start()
    except Exception as exc:  # noqa: BLE001 — bad config / port in use
        raise HTTPException(400, f"could not start participant: {exc}")
    pid = uuid.uuid4().hex[:8]
    responder._flow_id = flow_id  # type: ignore[attr-defined]
    # Human-readable flow name for graph/list captions (the id is a stable handle,
    # not a label). Falls back to the id when a flow is unnamed.
    responder._flow_name = flow.get("name") or flow_id  # type: ignore[attr-defined]
    responder._topology_run = topology_run  # type: ignore[attr-defined]
    PARTICIPANTS[pid] = responder
    return _participant_info(pid, flow_id, responder)


@app.post("/participants/start", status_code=201)
async def start_participant(req: StartParticipant) -> dict:
    """Fetch a participant flow + its inbound connection, then start listening.

    Rejected (409) if the flow is already running as part of a live topology —
    starting it standalone too would orphan a listener and risk a port clash."""
    owner = _flow_in_running_topology(req.flow_id)
    if owner:
        raise HTTPException(
            409,
            f"flow '{req.flow_id}' is already running as part of topology run "
            f"'{owner}' — stop that topology before starting it standalone",
        )
    info = await _launch_participant(req.flow_id, req.port)
    _persist_runtime()
    return info


@app.get("/participants/traces")
async def participant_traces(
    limit: int = 100, q: str | None = None, flow: str | None = None
) -> dict:
    """Recent transactions across all listeners — the index for the Network Trace
    page. One row per correlation id (newest first) with an ok/fail flag, so the
    UI can list transactions and drill into any one via ``/participants/trace/{cid}``.
    ``q`` filters by correlation id substring (e.g. a STAN); ``flow`` keeps only
    transactions that passed through that participant flow (the map's node
    drawer deep-links here with it)."""
    by_cid: dict[str, dict] = {}

    def _acc(t: dict) -> None:
        cid = t.get("correlation_id")
        if not cid or (q and q not in cid):
            return
        row = by_cid.setdefault(cid, {
            "correlation_id": cid, "ts": 0, "mti": t.get("mti"),
            "kind": t.get("kind", "tcp"), "flows": [], "hops": 0, "ok": True,
        })
        row["ts"] = max(row["ts"], t.get("ts", 0))
        row["mti"] = row["mti"] or t.get("mti")
        # Tag the row by the entry hop's adapter (first one seen for this cid).
        row["kind"] = row.get("kind") or t.get("kind", "tcp")
        fid = t.get("flow_id")
        if fid and fid not in row["flows"]:
            row["flows"].append(fid)
        row["hops"] += 1 + len(t.get("calls") or [])
        if not t.get("ok", True):
            row["ok"] = False

    for pid, r in PARTICIPANTS.items():
        getter = getattr(r, "traces", None)
        if getter is None:
            continue
        for t in getter():
            _acc(t)
    for t in await _fleet_trace_records():
        _acc(t)
    rows = sorted(by_cid.values(), key=lambda x: x["ts"], reverse=True)
    if flow:
        rows = [r for r in rows if flow in r["flows"]]
    return {"transactions": rows[:limit]}


async def _fleet_trace_records(cid: str | None = None) -> list[dict]:
    """Trace hops shipped by fleet hosts for every live fleet run (ADR-0001
    item 8). Records already carry ``participant`` (the instance id) and
    ``worker_id``. Best-effort: a bus hiccup yields an empty fleet view, never
    an error on the local one."""
    out: list[dict] = []
    for t in TOPOLOGY_RUNS.values():
        if not t.get("fleet"):
            continue
        try:
            rows = await load_bus.flow_traces(str(t.get("run_id")))
        except Exception:  # noqa: BLE001
            continue
        for rec in rows:
            if cid is None or rec.get("correlation_id") == cid:
                out.append(rec)
    return out


@app.get("/participants/trace/{cid}")
async def participant_trace(cid: str) -> dict:
    """Cross-hop correlated trace: every hop tagged with this correlation id,
    across all live participant listeners — local *and* fleet-hosted — in time
    order. Stitches one transaction's path (e.g. scenario → switch → issuer →
    back) into one view."""
    hops: list[dict] = []
    for pid, r in PARTICIPANTS.items():
        getter = getattr(r, "traces", None)
        if getter is None:
            continue
        for t in getter(cid):
            hops.append({"participant": pid, **t})
    hops.extend(await _fleet_trace_records(cid))
    hops.sort(key=lambda h: h.get("ts", 0))
    return {"correlation_id": cid, "hops": hops}


@app.get("/participants/capture")
async def get_participants_capture() -> dict:
    """Current trace-capture state — drives the Network Trace pause/resume control.
    ``capturing`` reflects the global gate; ``buffered`` is how many hop records
    are currently held across all live listeners."""
    buffered = sum(
        len(getattr(r, "traces", lambda: [])()) for r in PARTICIPANTS.values()
    )
    return {
        "capturing": _CAPTURE_ENABLED,
        "participants": len(PARTICIPANTS),
        "buffered": buffered,
    }


@app.post("/participants/capture")
async def set_participants_capture(req: CaptureToggle) -> dict:
    """Pause or resume trace recording across every live listener (and any
    launched afterwards). Listeners keep serving — only recording is gated.
    Fleet hosts pick the gate up from the bus on their next heartbeat."""
    global _CAPTURE_ENABLED
    _CAPTURE_ENABLED = bool(req.enabled)
    for r in PARTICIPANTS.values():
        r._capture = _CAPTURE_ENABLED  # type: ignore[attr-defined]
    try:
        await load_bus.set_flow_capture(_CAPTURE_ENABLED)
    except Exception:  # noqa: BLE001 — best-effort fleet propagation
        log.debug("could not propagate capture gate to the fleet", exc_info=True)
    return {"capturing": _CAPTURE_ENABLED, "participants": len(PARTICIPANTS)}


@app.delete("/participants/traces", status_code=204, response_class=Response)
async def clear_traces() -> Response:
    """Empty every live listener's trace ring buffer (the Network Trace "Clear"
    control). Does not stop capture or touch the listeners themselves."""
    for r in PARTICIPANTS.values():
        for attr in ("_traces", "_relay_traces"):  # flow listeners + relay proxies
            buf = getattr(r, attr, None)
            if isinstance(buf, list):
                buf.clear()
        opens = getattr(r, "_open_relay", None)  # drop in-flight proxy pairings
        if isinstance(opens, dict):
            opens.clear()
    # fleet runs: drop the shipped records too (hosts only ship *new* hops —
    # their watermarks keep already-shipped ones from coming back).
    for t in TOPOLOGY_RUNS.values():
        if t.get("fleet"):
            try:
                await load_bus.clear_flow_traces(str(t.get("run_id")))
            except Exception:  # noqa: BLE001 — best-effort
                pass
    return Response(status_code=204)


@app.get("/participants/{pid}")
async def get_participant(pid: str) -> dict:
    r = PARTICIPANTS.get(pid)
    if r is None:
        raise HTTPException(404, f"no participant '{pid}'")
    info = _participant_info(pid, getattr(r, "_flow_id", pid), r)
    info["stats"] = r.stats()
    info["log"] = r.received[-50:]  # recent decoded requests
    info["traces"] = r.traces()[-50:] if hasattr(r, "traces") else []
    return info


@app.delete("/participants/{pid}", status_code=204, response_class=Response)
async def stop_participant(pid: str) -> Response:
    r = PARTICIPANTS.pop(pid, None)
    if r is None:
        raise HTTPException(404, f"no participant '{pid}'")
    await r.stop()
    _persist_runtime()
    return Response(status_code=204)


# -- topologies (start/stop a whole set of participant flows as a unit) --------
#
# A topology run launches each participant's instances (callees first, per the
# topology's order) and tracks them under a run id so they stop together.

TOPOLOGY_RUNS: dict[str, dict] = {}


def _piece_live(r: Any) -> bool:
    """Is a participant/simulator responder actually up? A port-bound listener is
    live when it holds a port; a **port-less NATS responder** (ADR-0006) is live
    when it's connected to the broker with active subscriptions (``listening()``).
    """
    if r is None:
        return False
    if getattr(r, "port", None) is not None:
        return True
    lst = getattr(r, "listening", None)
    return bool(lst()) if callable(lst) else False


def _piece_endpoint(r: Any) -> str | None:
    """Display endpoint for a live responder: ``host:port`` for a bound listener,
    or ``nats:<subjects>[@queue_group]`` for a port-less NATS responder."""
    if getattr(r, "port", None):
        host = (getattr(r, "config", {}) or {}).get("host", "127.0.0.1")
        return f"{host}:{r.port}"
    lst = getattr(r, "listening", None)
    if callable(lst) and lst():
        subjects = ",".join(getattr(r, "subjects", []) or [])
        qg = getattr(r, "queue_group", "") or ""
        return f"nats:{subjects}" + (f"@{qg}" if qg else "")
    return None


def _run_endpoints(run: dict) -> list[dict]:
    """Bound listen endpoints of a run's live participants + referenced
    simulators (for display). Fleet runs (ADR-0001) read the advertised
    endpoint registry snapshot instead of local listeners."""
    eps: list[dict] = []
    if run.get("fleet"):
        for i in flow_fleet.instances_cached(str(run.get("run_id"))):
            if i.get("port"):
                eps.append({"participant": i.get("instance_id"),
                            "flow_id": i.get("flow_id"),
                            "endpoint": f"{i.get('host')}:{i.get('port')}",
                            "worker": i.get("worker_id")})
    for pid in run.get("participants", []):
        r = PARTICIPANTS.get(pid)
        ep = _piece_endpoint(r) if r is not None else None
        if ep:
            eps.append({"participant": pid, "flow_id": getattr(r, "_flow_id", pid),
                        "endpoint": ep})
    for sid in run.get("simulator_ids", []):
        s = SIMULATORS.get(sid)
        ep = _piece_endpoint(s) if s is not None else None
        if ep:
            eps.append({"participant": sid, "flow_id": sid, "endpoint": ep})
    return eps


def _run_health(run: dict) -> dict:
    """Live readiness of a topology run: how many of its participants AND
    referenced simulators are still registered and actually listening.

    ``simulator_ids`` is every simulator node the network references — a
    simulator counts toward readiness whether this run started it or found it
    already running (the network needs it up either way).

    Fleet runs (ADR-0001) count *expected* instances against the freshly
    advertised (heartbeat-refreshed) endpoint registry, so a dead flow host
    degrades the run within ``INSTANCE_TTL_S`` — the same readiness the
    ``requires_topology`` gate enforces."""
    pids = run.get("participants", [])
    sids = run.get("simulator_ids", [])
    live = 0
    if run.get("fleet"):
        expected = len(run.get("instance_ids", []))
        live += len([
            i for i in flow_fleet.instances_cached(str(run.get("run_id")))
            if i.get("status") == "up"
        ])
    else:
        expected = len(pids)
        for pid in pids:
            if _piece_live(PARTICIPANTS.get(pid)):
                live += 1
    total = expected + len(sids)
    for sid in sids:
        if _piece_live(SIMULATORS.get(sid)):
            live += 1
    return {"total": total, "live": live, "ready": total > 0 and live == total}


def _topology_is_up(topology_id: str) -> bool:
    """Is a run of this topology currently live AND healthy?

    "Healthy" means every participant instance the run started is still
    registered and bound to a port — not merely that a run record exists. This
    backs the ``requires_topology`` gate so a scenario can't fire against a
    half-dead network."""
    return any(
        t.get("topology_id") == topology_id and _run_health(t)["ready"]
        for t in TOPOLOGY_RUNS.values()
    )


async def _flow_listen_target(flow: dict) -> tuple[str, int | None]:
    """Resolve a flow's inbound (trigger) connection to (host, base listen port).

    ``base port`` is ``None`` when the connection has no fixed ``listen_port``
    (so instances bind ephemeral ports). Best-effort: an unknown connection
    yields ``("127.0.0.1", None)``."""
    conn_name = (flow.get("trigger") or {}).get("connection")
    if not conn_name:
        return "127.0.0.1", None
    try:
        doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{conn_name}")
    except Exception:  # noqa: BLE001 — best-effort
        return "127.0.0.1", None
    if not isinstance(doc, dict):
        return "127.0.0.1", None
    host = doc.get("host") or "127.0.0.1"
    base = doc.get("listen_port") or doc.get("port")  # one port; listen_port legacy
    return host, (int(base) if base else None)


async def _flow_nats_target(flow: dict) -> tuple[str, list[str], str] | None:
    """Resolve a NATS flow's subject *claim*: ``(server_url, subjects, queue_group)``.

    NATS participants are **port-less** (ADR-0006): the uniqueness resource is the
    subject, not a ``(host, port)``. Returns ``None`` when the flow's trigger
    connection is not a NATS connection, so the caller falls back to port planning.
    Best-effort: an unresolvable connection yields ``None``."""
    conn_name = (flow.get("trigger") or {}).get("connection")
    if not conn_name:
        return None
    try:
        doc = await _http_get_json(f"{SCENARIO_API_URL}/connections/{conn_name}")
    except Exception:  # noqa: BLE001 — best-effort
        return None
    if not isinstance(doc, dict) or str(doc.get("adapter") or "").lower() != "nats":
        return None
    servers = doc.get("servers")
    if servers:
        server = str(servers[0] if isinstance(servers, list) else servers)
    else:
        host = doc.get("host") or "127.0.0.1"
        port = int(doc.get("port") or 4222)
        server = host if "://" in str(host) else f"nats://{host}:{port}"
    subj = doc.get("subjects") or doc.get("subject") or []
    subjects = [subj] if isinstance(subj, str) else [str(s) for s in subj]
    return server, subjects, str(doc.get("queue_group") or "")


# NOTE: the legacy POST /topologies/{tid}/start endpoint was removed
# (ADR-0004) — topologies were absorbed by network flows with the same ids;
# use POST /network-flows/{id}/start. The /topology-runs endpoints stay: they
# are the run API network flows execute through.


@app.get("/topology-runs")
async def list_topology_runs() -> list[dict]:
    return [{"id": rid, "topology_id": t["topology_id"], "name": t["name"],
             "kind": t.get("kind", "topology"),
             "instances": len(t["participants"]), "health": _run_health(t),
             "endpoints": _run_endpoints(t)}
            for rid, t in TOPOLOGY_RUNS.items()]


@app.delete("/topology-runs/{run_id}", status_code=204, response_class=Response)
async def stop_topology(run_id: str) -> Response:
    t = TOPOLOGY_RUNS.pop(run_id, None)
    if t is None:
        raise HTTPException(404, f"no topology run '{run_id}'")
    # stop the bound traffic drivers first so they aren't calling dead listeners
    init_runs = t.get("initiator_runs") or (
        [t["initiator_run"]] if t.get("initiator_run") else [])
    for init_run in init_runs:
        try:
            await cancel_run(init_run)
        except Exception:  # noqa: BLE001 — best-effort
            pass
    for pid in t["participants"]:
        r = PARTICIPANTS.pop(pid, None)
        if r is not None:
            await r.stop()
    # fleet runs: raise the network-wide stop flag — every flow host tears down
    # its instances of this run and retracts them from the registry.
    if t.get("fleet"):
        try:
            await flow_fleet.stop(str(t.get("run_id", run_id)))
        except Exception:  # noqa: BLE001 — best-effort; TTLs clean up anyway
            log.warning("fleet stop for run '%s' failed", run_id, exc_info=True)
    # stop the saved simulators this run brought up (never ones it found running)
    for sid in t.get("simulators", []):
        r = SIMULATORS.pop(sid, None)
        if r is not None:
            await r.stop()
            _drop_sim_runtime(sid, getattr(r, "_label", sid))
    _persist_runtime()
    return Response(status_code=204)


# -- network flows (ADR-0004: canvas-authored composite, successor of topologies)

@app.post("/network-flows/{nid}/start", status_code=201)
async def start_network_flow(nid: str) -> dict:
    """Start a network flow: every ``participant`` node's instances,
    callees-first per the wiring plan, then fire its autostart initiators.

    Runs share the topology-run machinery (``TOPOLOGY_RUNS``, health,
    ``/topology-runs`` stop) — the run's ``topology_id`` carries the network
    -flow id, so the ``requires_topology`` gate accepts network-flow ids
    transparently (migration reuses topology ids on purpose)."""
    import httpx

    try:
        net = await _http_get_json(f"{SCENARIO_API_URL}/network-flows/{nid}")
        plan_doc = await _http_get_json(f"{SCENARIO_API_URL}/network-flows/{nid}/plan")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"network flow '{nid}' not found") from exc
        raise HTTPException(502, f"scenario-service HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f"scenario-service unreachable: {exc}") from exc

    # Same per-instance port planning as topologies: allocate base, base+1, …
    # per flow BEFORE starting anything. A participant node's explicit ``port``
    # overrides the connection's listen_port as the base.
    plan: list[dict] = []                 # {flow_id, flow, port, node_id}
    seen: dict[tuple[str, int], str] = {}  # (host, port) -> flow_id
    # NATS participants are port-less (ADR-0006): they claim a subject, not a
    # port. A subject with NO queue group claimed twice in one plan is a 409
    # (both would receive every message); the SAME subject + queue group is
    # legal fan-out (that's what queue groups are for), so those never collide.
    subj_seen: dict[tuple[str, str], str] = {}  # (server, subject) no-queue -> flow_id
    for entry in plan_doc.get("participants", []):
        fid = entry.get("flow_id")
        if not fid:
            continue
        try:
            flow = await _http_get_json(f"{SCENARIO_API_URL}/participant-flows/{fid}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    404, f"participant flow '{fid}' (node "
                         f"'{entry.get('node_id')}') not found") from exc
            raise HTTPException(502, f"scenario-service HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, f"scenario-service unreachable: {exc}") from exc
        n = max(1, int(entry.get("instances", 1) or 1))
        nats_target = await _flow_nats_target(flow)
        if nats_target is not None:
            # Port-less NATS participant: validate the subject claim, then plan
            # every instance with port=None (they subscribe, they don't bind).
            server, subjects, qgroup = nats_target
            for idx in range(n):
                if not qgroup:
                    for subj in subjects:
                        key = (server, subj)
                        if key in subj_seen:
                            raise HTTPException(
                                409,
                                f"subject collision in network flow: '{subj}' on "
                                f"{server} wanted by both '{subj_seen[key]}' and "
                                f"'{fid}' with no queue group — both would receive "
                                "every message; give them a shared queue_group for "
                                "fan-out or use distinct subjects",
                            )
                        subj_seen[key] = fid
                plan.append({"flow_id": fid, "flow": flow, "port": None,
                             "node_id": entry.get("node_id")})
            continue
        host, base = await _flow_listen_target(flow)
        if entry.get("port"):
            base = int(entry["port"])
        for idx in range(n):
            port = (base + idx) if base else None
            if port is not None:
                key = (host, port)
                if key in seen:
                    raise HTTPException(
                        409,
                        f"port collision in network flow: {host}:{port} wanted "
                        f"by both '{seen[key]}' and '{fid}' — adjust ports / "
                        f"instances so each instance gets a distinct port",
                    )
                seen[key] = fid
            plan.append({"flow_id": fid, "flow": flow, "port": port,
                         "node_id": entry.get("node_id")})

    # ``simulator`` nodes: bring up saved simulators first (pure callees). Only
    # simulators *this run actually started* are stopped with it — one already
    # running (autostarted or shared with another network) is left alone. ALL
    # referenced simulators count toward the run's health/readiness.
    sims_started: list[str] = []
    sim_ids: list[str] = []
    for sim in plan_doc.get("simulators", []):
        sid = sim.get("simulator_id")
        if not sid:
            continue
        sim_ids.append(sid)
        saved = simulator_store.get(sid)
        if saved is None:
            raise HTTPException(
                404, f"saved simulator '{sid}' (node '{sim.get('node_id')}') "
                     "not found")
        if sid not in SIMULATORS:
            try:
                await _start_responder(sid, saved["label"], saved["config"])
                sims_started.append(sid)
            except Exception as exc:  # noqa: BLE001 — fail closed, nothing lingers
                for done in sims_started:
                    r = SIMULATORS.pop(done, None)
                    if r is not None:
                        await r.stop()
                raise HTTPException(
                    400, f"could not start simulator '{sid}': {exc}") from exc

    run_id = uuid.uuid4().hex[:8]

    # ADR-0001: with NETWORK_FLEET=1 and flow hosts heartbeating, the listeners
    # are placed on the worker fleet (endpoints advertised into the registry);
    # otherwise they start in-process exactly as before.
    fleet_mode = _fleet_enabled() and await flow_fleet.available()
    started: list[dict] = []
    instance_ids: list[str] = []
    if fleet_mode:
        placements: list[dict] = []
        for idx, p in enumerate(plan):
            prep = await _prepare_flow_launch(p["flow_id"], p["flow"], p["port"])
            iid = f"{run_id}-{idx:03d}"
            instance_ids.append(iid)
            placements.append({
                "run_id": run_id,
                "instance_id": iid,
                "flow_id": p["flow_id"],
                "flow_name": (p["flow"].get("name") or p["flow_id"]),
                "node_id": p["node_id"],
                "flow": prep["flow"],
                "config": prep["config"],
                "downstream": prep["downstream"],
                "kind": prep["kind"],
                "proxy_config": prep["proxy_config"],
                "capture": _CAPTURE_ENABLED,
            })
        try:
            # Callee-first *waves*: place one instance at a time and feed the
            # endpoints advertised so far into the next placement's outbound
            # references (downstream / relay upstreams). This is what lets a
            # caller on host B reach its callee on host A (ADR-0001 item 4).
            instances = []
            wave_idx: dict[int, str] = {}
            for placement in placements:
                if wave_idx:
                    _rewrite_fleet_refs(placement, wave_idx)
                placed = await flow_fleet.place(run_id, [placement])
                instances.extend(placed)
                for i in placed:
                    # NATS participants (ADR-0006) are port-less: they subscribe
                    # to a subject on the shared broker, so there is no
                    # (host, port) endpoint to advertise into the wave index —
                    # a downstream NATS caller reaches them by subject via the
                    # broker regardless of which fleet host runs them. Only
                    # port-binding (TCP/HTTP) instances feed the port→host map.
                    if i.get("status") == "up" and i.get("port") and i.get("host"):
                        wave_idx[int(i["port"])] = str(i["host"])
        except PlacementError as exc:
            for sid in sims_started:
                r = SIMULATORS.pop(sid, None)
                if r is not None:
                    await r.stop()
            raise HTTPException(502, str(exc)) from exc
        started = [{
            "id": i.get("instance_id"), "flow_id": i.get("flow_id"),
            "port": i.get("port"),
            "endpoint": f"{i.get('host')}:{i.get('port')}",
            "worker": i.get("worker_id"), "fleet": True,
        } for i in instances]
    else:
        try:
            for p in plan:
                started.append(await _launch_participant(
                    p["flow_id"], port=p["port"], flow=p["flow"],
                    topology_run=run_id))
        except HTTPException:
            # roll back anything started so a partial network doesn't linger
            for info in started:
                r = PARTICIPANTS.pop(info["id"], None)
                if r is not None:
                    await r.stop()
            for sid in sims_started:
                r = SIMULATORS.pop(sid, None)
                if r is not None:
                    await r.stop()
            raise
    run = {
        "topology_id": nid, "kind": "network_flow",
        "run_id": run_id,
        "name": net.get("name", nid),
        "fleet": fleet_mode,
        "participants": [] if fleet_mode else [i["id"] for i in started],
        "instance_ids": instance_ids,   # fleet: expected registry entries
        "simulators": sims_started,     # stop-ownership: started by this run
        "simulator_ids": sim_ids,       # health: every referenced simulator
    }
    TOPOLOGY_RUNS[run_id] = run

    # Traffic drivers: fire every autostart ``scenario`` node now that the
    # listeners are up. Best-effort — a failed initiator never tears down the
    # network; errors are surfaced on the run instead.
    initiator_runs: list[str] = []
    initiator_errors: list[str] = []
    for init in plan_doc.get("initiators", []):
        if not init.get("autostart", True) or not init.get("scenario_id"):
            continue
        try:
            resp = await create_run(CreateRunRequest(
                scenario_ids=[init["scenario_id"]],
                environment_name=init.get("environment"),
                label=f"initiator · {net.get('name', nid)}",
            ))
            initiator_runs.append(resp.run_id)
        except Exception as exc:  # noqa: BLE001 — never block network start
            initiator_errors.append(f"{init.get('node_id', '?')}: {exc}")
            log.warning("network flow '%s' initiator '%s' failed: %s",
                        nid, init.get("node_id"), exc, exc_info=True)
    if initiator_runs:
        run["initiator_runs"] = initiator_runs
        run["initiator_run"] = initiator_runs[0]  # legacy display field
    if initiator_errors:
        run["initiator_error"] = "; ".join(initiator_errors)
    _persist_runtime()
    return {"id": run_id, "network_flow_id": nid, "topology_id": nid,
            "kind": "network_flow", "name": net.get("name", nid),
            "fleet": fleet_mode,
            "health": _run_health(run), "participants": started,
            "simulators": sims_started,
            "warnings": plan_doc.get("warnings", []),
            "initiator_runs": initiator_runs,
            "initiator_error": run.get("initiator_error")}


# -- showcase one-click install (the empty-state onboarding path) --------------
# Builds the demo acquiring network (driver → switch → 3 issuers + payShield)
# through the same public APIs scripts/showcase.py uses, from one shared spec
# (api.showcase_spec). This is what the portal's "Load showcase" button calls so
# a fresh install is one click from a live, wired, runnable network.

class ShowcaseInstall(BaseModel):
    #: also start the network after building it (default: just build).
    start: bool = False


@app.post("/showcase/install", status_code=201)
async def install_showcase(req: ShowcaseInstall = ShowcaseInstall()) -> dict:
    """Create the showcase demo network (idempotent) and optionally start it."""
    from . import showcase_spec as spec

    sceno = SCENARIO_API_URL

    async def _get_or_none(url: str):
        import httpx
        try:
            return await _http_get_json(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    try:
        # connections + participant flows + group: PUT by chosen id (idempotent)
        for name, cfg in spec.CONNS.items():
            await _http_json("PUT", f"{sceno}/connections/{name}", cfg)
        await _http_json("PUT", f"{sceno}/participant-flows/{spec.ISSUER_FLOW}",
                         spec.issuer_flow())
        await _http_json("PUT", f"{sceno}/participant-flows/{spec.SWITCH_FLOW}",
                         spec.switch_flow())
        await _http_json("PUT", f"{sceno}/participant-groups/{spec.GROUP_ID}",
                         spec.issuer_group())
        # the live selector env the autostart driver runs against (so it dials
        # the real switch instead of the mock adapter)
        await _http_json("PUT", f"{sceno}/environments/{spec.ENV_NAME}",
                         spec.showcase_environment())

        # driver scenario: server-assigned id, resolve by name
        scenarios = await _http_get_json(f"{sceno}/scenarios")
        existing = next((s for s in scenarios
                         if s.get("name") == spec.DRIVER_NAME), None)
        if existing:
            driver_id = existing["id"]
            await _http_json("PUT", f"{sceno}/scenarios/{driver_id}",
                             {"scenario": spec.driver_scenario(),
                              "comment": "showcase"})
        else:
            created = await _http_post_json(
                f"{sceno}/scenarios",
                {"scenario": spec.driver_scenario(), "comment": "showcase"})
            driver_id = created["id"]
    except Exception as exc:  # noqa: BLE001 — surface a clean install error
        raise HTTPException(
            502, f"could not install showcase config on scenario-service: {exc}")

    # payShield simulator: saved locally on the orchestrator, resolve by label
    saved = next((s for s in simulator_store.list()
                  if s.get("label") == spec.SIM_LABEL), None)
    cfg = spec.simulator_config()
    if saved:
        sim_id = saved["id"]
    else:
        sim_id = simulator_store.create(cfg)["id"]

    # the network wiring, referencing the resolved driver + sim ids
    await _http_json("PUT", f"{sceno}/network-flows/{spec.NET_ID}",
                     spec.network_doc(driver_id, sim_id))

    result = {"network_flow_id": spec.NET_ID, "driver_id": driver_id,
              "simulator_id": sim_id, "started": False}
    if req.start:
        started = await start_network_flow(spec.NET_ID)
        result["started"] = True
        result["run"] = started
    return result


# -- live network graph (Topology Map) ----------------------------------------

def _adapter_host_port(cfg: Any) -> tuple[str | None, int | None]:
    """Best-effort (host, port) from a worker adapter config (tcp/grpc/http).

    Used to resolve a participant's downstream target back to a listener node so
    the network graph can draw the edge. A ``group`` adapter has no single host —
    returns (None, None) and the edge falls back to a named target node."""
    if not isinstance(cfg, dict):
        return None, None
    host = cfg.get("host")
    port = cfg.get("port")
    target = cfg.get("target") or (cfg.get("grpc") or {}).get("target")
    if (not host or not port) and isinstance(target, str) and ":" in target:
        h, _, p = target.rpartition(":")
        host = host or h
        try:
            port = port or int(p)
        except ValueError:
            pass
    if not port:
        url = cfg.get("base_url") or cfg.get("url")
        if isinstance(url, str):
            import re

            m = re.search(r"://([^/:]+)(?::(\d+))?", url)
            if m:
                host = host or m.group(1)
                if m.group(2):
                    port = int(m.group(2))
    try:
        port = int(port) if port is not None else None
    except (ValueError, TypeError):
        port = None
    return host, port


def _resolved_endpoints(env: dict | None) -> list[dict]:
    """Runtime-resolved target endpoints from a run's environment adapters.

    One ``{target, host, port}`` per adapter the run was wired to, captured at
    run time so the sign-off's provenance records *what was tested against what*.
    Hosts/ports are connection targets (not credentials); any secret-named adapter
    fields are masked separately by the store's box at rest."""
    out: list[dict] = []
    for target, cfg in ((env or {}).get("adapters") or {}).items():
        host, port = _adapter_host_port(cfg)
        if host is None and port is None:
            continue  # e.g. a group adapter has no single host
        out.append({"target": target, "host": host, "port": port})
    return out


def _emit_group(
    src: str, gname: str, group_cfg: dict, nodes: dict, edges: list,
    port_index: dict, added: set,
) -> None:
    """Render a participant group (fleet) as its own node fanning out to each
    member connection, with the selection policy. ``group_cfg`` is a worker
    ``{"adapter": "group", "members": [...], "selection": {...}}`` config — its
    members already carry resolved connection configs (host/port)."""
    gkey = f"group:{gname}"
    members = group_cfg.get("members") or []
    policy = (group_cfg.get("selection") or {}).get("policy", "round_robin")
    if gkey not in nodes:
        nodes[gkey] = {
            "id": gkey, "kind": "group", "label": gname,
            "sublabel": f"{policy} · {len(members)} members",
            "protocol": "", "port": None, "status": "up",
            "received": 0, "peers": 0,
        }
    if gname not in added:
        added.add(gname)
        edges.append({"id": f"{src}->{gkey}", "source": src, "target": gkey,
                      "kind": "route", "label": gname})
    grp_received = 0
    for m in members:
        mcfg = m.get("config") or {}
        cname = m.get("connection") or "?"
        host, port = _adapter_host_port(mcfg)
        tgt = port_index.get(port) if port else None
        if tgt is None:
            ok = bool(host and port)
            mkey = f"host:{cname}"
            if mkey not in nodes:
                nodes[mkey] = {
                    "id": mkey, "kind": "host", "label": cname,
                    "sublabel": (f"{host}:{port}" if ok else "no address"),
                    "protocol": mcfg.get("protocol", "") if isinstance(mcfg, dict) else "",
                    "port": port, "status": "external" if ok else "unresolved",
                    "received": 0, "peers": 0,
                }
            tgt = mkey
        grp_received += int(nodes.get(tgt, {}).get("received", 0) or 0)
        edges.append({"id": f"{gkey}->{tgt}:{cname}", "source": gkey,
                      "target": tgt, "kind": "member", "label": cname})
    # The fleet itself carries no socket, so let its throughput = the sum of its
    # members' — otherwise the edge *into* the fleet never animates (its endpoint
    # would always look idle) even while data fans out the other side.
    nodes[gkey]["received"] = grp_received


async def _connection_ports() -> dict[str, int]:
    """Outbound connection name -> dial port, from the connection store. Lets the
    graph resolve an env adapter that references a connection by name without an
    inlined host/port (connections are attached at run-build, not in env files)."""
    try:
        conns = await _http_get_json(f"{SCENARIO_API_URL}/connections")
    except Exception:  # noqa: BLE001 — best-effort
        return {}
    out: dict[str, int] = {}
    if isinstance(conns, list):
        for c in conns:
            if isinstance(c, dict) and c.get("mode") != "inbound" and c.get("port"):
                out[str(c.get("name"))] = int(c["port"])
    return out


def _nats_subjects_of(cfg: Any) -> list[str]:
    """NATS subject(s) declared on a worker/connection config (ADR-0006). A
    producer connection carries ``subject``; a responder carries ``subjects``.
    Empty for non-NATS configs — used to draw broker-mediated graph edges."""
    if not isinstance(cfg, dict):
        return []
    if str(cfg.get("adapter") or cfg.get("protocol") or "").lower() != "nats":
        # tolerate configs that carry a subject without an explicit adapter tag
        if not (cfg.get("subject") or cfg.get("subjects")):
            return []
    subj = cfg.get("subjects") or cfg.get("subject") or []
    return [str(subj)] if isinstance(subj, str) else [str(s) for s in subj]


async def _connection_subjects() -> dict[str, str]:
    """Outbound NATS connection name -> its subject (ADR-0006). The producer side
    of a broker-mediated edge: a step targets this connection, which publishes to
    this subject; the graph matches it to whichever responder subscribes there."""
    try:
        conns = await _http_get_json(f"{SCENARIO_API_URL}/connections")
    except Exception:  # noqa: BLE001 — best-effort
        return {}
    out: dict[str, str] = {}
    if isinstance(conns, list):
        for c in conns:
            if not isinstance(c, dict):
                continue
            subs = _nats_subjects_of(c)
            if subs:
                out[str(c.get("name"))] = subs[0]
    return out


@app.get("/network-graph")
async def network_graph() -> dict:
    """One live graph of the simulated network for the Topology Map: traffic
    drivers → participant listeners → downstream simulators / hosts, plus an
    external-clients node feeding any listener that has inbound peers. Nodes
    carry live status + cumulative message counts; the portal derives per-poll
    rates and edge animation from successive snapshots. Assembled purely from
    in-memory runtime — no extra service round-trips."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    port_index: dict[int, str] = {}  # listen port -> node id (for downstream match)
    #: NATS subject -> responder node id (ADR-0006). Broker-mediated participants
    #: bind no port, so producers are matched to them by subject instead of
    #: (host, port) — this is what lets the map draw a NATS conversation.
    subject_index: dict[str, str] = {}

    # -- simulators: running instances first, then saved-but-stopped configs --
    saved_sims = {s["id"]: s for s in simulator_store.list()}
    for sid, r in SIMULATORS.items():
        nid = f"sim:{sid}"
        nodes[nid] = {
            "id": nid, "kind": "simulator", "label": getattr(r, "_label", sid),
            "sublabel": r.protocol, "protocol": r.protocol, "port": r.port,
            "status": "up", "received": len(r.received), "peers": len(r.peers()),
            "by_rc": dict(r.by_rc), "by_mti": dict(r.by_mti),
            "saved": sid in saved_sims,
        }
        if r.port:
            port_index[r.port] = nid
        for subj in getattr(r, "subjects", None) or []:
            subject_index.setdefault(str(subj), nid)
    for sid, s in saved_sims.items():
        nid = f"sim:{sid}"
        if nid in nodes:
            continue
        cfg = s.get("config") or {}
        nodes[nid] = {
            "id": nid, "kind": "simulator", "label": s.get("label", sid),
            "sublabel": cfg.get("protocol", "iso8583"),
            "protocol": cfg.get("protocol", "iso8583"), "port": cfg.get("port"),
            "status": "down", "received": 0, "peers": 0, "saved": True,
        }
        for subj in _nats_subjects_of(cfg):
            subject_index.setdefault(subj, nid)

    # -- participants: running flow listeners --
    for pid, r in PARTICIPANTS.items():
        flow_id = getattr(r, "_flow_id", pid)
        topo = getattr(r, "_topology_run", None)
        nid = f"pt:{pid}"
        nodes[nid] = {
            "id": nid, "kind": "participant",
            # caption is the flow's name (human label), not its id (stable handle)
            "label": getattr(r, "_flow_name", None) or flow_id,
            "flow_id": flow_id,
            "sublabel": r.protocol, "protocol": r.protocol, "port": r.port,
            # port-less NATS participants (ADR-0006) are "up" when subscribed
            "status": "up" if _piece_live(r) else "degraded", "received": len(r.received),
            "peers": len(r.peers()), "by_rc": dict(r.by_rc), "by_mti": dict(r.by_mti),
            "owner": topo or "standalone",
        }
        if r.port:
            port_index[r.port] = nid
        for subj in getattr(r, "subjects", None) or []:
            subject_index.setdefault(str(subj), nid)

    # -- topology containers: a running topology *contains* its participant
    #    instances (mirrors the Topologies page). Represent each topology as a
    #    container drawn around its participants rather than a synthetic driver
    #    node — the real traffic source is the initiator (load run) layer.
    containers: list[dict] = []
    for rid, t in TOPOLOGY_RUNS.items():
        members = [f"pt:{pid}" for pid in t.get("participants", []) if f"pt:{pid}" in nodes]
        if members:
            containers.append({
                "id": f"topo:{rid}", "label": t.get("name", rid),
                "members": members, "instances": len(members),
                #: the network-flow id behind this run — the map links it to
                #: the authoring canvas (/network-flows?id=…).
                "network_flow_id": t.get("topology_id"),
            })

    # -- participant → downstream target edges --
    # Participant groups (fleets) a flow may target — fetched once so an action
    # naming a group resolves to a fan-out node even if the running participant's
    # captured downstream is stale (e.g. the group was defined after launch).
    groups_index = await _load_groups_index()
    for pid, r in PARTICIPANTS.items():
        src = f"pt:{pid}"
        downstream = getattr(r, "_downstream", None) or {}
        added: set[str] = set()  # edge labels already drawn for this participant
        resolved_names: set[str] = set()
        for name, cfg in downstream.items():
            resolved_names.add(name)
            # A group (fleet) target inlines as an "adapter": "group" config —
            # render it as a fleet node fanning out to its members, not a host.
            if isinstance(cfg, dict) and cfg.get("adapter") == "group":
                _emit_group(src, name, cfg, nodes, edges, port_index, added)
                continue
            host, port = _adapter_host_port(cfg)
            tgt = port_index.get(port) if port else None
            if tgt is None:
                # broker-mediated NATS (ADR-0006): match the downstream by subject
                # to whichever responder subscribes there, since it binds no port.
                for subj in _nats_subjects_of(cfg):
                    if subj in subject_index:
                        tgt = subject_index[subj]
                        break
            if tgt is None:
                ok = bool(host and port)
                key = f"host:{name}"
                if key not in nodes:
                    nodes[key] = {
                        "id": key, "kind": "host", "label": name,
                        "sublabel": (f"{host}:{port}" if ok else "target not defined"),
                        "protocol": cfg.get("protocol") if isinstance(cfg, dict) else "",
                        "port": port, "status": "external" if ok else "unresolved",
                        "received": 0, "peers": 0,
                    }
                tgt = key
            if tgt != src and name not in added:
                added.add(name)
                edges.append({"id": f"{src}->{tgt}:{name}", "source": src,
                              "target": tgt, "kind": "route", "label": name})
        # Action nodes naming a target the captured downstream doesn't cover: it
        # may be a group defined after launch (render the fleet) or a connection
        # that simply isn't defined (surface it as broken, not silently absent).
        flow = getattr(r, "flow", {}) or {}
        for n in flow.get("nodes", []):
            if n.get("kind", "action") != "action":
                continue
            cname = (n.get("config") or {}).get("connection")
            if not cname or cname in resolved_names or cname in added:
                continue
            grp = groups_index.get(cname)
            if grp:
                gcfg = await _group_adapter_config(grp)
                if gcfg:
                    _emit_group(src, cname, gcfg, nodes, edges, port_index, added)
                    continue
            key = f"host:{cname}"
            if key not in nodes:
                nodes[key] = {
                    "id": key, "kind": "host", "label": cname,
                    "sublabel": "target not defined", "protocol": "", "port": None,
                    "status": "unresolved", "received": 0, "peers": 0,
                }
            added.add(cname)
            edges.append({"id": f"{src}->{key}:{cname}", "source": src,
                          "target": key, "kind": "route", "label": cname})

    # -- initiators: load runs are what actually generate the traffic. Only
    #    CURRENTLY-RUNNING runs count — the coordinator keeps finished runs in its
    #    dict (status flips to "completed" but they're not removed), so filtering
    #    on a non-None handle alone would accumulate every run ever started. Nodes
    #    are keyed by the run LABEL (not id), so restarting "payshield_hsm_full"
    #    reuses one node instead of stacking a new one each run.
    init_env: dict[str, str] = {}  # initiator node id -> environment name
    init_scn: dict[str, str] = {}  # initiator node id -> scenario id (for targets)
    for rec in run_store.list():
        label_raw = str(rec.get("label") or "")
        if not label_raw.startswith("load:"):
            continue
        live = load_coordinator.get(rec["id"])
        if live is None or getattr(live, "status", None) != "running":
            continue  # finished/retained runs are not generating traffic
        try:
            snap = live.snapshot()
        except Exception:  # noqa: BLE001 — never let one run break the graph
            snap = {}
        name = label_raw[len("load:"):] or rec["id"]
        iid = f"load:{name}"
        tps = int(snap.get("tps") or 0)
        node = nodes.get(iid)
        if node is None:
            node = nodes[iid] = {
                "id": iid, "kind": "initiator", "label": name, "sublabel": "",
                "protocol": "", "port": None, "status": "up",
                "received": 0, "peers": 0, "tps": 0,
            }
            env_name = rec.get("environment")
            if isinstance(env_name, str) and env_name:
                init_env[iid] = env_name
            sid = getattr(getattr(live, "profile", None), "scenario_id", None)
            if isinstance(sid, str) and sid:
                init_scn[iid] = sid
        node["tps"] += tps
        node["received"] += int(snap.get("sent") or 0)
        node["sublabel"] = f"{snap.get('profile', 'load')} · {node['tps']}/s"

    conn_ports = await _connection_ports()
    conn_subjects = await _connection_subjects()
    for iid, env_name in init_env.items():
        try:
            env = await _load_env_by_name(env_name)
            adapters = env.get("adapters") or {}
        except Exception:  # noqa: BLE001 — best-effort target resolution
            adapters = {}
        seen_t: set[str] = set()
        for cname, cfg in adapters.items():
            if not isinstance(cfg, dict):
                continue
            _h, port = _adapter_host_port(cfg)
            if not port:
                port = conn_ports.get(cname)  # resolve a by-name connection ref
            tgt = port_index.get(port) if port else None
            if tgt is None:  # broker-mediated NATS: match by subject (ADR-0006)
                for subj in _nats_subjects_of(cfg) or [conn_subjects.get(cname)]:
                    if subj and subj in subject_index:
                        tgt = subject_index[subj]
                        break
            if tgt and tgt != iid and tgt not in seen_t:
                seen_t.add(tgt)
                edges.append({"id": f"{iid}->{tgt}:{cname}", "source": iid,
                              "target": tgt, "kind": "init", "label": cname})
        # The env often doesn't list the entry connection (it's only named in the
        # scenario's steps and attached at run-build). Resolve step targets too.
        sid = init_scn.get(iid)
        if sid:
            try:
                sc = await _http_get_json(f"{SCENARIO_API_URL}/scenarios/{sid}")
            except Exception:  # noqa: BLE001 — best-effort
                sc = None
            for st in (sc or {}).get("steps", []):
                cn = (st.get("config") or {}).get("connection")
                port = conn_ports.get(cn) if cn else None
                tgt = port_index.get(port) if port else None
                if tgt is None and cn:  # NATS subject match (ADR-0006)
                    subj = conn_subjects.get(cn)
                    tgt = subject_index.get(subj) if subj else None
                if tgt and tgt != iid and tgt not in seen_t:
                    seen_t.add(tgt)
                    edges.append({"id": f"{iid}->{tgt}:{cn}", "source": iid,
                                  "target": tgt, "kind": "init", "label": cn})

    # -- topology initiators: a bound scenario auto-run drives a topology's entry
    #    listeners. Surface it as an initiator node wired to each listener it
    #    targets — matched via the run env's outbound connection port → the bound
    #    listener. The edge is labelled with the connection name (e.g. "outgoing")
    #    so the driver→listener link is explicit.
    _FINISHED = {"completed", "cancelled", "error", "failed"}
    for trun in TOPOLOGY_RUNS.values():
        irun = trun.get("initiator_run")
        if not irun:
            continue
        rec = RUNS.get(irun)
        if rec is None or getattr(rec, "status", "") in _FINISHED:
            continue  # only a live driver generates traffic
        iid = f"init:{irun}"
        if iid not in nodes:
            nodes[iid] = {
                "id": iid, "kind": "initiator",
                "label": f"{trun.get('name', 'topology')} driver",
                "sublabel": "scenario", "protocol": "", "port": None,
                "status": "up", "received": 0, "peers": 0, "tps": 0,
            }
        adapters = (getattr(rec, "env", None) or {}).get("adapters") or {}
        seen_t: set[str] = set()
        for cname, cfg in adapters.items():
            if not isinstance(cfg, dict):
                continue
            _h, port = _adapter_host_port(cfg)
            if not port:
                port = conn_ports.get(cname)
            tgt = port_index.get(port) if port else None
            if tgt is None:  # broker-mediated NATS: match by subject (ADR-0006)
                for subj in _nats_subjects_of(cfg) or [conn_subjects.get(cname)]:
                    if subj and subj in subject_index:
                        tgt = subject_index[subj]
                        break
            if tgt and tgt != iid and tgt not in seen_t:
                seen_t.add(tgt)
                edges.append({"id": f"{iid}->{tgt}:{cname}", "source": iid,
                              "target": tgt, "kind": "init", "label": cname})

    # -- external clients → listeners whose inbound is NOT already explained by
    #    internal routing or a named initiator. A listener fed by a driver /
    #    route / fleet member / initiator edge already shows where its traffic
    #    comes from; a clients→listener edge on top is the same link twice. Only
    #    listeners with no such in-edge (genuinely unattributed load) get one.
    #    Skip the whole aggregate when named initiators (load runs) exist: in that
    #    case the real generators are on the board, so attributing a listener's
    #    inbound to an anonymous "external clients" blob is misleading. The catch-
    #    all only appears when there are no initiators to explain the traffic.
    has_initiators = any(n["kind"] == "initiator" for n in nodes.values())
    explained = {e["target"] for e in edges if e["kind"] in ("route", "member", "init")}
    clients_added = False
    if not has_initiators:
        for nid, node in list(nodes.items()):
            if (node["kind"] in ("simulator", "participant")
                    and node.get("peers") and nid not in explained):
                if not clients_added:
                    nodes["clients"] = {
                        "id": "clients", "kind": "clients", "label": "external clients",
                        "sublabel": "inbound sockets", "status": "up", "received": 0,
                        "peers": 0,
                    }
                    clients_added = True
                edges.append({"id": f"clients->{nid}", "source": "clients", "target": nid,
                              "kind": "client", "label": str(node["peers"])})

    node_list = list(nodes.values())
    return {
        "nodes": node_list,
        "edges": edges,
        "containers": containers,
        "generated_at": time.time(),
        "counts": {
            "initiators": sum(1 for n in node_list if n["kind"] == "initiator"),
            "topologies": len(containers),
            "participants": sum(1 for n in node_list if n["kind"] == "participant"),
            "simulators": sum(1 for n in node_list if n["kind"] == "simulator"),
            "hosts": sum(1 for n in node_list if n["kind"] == "host"),
            "edges": len(edges),
        },
    }


@app.get("/network-graph/stream")
async def network_graph_stream(interval: float = 2.0) -> StreamingResponse:
    """SSE push of the network graph — one `data: {…}` frame per tick, same
    payload as GET /network-graph. Replaces N independent portal poll loops
    with one held connection per client; the portal falls back to polling when
    the stream can't be opened. `interval` is clamped to 0.5–10s. Auth rides
    the normal app-level gate (the portal opens this via fetch with a Bearer
    header — EventSource can't send headers, so it isn't used)."""
    period = max(0.5, min(10.0, interval))

    async def gen():
        while True:
            try:
                snap = await network_graph()
                yield "data: " + json.dumps(snap) + "\n\n"
            except Exception:  # noqa: BLE001 — one bad frame must not kill the stream
                log.debug("network-graph stream frame failed", exc_info=True)
                yield ": frame-error\n\n"  # SSE comment keeps the pipe warm
            await asyncio.sleep(period)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- saved-simulator registry --------------------------------------------------

def _saved_info(saved: dict) -> dict:
    """Saved config + live running state (port/throughput) when it's up."""
    sid = saved["id"]
    out = dict(saved)
    running = SIMULATORS.get(sid)
    out["running"] = running is not None
    if running is not None:
        buf = SIM_SAMPLES.get(sid) or []
        out["port"] = running.port
        out["received"] = len(running.received)
        out["faults"] = getattr(running, "faults", 0)
        out["rps"] = buf[-1]["rps"] if buf else 0
        out["playground_hits"] = PLAYGROUND_HITS.get(sid, 0)
    return out


@app.get("/simulator-configs")
async def list_saved_simulators() -> list[dict]:
    return [_saved_info(s) for s in simulator_store.list()]


@app.post("/simulator-configs", status_code=201)
async def create_saved_simulator(draft: SavedSimulatorDraft) -> dict:
    return _saved_info(simulator_store.create(draft.model_dump()))


@app.get("/simulator-configs/{cid}")
async def get_saved_simulator(cid: str) -> dict:
    saved = simulator_store.get(cid)
    if saved is None:
        raise HTTPException(404, f"no saved simulator '{cid}'")
    return _saved_info(saved)


@app.put("/simulator-configs/{cid}")
async def update_saved_simulator(cid: str, patch: SavedSimulatorPatch) -> dict:
    saved = simulator_store.update(cid, patch.model_dump(exclude_unset=True))
    if saved is None:
        raise HTTPException(404, f"no saved simulator '{cid}'")
    return _saved_info(saved)


@app.post("/simulator-configs/{cid}/start", status_code=201)
async def start_saved_simulator(cid: str) -> dict:
    saved = simulator_store.get(cid)
    if saved is None:
        raise HTTPException(404, f"no saved simulator '{cid}'")
    if cid not in SIMULATORS:
        await _start_responder(cid, saved["label"], saved["config"])
    return _saved_info(simulator_store.get(cid))


@app.post("/simulator-configs/{cid}/stop")
async def stop_saved_simulator(cid: str) -> dict:
    if not simulator_store.has(cid):
        raise HTTPException(404, f"no saved simulator '{cid}'")
    r = SIMULATORS.pop(cid, None)
    if r is not None:
        await r.stop()
        _drop_sim_runtime(cid, getattr(r, "_label", cid))
    return _saved_info(simulator_store.get(cid))


@app.post("/simulator-configs/{cid}/enabled")
async def set_saved_simulator_enabled(cid: str, enabled: bool) -> dict:
    saved = simulator_store.set_enabled(cid, enabled)
    if saved is None:
        raise HTTPException(404, f"no saved simulator '{cid}'")
    return _saved_info(saved)


@app.delete("/simulator-configs/{cid}", status_code=204, response_class=Response)
async def delete_saved_simulator(cid: str) -> Response:
    """Forget a saved simulator (stopping it first if it's running)."""
    r = SIMULATORS.pop(cid, None)
    if r is not None:
        await r.stop()
        _drop_sim_runtime(cid, getattr(r, "_label", cid))
    try:
        simulator_store.delete(cid)
    except KeyError:
        raise HTTPException(404, f"no saved simulator '{cid}'")
    return Response(status_code=204)


# -- live sampler + auto-start -------------------------------------------------

async def _simulator_sampler_loop(interval: float = 2.0) -> None:
    """Every ``interval`` seconds, derive each running simulator's throughput
    and append a sample, and refresh the Prometheus gauges. Best-effort; one bad
    iteration never kills the loop."""
    while True:
        await asyncio.sleep(interval)
        try:
            now = time.time()
            for sid, r in list(SIMULATORS.items()):
                received = len(r.received)
                prev = _SIM_PREV.get(sid, received)
                rps = max(0.0, (received - prev) / interval)
                _SIM_PREV[sid] = received
                faults = getattr(r, "faults", 0)
                buf = SIM_SAMPLES.setdefault(sid, [])
                buf.append({
                    "t": round(now, 1), "rps": round(rps, 2),
                    "received": received, "faults": faults,
                })
                if len(buf) > SIM_SAMPLE_CAP:
                    del buf[: len(buf) - SIM_SAMPLE_CAP]
                label = getattr(r, "_label", sid)
                SIMULATOR_UP.set(1, sim_id=sid, label=label)
                SIMULATOR_MESSAGES.set(received, sim_id=sid, label=label)
                SIMULATOR_RPS.set(rps, sim_id=sid, label=label)
                SIMULATOR_FAULTS.set(faults, sim_id=sid, label=label)
        except Exception:  # noqa: BLE001
            log.warning("simulator sampler iteration failed", exc_info=True)


@app.on_event("startup")
async def _start_simulator_sampler() -> None:
    asyncio.create_task(_simulator_sampler_loop())


@app.on_event("startup")
async def _autostart_simulators() -> None:
    """Bring up every saved simulator flagged ``enabled`` on boot."""
    if os.environ.get("DISABLE_SIMULATOR_AUTOSTART") == "1":
        return
    for saved in simulator_store.enabled():
        if saved["id"] in SIMULATORS:
            continue
        try:
            await _start_responder(saved["id"], saved["label"], saved["config"])
            log.info("auto-started simulator '%s'", saved["id"])
        except Exception:  # noqa: BLE001 — never block boot on one bad config
            log.warning("failed to auto-start simulator '%s'", saved["id"], exc_info=True)


# -- durable participant / topology desired-state -----------------------------

def _persist_runtime() -> None:
    """Snapshot the live network's desired state (standalone participant flows +
    running topologies) so it can be restored after a restart. No-op when
    persistence is disabled (``ORCH_RUNTIME_FILE=:memory:``)."""
    if RUNTIME_STATE_FILE == ":memory:":
        return
    state = {
        "standalone": sorted({
            getattr(r, "_flow_id", None)
            for r in PARTICIPANTS.values()
            if not getattr(r, "_topology_run", None) and getattr(r, "_flow_id", None)
        }),
        "network_flows": sorted({
            t.get("topology_id") for t in TOPOLOGY_RUNS.values()
            if t.get("topology_id")
        }),
    }
    try:
        with open(RUNTIME_STATE_FILE, "w") as fh:
            json.dump(state, fh)
    except OSError:
        log.warning("could not persist runtime state to %s", RUNTIME_STATE_FILE,
                    exc_info=True)


@app.on_event("startup")
async def _autostart_runtime() -> None:
    """Restore topology runs + standalone participant listeners persisted by
    :func:`_persist_runtime`. Best-effort: a single bad entry never blocks boot."""
    if os.environ.get("DISABLE_PARTICIPANT_AUTOSTART") == "1":
        return
    if RUNTIME_STATE_FILE == ":memory:" or not os.path.exists(RUNTIME_STATE_FILE):
        return
    try:
        with open(RUNTIME_STATE_FILE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        log.warning("could not read runtime state %s", RUNTIME_STATE_FILE, exc_info=True)
        return
    # "topologies" is the pre-ADR-0004 key: those ids are valid network-flow
    # ids after migration, so a legacy state file restores through the same path.
    restore = list(dict.fromkeys(
        [*state.get("network_flows", []), *state.get("topologies", [])]))
    for nid in restore:
        try:
            await start_network_flow(nid)
            log.info("auto-started network flow '%s'", nid)
        except Exception:  # noqa: BLE001 — never block boot on one bad network
            log.warning("failed to auto-start network flow '%s'", nid, exc_info=True)
    for fid in state.get("standalone", []):
        if _flow_in_running_topology(fid):
            continue  # already brought up by a restored topology
        try:
            await _launch_participant(fid)
            log.info("auto-started participant flow '%s'", fid)
        except Exception:  # noqa: BLE001 — never block boot on one bad flow
            log.warning("failed to auto-start participant '%s'", fid, exc_info=True)


async def _load_pack(pack_id: str) -> dict:
    try:
        return await _http_get_json(f"{SCENARIO_API_URL}/packs/{pack_id}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not load pack '{pack_id}': {exc}")


@app.get("/runs/{run_id}/certification")
async def run_certification(run_id: str, pack: str) -> dict:
    """Certification summary: compliance % of a run against a test-case pack."""
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, f"no run '{run_id}'")
    return _certify(detail, await _load_pack(pack))


@app.get("/runs/{run_id}/certification/html")
async def run_certification_html(run_id: str, pack: str) -> HTMLResponse:
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, f"no run '{run_id}'")
    return HTMLResponse(_certification_html(_certify(detail, await _load_pack(pack))))


# -- Go/No-Go sign-off (ADR-0003) -------------------------------------------
# Certifying a run evaluates explicit gates, stamps provenance, and freezes an
# immutable, tamper-evident snapshot — the fact that gives the green light to
# production. Distinct from the live certification view above.


def _principal(request: Request) -> str:
    """Who is acting — from the verified auth claims, else 'unknown'."""
    auth = getattr(request.state, "auth", None) or {}
    return str(auth.get("sub") or auth.get("email") or "unknown")


def _signoff_endpoints(detail: dict) -> list[dict]:
    """Runtime-resolved target endpoints recorded on the run, if any.

    Prefers the endpoints stamped onto the summary at run completion
    (:func:`_resolved_endpoints`); falls back to run-level fields for older runs.
    Hosts/ports are *targets*, not credentials; secret-named fields are masked by
    the store's box at rest. Returns ``[]`` when the run didn't record them.
    """
    eps = (
        (detail.get("summary") or {}).get("endpoints")
        or detail.get("endpoints")
        or detail.get("targets")
        or []
    )
    return eps if isinstance(eps, list) else []


class CertifyRequest(BaseModel):
    pack: str
    policy: dict | None = None
    project: str | None = None


class ApprovalRequest(BaseModel):
    decision: str = "approved"
    note: str = ""


@app.post("/runs/{run_id}/certify")
async def certify_run(run_id: str, body: CertifyRequest, request: Request) -> dict:
    """Evaluate gates + stamp provenance + freeze an immutable sign-off snapshot.

    The regression gate compares against the last GO run for this
    ``(project, pack, environment)``. A GO verdict advances that baseline.
    """
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, f"no run '{run_id}'")
    pack = await _load_pack(body.pack)
    env = detail.get("environment")

    baseline_id = signoff_store.baseline_run(
        project=body.project, pack=body.pack, environment=env,
    )
    baseline = run_store.get(baseline_id) if baseline_id else None

    gate = _evaluate_gates(detail, policy=body.policy, pack=pack, baseline=baseline)
    prov = _provenance(detail, {
        "pack": {"id": pack.get("id"), "version": pack.get("version")},
        "endpoints": _signoff_endpoints(detail),
        "triggered_by": _principal(request),
        "baseline_run_id": baseline_id,
        "playground_traffic": _playground_traffic(detail),
    })
    summary = detail.get("summary") or {}
    chash = _content_hash(summary=summary, gate_result=gate, prov=prov)

    return signoff_store.create({
        "run_id": run_id, "project": body.project, "pack": body.pack,
        "environment": env, "verdict": gate["verdict"], "gate_result": gate,
        "provenance": prov, "content_hash": chash, "summary": summary,
        "created_by": _principal(request),
    })


@app.get("/signoffs")
async def list_signoffs(
    run_id: str | None = None, project: str | None = None,
    verdict: str | None = None,
) -> dict:
    return {"signoffs": signoff_store.list(
        run_id=run_id, project=project, verdict=verdict)}


@app.get("/signoffs/{sid}")
async def get_signoff(sid: str) -> dict:
    doc = signoff_store.get(sid)
    if doc is None:
        raise HTTPException(404, f"no sign-off '{sid}'")
    return doc


@app.get("/signoffs/{sid}/html")
async def signoff_html_doc(sid: str) -> Response:
    """Printable, self-contained sign-off document (Print → PDF for the artifact)."""
    doc = signoff_store.get(sid)
    if doc is None:
        raise HTTPException(404, f"no sign-off '{sid}'")
    return Response(
        content=_signoff_html(doc), media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="signoff-{sid}.html"'},
    )


@app.post("/signoffs/{sid}/approve")
async def approve_signoff(sid: str, body: ApprovalRequest, request: Request) -> dict:
    """Append to the sign-off's approval trail (the frozen evidence is untouched)."""
    doc = signoff_store.add_approval(
        sid, by=_principal(request), decision=body.decision, note=body.note,
    )
    if doc is None:
        raise HTTPException(404, f"no sign-off '{sid}'")
    return doc


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    """Full run detail incl. per-scenario / per-step results for the report."""
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, "run not found")
    return detail


#: finding categories that mean "the wire, not the scenario" — these trigger a
#: live platform-doctor pass so the diagnosis shows what's broken *right now*
_DOCTOR_TRIGGER_CATS = ("unreachable", "timeout", "tls", "auth", "binding")


@app.get("/runs/{run_id}/diagnose")
async def diagnose_run_endpoint(run_id: str) -> dict:
    """Triage a finished run (functional or load): classify the failure against
    a taxonomy and return likely causes + suggested fixes. For a live load run
    we diagnose the coordinator's current snapshot; otherwise the persisted
    detail.

    When the failure looks connectivity-class, a scoped platform-doctor pass
    (connections + listeners layers, see ``/diagnostics``) runs too and its
    problems ride along under ``doctor`` — the run report can then say "this
    failed because visa was unreachable, and visa is STILL down" without the
    user visiting the Diagnostics page."""
    live = load_coordinator.get(run_id)
    if live is not None:
        result = _diagnose(live.snapshot())
    else:
        detail = run_store.get(run_id)
        if detail is None:
            raise HTTPException(404, "run not found")
        result = _diagnose(detail)

    connectivity = any(
        cat in str(f.get("code", ""))
        for f in result.get("findings", [])
        for cat in _DOCTOR_TRIGGER_CATS
        if f.get("severity") == "error"
    )
    if connectivity:
        try:
            rep = await diagnostics(layers="connections,listeners")
            result["doctor"] = {
                "status": rep["status"],
                "problems": [c for c in rep["checks"]
                             if c["status"] in ("fail", "warn")],
            }
        except Exception:  # noqa: BLE001 — doctor is best-effort enrichment
            pass
    return result


# -- regression: JUnit export + run-to-run compare ---------------------------
# Generators live in the report-service library (imported above as _junit_xml,
# _html_report, _index_steps); the endpoints below just wire them to run data.


@app.get("/runs/{run_id}/html")
async def run_html(run_id: str) -> Response:
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, "run not found")
    return Response(
        content=_html_report(detail), media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}.html"'},
    )


@app.get("/runs/{run_id}/junit")
async def run_junit(run_id: str) -> Response:
    detail = run_store.get(run_id)
    if detail is None:
        raise HTTPException(404, "run not found")
    xml = _junit_xml(detail)
    return Response(
        content=xml, media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}.xml"'},
    )


@app.get("/runs/{run_id}/compare")
async def compare_run(run_id: str, to: str | None = None) -> dict:
    """Diff a run against another (default: the previous like-for-like run).

    Reports per-step status changes — chiefly ``regressed`` (was passing, now
    failing) and ``fixed`` (was failing, now passing)."""
    cur = run_store.get(run_id)
    if cur is None:
        raise HTTPException(404, "run not found")
    base_id = to or run_store.previous(run_id)
    base = run_store.get(base_id) if base_id else None

    now = _index_steps(cur.get("summary"))
    was = _index_steps(base.get("summary")) if base else {}
    ok = {"passed"}

    changes: list[dict] = []
    counts = {"regressed": 0, "fixed": 0, "changed": 0, "added": 0, "removed": 0}
    for key in sorted(set(now) | set(was)):
        scenario, step = key
        a, b = was.get(key), now.get(key)
        if a is None:
            kind = "added"
        elif b is None:
            kind = "removed"
        elif a == b:
            continue
        elif a in ok and b not in ok:
            kind = "regressed"
        elif a not in ok and b in ok:
            kind = "fixed"
        else:
            kind = "changed"
        counts[kind] += 1
        changes.append({
            "scenario": scenario, "step": step,
            "from": a, "to": b, "kind": kind,
        })
    return {
        "run_id": run_id, "base_run_id": base_id, "base_found": base is not None,
        "summary": counts, "changes": changes,
    }


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    # 1) owned by this replica → cancel the task directly.
    rec = RUNS.get(run_id)
    if rec is not None and rec.task and not rec.task.done():
        rec.task.cancel()
        return {"id": run_id, "status": "cancelling"}

    # 2) running on another replica → route the cancel via the control channel.
    if await run_control.request_cancel(run_id):
        return {"id": run_id, "status": "cancelling"}

    # 3) already finished (persisted) → report its terminal status, not a 404.
    detail = run_store.get(run_id)
    if detail is not None:
        return {"id": run_id, "status": detail.get("status", "completed")}

    raise HTTPException(404, "run not found")


# -- debugger control (step-through / breakpoints) ----------------------------

class BreakpointsBody(BaseModel):
    breakpoints: list[str] = []


def _debug_session(run_id: str) -> DebugSession:
    rec = RUNS.get(run_id)
    if rec is None or rec.debug is None:
        raise HTTPException(404, "no active debug session for this run")
    return rec.debug


@app.post("/runs/{run_id}/debug/step")
async def debug_step(run_id: str) -> dict:
    """Run the paused node and pause again at the next one."""
    sess = _debug_session(run_id)
    sess.step_mode = True
    sess.resume.set()
    return {"status": "stepping"}


@app.post("/runs/{run_id}/debug/continue")
async def debug_continue(run_id: str) -> dict:
    """Resume until the next breakpoint (or the end)."""
    sess = _debug_session(run_id)
    sess.step_mode = False
    sess.resume.set()
    return {"status": "running"}


@app.post("/runs/{run_id}/debug/pause")
async def debug_pause(run_id: str) -> dict:
    """Break in at the next node."""
    sess = _debug_session(run_id)
    sess.pause_requested = True
    return {"status": "pausing"}


@app.post("/runs/{run_id}/debug/breakpoints")
async def debug_breakpoints(run_id: str, body: BreakpointsBody) -> dict:
    sess = _debug_session(run_id)
    sess.breakpoints = set(body.breakpoints)
    return {"breakpoints": sorted(sess.breakpoints)}


# -- WebSocket transport with resume ------------------------------------------

@app.websocket("/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    # resume cursor: ?last_event_id=<id> (or the client's first message)
    last_event_id = websocket.query_params.get("last_event_id")
    try:
        async for entry_id, event in backbone.stream(run_id, after_id=last_event_id):
            await websocket.send_json({"id": entry_id, **event})
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


# -- single-node execution (editor "Execute step") ----------------------------

class ExecuteNodeRequest(BaseModel):
    node: dict[str, Any]
    # accumulated upstream results: {step_id: {request, response}}
    context: dict[str, Any] = {}
    environment: dict[str, Any] | None = None


@app.post("/nodes/execute")
async def execute_node(req: ExecuteNodeRequest) -> dict:
    """Run a single node with the supplied input context and return its output.

    Powers the editor's per-node "Execute step": ``http`` and ``code`` nodes run
    standalone; ``action`` nodes run against the mock environment (or an explicit
    one) so they work without real hardware; control nodes report the branch they
    would take.
    """
    node = req.node or {}
    kind = node.get("kind", "action")
    context = req.context or {}
    import time as _time
    start = _time.monotonic()

    def _done(payload: dict) -> dict:
        payload.setdefault("duration_ms", int((_time.monotonic() - start) * 1000))
        return payload

    if kind == "crypto":
        from worker.engine.crypto_tools import run_crypto
        from worker.engine.variables import resolve_value, UnresolvedReferenceError
        cfg = node.get("config") or {}
        op = cfg.get("operation", "")
        params = {}
        for k, v in cfg.items():
            if k == "operation":
                continue
            if isinstance(v, str):
                try:
                    params[k] = str(resolve_value(v, context))
                except UnresolvedReferenceError:
                    params[k] = v
            else:
                params[k] = v
        res = run_crypto(op, params)
        if "error" in res:
            return _done({"ok": False, "status": "error", "error": res["error"]})
        return _done({"ok": True, "status": "passed", "response": res})

    if kind == "init":
        from worker.engine.variables import resolve_value, UnresolvedReferenceError
        cfg = node.get("config") or {}
        raw = cfg.get("data")
        try:
            if isinstance(raw, str):
                obj = json.loads(raw) if raw.strip() else {}
            elif isinstance(raw, (dict, list)):
                obj = raw
            else:
                obj = {}
            resolved = resolve_value(obj, context)
            return _done({"ok": True, "status": "passed", "response": resolved})
        except (json.JSONDecodeError, ValueError, UnresolvedReferenceError) as exc:
            return _done({"ok": False, "status": "error", "error": f"init data error: {exc}"})

    if kind == "http":
        from worker.engine.http_runner import run_http
        res = await run_http(node.get("config") or {}, context)
        return _done({
            "ok": res.ok,
            "status": "passed" if res.ok else "failed",
            "request": res.request,
            "response": res.response,
            "error": res.error,
        })

    if kind == "code":
        from worker.engine.code_runner import network_isolation_active, run_code
        from worker.engine.variables import resolve_inputs

        # Code nodes execute arbitrary user code. If the API is open (auth
        # disabled) we only run them when a network sandbox is active or an
        # operator has explicitly opted in for trusted local dev. This keeps an
        # unauthenticated /nodes/execute from becoming RCE/SSRF.
        allow_unauth = os.environ.get("PAYPROBE_ALLOW_UNAUTH_CODE", "").lower() in (
            "1", "true", "yes",
        )
        if not auth_enforced() and not allow_unauth and not network_isolation_active():
            return _done({
                "ok": False, "status": "error",
                "error": (
                    "code execution is disabled while auth is off and no sandbox "
                    "is active. Enable auth, run with PAYPROBE_CODE_SANDBOX, or set "
                    "PAYPROBE_ALLOW_UNAUTH_CODE=1 for trusted local dev."
                ),
            })

        cfg = node.get("config") or {}
        res = await run_code(
            cfg.get("language", "python"), cfg.get("code", ""), context,
            cfg.get("timeout_ms"), inputs=resolve_inputs(cfg.get("inputs"), context),
        )
        return _done({
            "ok": res.ok,
            "status": "passed" if res.ok else "error",
            "response": res.output,
            "error": res.error,
            "logs": res.logs,
        })

    if kind == "action":
        from worker.engine.variables import resolve_value, UnresolvedReferenceError
        env = req.environment if req.environment is not None else _load_defaults()[0]
        engine = WorkerEngine(env)
        try:
            await engine.warmup_adapters()
            try:
                payload = resolve_value(node.get("payload", {}), context)
            except UnresolvedReferenceError as exc:
                return _done({"ok": False, "status": "error",
                              "error": f"unresolved reference: {exc}"})
            sr = await engine._execute_step(
                node.get("target", ""), node.get("action", ""), payload,
                node.get("assertions", []) or [],
            )
            ok = sr.success and all(getattr(a, "passed", True) for a in sr.assertions)
            return _done({
                "ok": ok,
                "status": "passed" if ok else "failed",
                "request": sr.request_payload,
                "response": sr.response_payload,
                "error": sr.error,
                "assertions": [a.__dict__ for a in sr.assertions],
            })
        except Exception as exc:  # noqa: BLE001
            return _done({"ok": False, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await engine.teardown()

    # control nodes: report the routing decision without side effects
    return _done({
        "ok": True,
        "status": "passed",
        "response": {"kind": kind, "note": "control node — run the full scenario to see routing"},
    })


class ValidateCodeRequest(BaseModel):
    language: str
    code: str = ""


@app.post("/code/validate")
async def validate_code(req: ValidateCodeRequest) -> dict:
    """Syntax-check a code-node snippet without running it (editor diagnostics)."""
    from worker.engine.code_validate import validate_syntax
    return await validate_syntax(req.language, req.code)


class TestConnectionRequest(BaseModel):
    """A worker-shaped adapter config to probe (host/port/protocol/...)."""
    config: dict[str, Any]


class HsmCommandRequest(BaseModel):
    """One payShield host command to send to a running simulator/appliance."""
    host: str = "127.0.0.1"
    port: int = 1500
    #: high-level action (generate_cvv, verify_pin, nc, …) or "raw" for a
    #: hand-built command (set ``command`` + ``data``).
    action: str = "nc"
    params: dict[str, Any] = {}
    command: str | None = None  # raw: 2-char command code
    data: str | None = None     # raw: command data tail
    header: str = "HDR1"
    response_timeout_sec: float = 5


@app.post("/hsm/command")
async def hsm_command(req: HsmCommandRequest) -> dict:
    """Send one payShield host command to an HSM (the bundled simulator or a real
    appliance) via the host-command client adapter, and return the parsed reply.

    ``response`` carries ``response_code``, ``error_code``, ``ok`` and the raw
    ``data``, plus any action-specific field (``cvv``, ``key``, ``kcv`` …). Never
    raises — a connection/timeout failure is a normal ``{ok: false, error}``."""
    from worker.adapters.hsm.adapter import HSMAdapter

    cfg = {
        "host": req.host,
        "port": req.port,
        "header": req.header,
        "response_timeout_sec": req.response_timeout_sec,
    }
    adapter = HSMAdapter(cfg)
    payload = dict(req.params or {})
    if req.command:
        payload.setdefault("command", req.command)
    if req.data is not None:
        payload.setdefault("data", req.data)
    try:
        await adapter.connect()
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "response": {}}
    try:
        sr = await adapter.execute(req.action, payload)
        return {
            "ok": sr.success,
            "request": sr.request_payload,
            "response": sr.response_payload,
            "error": sr.error,
            "duration_ms": sr.duration_ms,
        }
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass


# Adapters whose reachability probe needs a host+port (vs base_url / engine).
# payShield + header-echo HSM links are TCP under the hood.
_HOST_PORT_ADAPTERS = {"tcp", "payshield", "hsm_client", "hsm_tcp", "switch", "acquirer"}


@app.post("/connections/test")
async def test_connection(req: TestConnectionRequest) -> dict:
    """Open a connection, sign on / health-check it, and report the result.

    Returns ``{ok, latency_ms, error}``. Never raises — a failure to connect is
    a normal result the UI shows. Reconnect is disabled for the probe so a bad
    host fails fast instead of retrying.
    """
    cfg = dict(req.config)
    impl = (cfg.get("adapter") or cfg.get("type") or "tcp").lower()

    if impl == "grpc":
        return await _test_grpc_connection(cfg)

    # Host/port-based adapters fail clearly if unconfigured, instead of a cryptic
    # connect error. REST (base_url) and DB (engine/host) adapters skip this.
    if impl in _HOST_PORT_ADAPTERS and not (cfg.get("host") and cfg.get("port")):
        return {"ok": False, "latency_ms": 0,
                "error": "connection has no host/port configured"}

    # Resolve the adapter implementation generically so the probe works for every
    # registered adapter (tcp, http/REST, payShield, db_probe, …), not just TCP.
    from worker.adapters.registry import ADAPTER_MAP

    cls = ADAPTER_MAP.get(impl)
    if cls is None:
        return {"ok": False, "latency_ms": 0,
                "error": f"unknown adapter '{impl}'"}

    cfg.setdefault("connect_timeout_sec", 5)
    cfg.setdefault("response_timeout_sec", 5)
    cfg["reconnect"] = {"enabled": False}

    adapter = cls(cfg)
    start = time.monotonic()
    try:
        await asyncio.wait_for(adapter.connect(),
                               timeout=float(cfg["connect_timeout_sec"]) + 1)
        healthy = await adapter.health_check()
        latency = int((time.monotonic() - start) * 1000)
        return {"ok": bool(healthy), "latency_ms": latency,
                "error": None if healthy else "connected, but health check failed"}
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"ok": False, "latency_ms": int((time.monotonic() - start) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _test_grpc_connection(cfg: dict) -> dict:
    """Probe a gRPC connection: open the channel and wait until it's ready.

    Requires grpcio (imported lazily by the adapter) and a descriptor set —
    the adapter's ``connect`` loads it before creating the channel. Any missing
    piece surfaces as a normal ``{ok: false, error}`` result the UI shows.
    """
    target = cfg.get("target") or (
        f"{cfg.get('host')}:{cfg.get('port')}" if cfg.get("host") and cfg.get("port") else None
    )
    if not target:
        return {"ok": False, "latency_ms": 0,
                "error": "gRPC connection has no target (or host/port) configured"}
    try:
        from worker.adapters.grpc.adapter import GrpcAdapter
    except Exception as exc:  # noqa: BLE001 - grpcio not installed, etc.
        return {"ok": False, "latency_ms": 0, "error": f"gRPC unavailable: {exc}"}

    probe = dict(cfg)
    probe.setdefault("timeout_sec", 5)
    adapter = GrpcAdapter(probe)
    start = time.monotonic()
    try:
        await asyncio.wait_for(adapter.connect(), timeout=6)
        healthy = await adapter.health_check()
        latency = int((time.monotonic() - start) * 1000)
        return {"ok": bool(healthy), "latency_ms": latency,
                "error": None if healthy else "channel did not become ready"}
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"ok": False, "latency_ms": int((time.monotonic() - start) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass


# -- playground (ADR-0007: unified ad-hoc execution surface) -------------------
#
# One place to act on ANY addressable resource interactively: fire a message
# through a registered connection (resolved server-side, so secrets never
# round-trip — invariant #8), at a running simulator or network participant,
# through a participant group, at a raw host/port (parity with /hsm/command),
# or run a local crypto function. Execution is fully DELEGATED to the existing
# owners (WorkerEngine._execute_step / run_crypto) — no second engine. The
# Playground never starts or stops anything (invariant #5 reads across: it is
# a client).

#: per-user in-memory interaction ring (the CaptureBuffer pattern — in-memory
#: by design, not a file store). Promote keepers via save-as-scenario.
playground_history = PlaygroundHistory(
    int(os.environ.get("PLAYGROUND_HISTORY_MAX", "200")))

#: sid -> count of playground-originated messages sent to a running simulator.
#: Surfaced in _sim_info as ``playground_hits`` so certification-time stats
#: contamination is visible (ADR-0007 provenance concern).
PLAYGROUND_HITS: dict[str, int] = {}

#: sid -> recent hit timestamps (epoch seconds), bounded. Lets certification
#: count only the playground traffic that landed *inside a run's window*
#: (started_at..completed_at) — an honest contamination note, not the lifetime
#: counter. Independent of the per-user history ring by design (contamination
#: is a property of the simulator, not of who poked it).
PLAYGROUND_HIT_LOG: dict[str, list[float]] = {}
_PLAYGROUND_HIT_LOG_MAX = 2000


def _record_playground_hit(sid: str) -> None:
    """Tag one playground fire against a running simulator (counter + windowed
    log). Called only when the resolved target is a live simulator."""
    PLAYGROUND_HITS[sid] = PLAYGROUND_HITS.get(sid, 0) + 1
    log = PLAYGROUND_HIT_LOG.setdefault(sid, [])
    log.append(time.time())
    if len(log) > _PLAYGROUND_HIT_LOG_MAX:
        del log[: len(log) - _PLAYGROUND_HIT_LOG_MAX]


def _playground_traffic(detail: dict) -> dict:
    """Playground contamination for a run's window, for the provenance stamp.

    Counts, per simulator, the playground hits whose timestamp falls between the
    run's ``started_at`` and ``completed_at`` (ISO 8601). A run that overlapped
    ad-hoc pokes at a live simulator has inflated stats/traces; recording it lets
    a sign-off note the contamination instead of hiding it. Best-effort: unusable
    timestamps yield an empty report (never blocks a certify)."""
    from datetime import datetime

    def _parse(ts: Any) -> float | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(str(ts)).timestamp()
        except (ValueError, TypeError):
            return None

    start = _parse(detail.get("started_at"))
    # an in-flight or clock-skewed run: treat "now" as the window end.
    end = _parse(detail.get("completed_at")) or time.time()
    if start is None:
        return {"total": 0, "by_simulator": {}, "window": None}
    by_sim: dict[str, int] = {}
    for sid, stamps in PLAYGROUND_HIT_LOG.items():
        n = sum(1 for t in stamps if start <= t <= end)
        if n:
            by_sim[sid] = n
    return {
        "total": sum(by_sim.values()),
        "by_simulator": by_sim,
        "window": {"from": detail.get("started_at"),
                   "to": detail.get("completed_at")},
    }


def _playground_user(request: Request) -> str:
    """History partition key: the JWT ``sub`` when the gate verified one, else
    a shared bucket (open dev gate)."""
    claims = getattr(request.state, "auth", None) or {}
    return str(claims.get("sub") or "anonymous")


class PlaygroundTarget(BaseModel):
    """A reference to something callable right now (see /playground/targets)."""
    #: connection | group | simulator | participant | raw | function
    kind: str
    #: connection name / group name-or-id / simulator id / participant id.
    id: str | None = None
    #: for kind=connection: resolve through the override matrix for this env.
    environment: str | None = None
    #: for kind=raw (parity with /hsm/command's hand-typed endpoint):
    host: str | None = None
    port: int | None = None
    adapter: str | None = None       # default "tcp"
    protocol: str | None = None      # e.g. iso8583 | header_echo
    config: dict[str, Any] = {}      # extra raw adapter keys (framing, header, …)


class PlaygroundExecuteRequest(BaseModel):
    target: PlaygroundTarget
    action: str
    payload: dict[str, Any] = {}
    #: pin the ISO 8583 dialect: the format's DE table is snapshotted into the
    #: payload (the per-step ``fields`` override the worker encode honours).
    message_format_id: str | None = None
    label: str | None = None


class PlaygroundSaveRequest(BaseModel):
    name: str
    #: history entries to promote (default: every entry, oldest first).
    seqs: list[int] | None = None
    project_id: str | None = None


#: wire-shape keys copied from a live responder's config to the client adapter
#: config, so an ad-hoc caller packs/frames exactly what the listener decodes.
_WIRE_KEYS = ("fields", "framing", "encoding", "header_bytes", "correlation",
              "message_format_id")


def _client_config_for_responder(r: Any) -> dict:
    """A worker-shaped *client* adapter config that reaches a live responder
    (simulator or participant listener) — the reverse of its listen config."""
    rcfg = dict(getattr(r, "config", {}) or {})
    proto = str(getattr(r, "protocol", "") or rcfg.get("protocol") or "").lower()
    host = rcfg.get("host") or "127.0.0.1"
    port = getattr(r, "port", None)
    cfg: dict[str, Any]
    if proto == "nats":
        subjects = list(getattr(r, "subjects", []) or [])
        cfg = {"adapter": "nats",
               "subject": subjects[0] if subjects else rcfg.get("subject")}
        if rcfg.get("servers"):
            cfg["servers"] = rcfg["servers"]
        else:
            cfg["host"], cfg["port"] = host, rcfg.get("port") or 4222
        return cfg
    if proto in ("http", "cybersource"):
        cfg = {"adapter": "http", "base_url": f"http://{host}:{port}"}
        if proto == "cybersource":
            # The sim's auth gate accepts any bearer credential — carry one so
            # ad-hoc pokes at the bundled gateway work out of the box. (Real
            # endpoints are reached via connections, which bring their own
            # configured auth.)
            cfg["headers"] = {"Authorization": "Bearer playground"}
        if rcfg.get("actions"):
            cfg["actions"] = rcfg["actions"]
        return cfg
    if proto == "payshield":
        return {"adapter": "payshield", "host": host, "port": port,
                "response_timeout_sec": 5}
    # iso8583 / visa / header_echo — the universal TCP adapter, wire-matched.
    cfg = {"adapter": "tcp",
           "protocol": "header_echo" if proto == "header_echo" else "iso8583",
           "host": host, "port": port,
           "sign_on": {"enabled": False},
           "connect_timeout_sec": 5, "response_timeout_sec": 5,
           "reconnect": {"enabled": False}}
    for k in _WIRE_KEYS:
        if rcfg.get(k) is not None:
            cfg.setdefault(k, rcfg[k])
    return cfg


def _match_local_simulator(cfg: dict) -> str | None:
    """The id of a RUNNING simulator this resolved adapter config points at (by
    port), else None — used to tag playground traffic in simulator stats."""
    port = cfg.get("port")
    if not port:
        return None
    for sid, r in SIMULATORS.items():
        if getattr(r, "port", None) == port:
            return sid
    return None


async def _resolve_playground_target(
    t: PlaygroundTarget,
) -> tuple[str, dict, dict]:
    """Resolve a target reference into ``(name, adapter_config, summary)``.

    Resolution happens SERVER-SIDE: a connection's override matrix is applied
    and its (SecretBox-decrypted-at-rest) credentials land in the adapter
    config here — they are used to execute and are never echoed to the client
    (``summary`` carries only addressing facts). Unknown references are a 404.
    """
    kind = (t.kind or "").lower()

    if kind == "connection":
        if not t.id:
            raise HTTPException(400, "target.id (connection name) is required")
        import httpx
        try:
            doc = await _http_get_json(
                f"{SCENARIO_API_URL}/connections/{quote(t.id, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(404, f"no connection '{t.id}'") from exc
            raise HTTPException(502, "scenario-service error resolving "
                                     f"connection '{t.id}'") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"scenario-service unreachable: {exc}")
        if not isinstance(doc, dict):
            raise HTTPException(404, f"no connection '{t.id}'")
        if doc.get("disabled"):
            raise HTTPException(409, f"connection '{t.id}' is disabled "
                                     "(administratively out of rotation)")
        cfg = {k: v for k, v in _connection_effective(doc, t.environment).items()
               if k not in _SKIP_CONN_KEYS}
        summary = {"kind": kind, "id": t.id, "environment": t.environment,
                   "adapter": cfg.get("adapter", "tcp"),
                   "host": cfg.get("host"), "port": cfg.get("port"),
                   "base_url": cfg.get("base_url")}
        return t.id, cfg, summary

    if kind == "group":
        if not t.id:
            raise HTTPException(400, "target.id (group name or id) is required")
        g = (await _load_groups_index()).get(t.id)
        cfg = await _group_adapter_config(g) if g else None
        if cfg is None:
            raise HTTPException(404, f"no participant group '{t.id}' "
                                     "(or no resolvable members)")
        return t.id, cfg, {"kind": kind, "id": t.id,
                           "members": [m["connection"] for m in cfg["members"]]}

    if kind == "simulator":
        r = SIMULATORS.get(t.id or "")
        if r is None:
            raise HTTPException(404, f"no running simulator '{t.id}'")
        cfg = _client_config_for_responder(r)
        return (getattr(r, "_label", t.id), cfg,
                {"kind": kind, "id": t.id, "adapter": cfg.get("adapter"),
                 "host": cfg.get("host"), "port": cfg.get("port"),
                 "subject": cfg.get("subject")})

    if kind == "participant":
        r = PARTICIPANTS.get(t.id or "")
        if r is not None:
            cfg = _client_config_for_responder(r)
            return (getattr(r, "_flow_id", t.id), cfg,
                    {"kind": kind, "id": t.id, "adapter": cfg.get("adapter"),
                     "host": cfg.get("host"), "port": cfg.get("port"),
                     "subject": cfg.get("subject")})
        # fleet-hosted instance (ADR-0001): look it up by planned endpoint.
        for run in TOPOLOGY_RUNS.values():
            if not run.get("fleet"):
                continue
            for inst in flow_fleet.instances_cached(str(run.get("run_id"))):
                if inst.get("instance_id") == t.id and inst.get("port"):
                    cfg = {"adapter": "tcp", "protocol": "iso8583",
                           "host": inst.get("host") or "127.0.0.1",
                           "port": inst["port"],
                           "sign_on": {"enabled": False},
                           "connect_timeout_sec": 5, "response_timeout_sec": 5,
                           "reconnect": {"enabled": False}}
                    return (str(inst.get("flow_id") or t.id), cfg,
                            {"kind": kind, "id": t.id, "adapter": "tcp",
                             "host": cfg["host"], "port": cfg["port"],
                             "fleet": True})
        raise HTTPException(404, f"no running participant '{t.id}'")

    if kind == "raw":
        adapter = (t.adapter or "tcp").lower()
        cfg = dict(t.config or {})
        cfg["adapter"] = adapter
        if t.protocol:
            cfg["protocol"] = t.protocol
        if t.host:
            cfg["host"] = t.host
        if t.port:
            cfg["port"] = t.port
        if adapter in _HOST_PORT_ADAPTERS and not (cfg.get("host") and cfg.get("port")):
            raise HTTPException(400, "raw target needs host and port")
        if adapter == "tcp":
            cfg.setdefault("sign_on", {"enabled": False})
            cfg.setdefault("reconnect", {"enabled": False})
        cfg.setdefault("connect_timeout_sec", 5)
        cfg.setdefault("response_timeout_sec", 5)
        name = f"{cfg.get('host', adapter)}:{cfg.get('port', '')}".rstrip(":")
        return name, cfg, {"kind": kind, "adapter": adapter,
                           "host": cfg.get("host"), "port": cfg.get("port")}

    raise HTTPException(
        400,
        f"unsupported target kind '{t.kind}' — one of: connection, group, "
        "simulator, participant, raw, function. (Code/http nodes stay on "
        "/nodes/execute — the Playground executes adapter actions and "
        "functions, it is not a second node runner.)")


async def _playground_fire(req: PlaygroundExecuteRequest, user: str) -> dict:
    """Resolve + execute one playground interaction and record it."""
    import time as _time
    start = _time.monotonic()
    kind = (req.target.kind or "").lower()

    if kind == "function":
        # local function families — delegated to run_crypto (same owner the
        # crypto node uses); no socket, no resolution.
        from worker.engine.crypto_tools import run_crypto
        res = run_crypto(req.action, req.payload or {})
        ok = "error" not in res
        duration = int((_time.monotonic() - start) * 1000)
        entry = playground_history.add(
            user, target={"kind": "function"}, action=req.action,
            payload=req.payload or {}, message_format_id=None,
            ok=ok, status="passed" if ok else "error",
            request=req.payload or {}, response=None if not ok else res,
            error=res.get("error"), duration_ms=duration, label=req.label)
        return {"ok": ok, "status": entry["status"],
                "target": {"kind": "function"},
                "request": entry["request"], "response": entry["response"],
                "error": entry["error"], "duration_ms": duration,
                "history_seq": entry["seq"]}

    name, cfg, summary = await _resolve_playground_target(req.target)

    payload = dict(req.payload or {})
    if req.message_format_id and not payload.get("fields"):
        # pin the dialect: snapshot the format's DE table into the payload —
        # exactly the per-step ``fields`` override the worker encode honours.
        try:
            fmt = await _http_get_json(
                f"{SCENARIO_API_URL}/formats/{quote(req.message_format_id, safe='')}")
            fields = ((fmt or {}).get("definition") or {}).get("fields")
            if fields:
                payload["fields"] = fields
        except Exception:  # noqa: BLE001 — best-effort, pack with the default
            log.warning("playground: could not resolve message format '%s'",
                        req.message_format_id, exc_info=True)

    env = {"mode": "real", "adapters": {name: cfg}}
    engine = WorkerEngine(env)
    ok, status, error = False, "error", None
    request_echo: Any = payload
    response_echo: Any = None
    try:
        await engine.warmup_adapters()
        sr = await engine._execute_step(name, req.action, payload, [])
        ok = bool(sr.success)
        status = "passed" if ok else "failed"
        error = sr.error
        request_echo = sr.request_payload or payload
        response_echo = sr.response_payload
    except Exception as exc:  # noqa: BLE001 — an unreachable target is a result
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await engine.teardown()

    # provenance tag: playground traffic against a RUNNING simulator inflates
    # its stats — count it so reports can note the contamination.
    sid = req.target.id if kind == "simulator" else _match_local_simulator(cfg)
    if sid and sid in SIMULATORS:
        _record_playground_hit(sid)

    duration = int((_time.monotonic() - start) * 1000)
    entry = playground_history.add(
        user, target=summary, action=req.action, payload=dict(req.payload or {}),
        message_format_id=req.message_format_id, ok=ok, status=status,
        request=request_echo, response=response_echo, error=error,
        duration_ms=duration, label=req.label)
    return {"ok": ok, "status": status, "target": summary,
            "request": entry["request"], "response": entry["response"],
            "error": error, "duration_ms": duration,
            "history_seq": entry["seq"]}


@app.get("/playground/targets")
async def playground_targets() -> dict:
    """Everything addressable from the Playground right now, merged from the
    existing registries (read-only aggregation — nothing here starts anything):
    registered connections (× their override-matrix environments), running
    simulators, network participants (incl. fleet instances and port-less NATS
    subjects), participant groups, and the local function families."""
    try:
        conns = await _http_get_json(f"{SCENARIO_API_URL}/connections")
    except Exception:  # noqa: BLE001 — scenario-service down ≠ no playground
        conns = []
    from .playground_samples import sample_family

    connections = [{
        "name": c.get("name"), "adapter": c.get("adapter"),
        "protocol": c.get("protocol"), "mode": c.get("mode"),
        "host": c.get("host"), "port": c.get("port"),
        "default": bool(c.get("default")), "disabled": bool(c.get("disabled")),
        "environments": sorted((c.get("environment_overrides") or {}).keys()),
        "sample_family": sample_family(c.get("protocol"), c.get("adapter")),
    } for c in conns if isinstance(c, dict)]

    groups = [{
        "id": g.get("id"), "name": g.get("name"),
        "adapter": g.get("adapter") or g.get("type"),
        "members": len(g.get("members") or []),
        "sample_family": sample_family(
            adapter=g.get("adapter") or g.get("type")),
    } for g in {id(v): v for v in (await _load_groups_index()).values()}.values()]

    simulators = [{
        "id": sid, "label": getattr(r, "_label", sid),
        "protocol": r.protocol, "port": getattr(r, "port", None),
        "endpoint": _piece_endpoint(r),
        "proxy": hasattr(r, "capture_rows"),
        "sample_family": sample_family(r.protocol),
    } for sid, r in SIMULATORS.items()]

    participants = [{
        "id": pid, "flow_id": getattr(r, "_flow_id", pid),
        "flow_name": getattr(r, "_flow_name", None),
        "protocol": r.protocol, "endpoint": _piece_endpoint(r),
        "owner": getattr(r, "_topology_run", None) or "standalone",
        "sample_family": sample_family(r.protocol),
    } for pid, r in PARTICIPANTS.items()]
    # fleet-hosted instances (ADR-0001): addressable by planned endpoint.
    for run in TOPOLOGY_RUNS.values():
        if not run.get("fleet"):
            continue
        for inst in flow_fleet.instances_cached(str(run.get("run_id"))):
            if inst.get("port"):
                participants.append({
                    "id": inst.get("instance_id"),
                    "flow_id": inst.get("flow_id"), "flow_name": None,
                    "protocol": "iso8583",
                    "endpoint": f"{inst.get('host')}:{inst.get('port')}",
                    "owner": str(run.get("run_id")), "fleet": True,
                    "sample_family": "iso8583",
                })

    from worker.engine.crypto_tools import OPERATIONS as _CRYPTO_OPS

    from .playground_samples import samples_catalog
    environments = sorted({e for c in connections for e in c["environments"]})
    return {
        "connections": connections,
        "environments": environments,
        "simulators": simulators,
        "participants": participants,
        "groups": groups,
        "functions": {"crypto": sorted(_CRYPTO_OPS)},
        #: raw host/port targets stay allowed (parity with /hsm/command).
        "raw": {"adapters": sorted(_HOST_PORT_ADAPTERS | {"http", "nats"})},
        #: ready-made example interactions per element family: pick
        #: ``samples.wire[target.protocol or target.adapter]`` for wire
        #: targets, ``samples.functions.crypto`` for function targets.
        "samples": samples_catalog(),
    }


@app.post("/playground/execute")
async def playground_execute(req: PlaygroundExecuteRequest,
                             request: Request) -> dict:
    """Execute one ad-hoc interaction *by reference* to a registered target.

    The target is resolved server-side (connection ⊕ override matrix, with
    secrets staying server-side — invariant #8); execution is delegated to the
    worker engine / crypto tools. Echoed request/response payloads are MASKED
    with the proxy-tap redaction rules. Never raises for an unreachable
    target — that is a normal ``{ok: false, error}`` result."""
    return await _playground_fire(req, _playground_user(request))


@app.get("/playground/history")
async def playground_history_rows(request: Request) -> dict:
    """This caller's recent playground interactions (masked), newest first."""
    rows = playground_history.rows(_playground_user(request))
    return {"count": len(rows), "rows": rows}


@app.delete("/playground/history", status_code=204, response_class=Response)
async def playground_history_clear(request: Request) -> Response:
    playground_history.clear(_playground_user(request))
    return Response(status_code=204)


@app.post("/playground/history/{seq}/refire")
async def playground_refire(seq: int, request: Request) -> dict:
    """Re-fire one history entry verbatim (the stored raw request — masking is
    a display concern, the replay uses what was originally sent)."""
    user = _playground_user(request)
    entry = playground_history.get(user, seq)
    if entry is None:
        raise HTTPException(404, f"no playground history entry {seq}")
    t = dict(entry.get("target") or {})
    return await _playground_fire(PlaygroundExecuteRequest(
        target=PlaygroundTarget(
            kind=t.get("kind", "raw"), id=t.get("id"),
            environment=t.get("environment"), host=t.get("host"),
            port=t.get("port"), adapter=t.get("adapter")),
        action=entry["action"], payload=dict(entry.get("_payload") or {}),
        message_format_id=entry.get("message_format_id"),
        label=entry.get("label")), user)


@app.post("/playground/save-as-scenario", status_code=201)
async def playground_save_as_scenario(req: PlaygroundSaveRequest,
                                      request: Request) -> dict:
    """Promote playground interactions into a durable scenario (the proxy
    capture→scenario precedent): the Playground is the on-ramp to authored
    tests, not a parallel world."""
    user = _playground_user(request)
    rows = playground_history.rows(user)
    if req.seqs:
        wanted = set(req.seqs)
        rows = [r for r in rows if r["seq"] in wanted]
    if not rows:
        raise HTTPException(400, "no playground history entries to save")
    draft = build_playground_scenario(rows, req.name)
    created = await _http_post_json(
        f"{SCENARIO_API_URL}/scenarios",
        {"scenario": draft, "comment": "promoted from playground",
         "project_id": req.project_id})
    return {"scenario_id": created.get("id"), "name": draft["name"],
            "steps": len(draft["steps"])}


@app.get("/environments")
async def list_environments() -> list[dict]:
    """Environments the editor's Execute picker can run against.

    Merges the editable registry (scenario-service) with the bundled JSON files,
    registry entries winning by name. ``source`` flags where each came from.
    """
    out: dict[str, dict] = {}
    env_dir = EXAMPLES_DIR / "environments"
    if env_dir.is_dir():
        for path in sorted(env_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out[path.stem] = {
                "name": path.stem,
                "label": doc.get("name", path.stem),
                "description": doc.get("description", ""),
                "source": "bundled",
            }
    try:
        for env in await _http_get_json(f"{SCENARIO_API_URL}/environments"):
            key = env.get("key") or env.get("name")
            if not key:
                continue
            out[key] = {
                "name": key,
                "label": env.get("name", key),
                "description": env.get("description", ""),
                "source": "registry",
            }
    except Exception:  # noqa: BLE001 — registry optional; bundled still listed
        pass
    return [out[k] for k in sorted(out)]


@app.get("/environments/{name}")
async def get_environment(name: str) -> dict:
    """Full resolved environment config (registry first, else bundled file).

    Lets the portal load a bundled environment's config to view/clone/edit even
    before it exists in the editable registry.
    """
    env = await _load_env_by_name(name)
    return {"name": name, **env}


@app.get("/health")
async def health() -> dict:
    """Liveness: the process is up and serving."""
    return {"status": "ok", "backbone": type(backbone).__name__}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition for this replica."""
    return Response(render_metrics(), media_type=METRICS_CT)


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: can this replica actually serve traffic right now?

    Checks the run registry and, when configured, the Redis backbone. Returns
    503 when a dependency is unreachable so a load balancer / k8s can route
    around it.
    """
    checks: dict[str, str] = {}
    ok = True
    try:
        run_store.list()
        checks["run_store"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["run_store"] = f"error: {exc}"
        ok = False
    client = getattr(backbone, "redis", None)
    if client is not None:
        try:
            await client.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"
            ok = False
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@app.get("/status")
async def status() -> dict:
    """Aggregated platform health: this service plus its dependencies.

    A single pane so operators don't have to poll each service's /health by
    hand. Each dependency is probed with a short timeout; failures are reported,
    never raised."""
    services: dict[str, dict] = {}

    async def _probe(name: str, url: str) -> None:
        try:
            detail = await asyncio.wait_for(_http_get_json(url), timeout=3)
            services[name] = {"status": "ok", "detail": detail}
        except Exception as exc:  # noqa: BLE001
            services[name] = {"status": "down", "error": f"{type(exc).__name__}: {exc}"}

    async def _probe_redis() -> None:
        """Ping Redis if configured. The browser can't reach Redis directly, so
        the portal's endpoint monitor reads this server-side result."""
        url = os.environ.get("REDIS_URL")
        if not url:
            services["redis"] = {
                "status": "disabled",
                "detail": "not configured (single-node)",
            }
            return
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(url, decode_responses=True)
            try:
                await asyncio.wait_for(client.ping(), timeout=3)
                services["redis"] = {"status": "ok"}
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            services["redis"] = {"status": "down", "error": f"{type(exc).__name__}: {exc}"}

    async def _probe_mcp() -> None:
        """Reachability of the MCP server's Streamable HTTP transport. Optional:
        only probed when MCP_API_URL is set (the MCP server is often run over
        stdio with no HTTP surface). The /mcp endpoint only answers POST (a bare
        GET/HEAD gets a 405), so we probe the dedicated /healthz liveness route,
        which returns a clean 200."""
        if not MCP_API_URL:
            services["mcp-server"] = {
                "status": "disabled",
                "detail": "not configured (stdio or not deployed)",
            }
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(MCP_API_URL.rstrip("/") + "/healthz")
                code = resp.status_code
                if code < 400:
                    services["mcp-server"] = {"status": "ok", "detail": f"HTTP {code}"}
                elif code == 421:
                    # DNS-rebinding guard rejected our Host header — reachable but
                    # misconfigured. Add this host to the server's MCP_ALLOWED_HOSTS.
                    services["mcp-server"] = {
                        "status": "down",
                        "detail": "host rejected (421) — set MCP_ALLOWED_HOSTS",
                    }
                else:
                    services["mcp-server"] = {"status": "down", "detail": f"HTTP {code}"}
        except Exception as exc:  # noqa: BLE001
            services["mcp-server"] = {
                "status": "down",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _probe_assistant() -> None:
        """Reachability of the config assistant. Optional: only probed when
        ASSIST_API_URL is set (it may not be deployed in every environment)."""
        if not ASSIST_API_URL:
            services["assistant"] = {
                "status": "disabled",
                "detail": "not configured (not deployed)",
            }
            return
        await _probe("assistant", f"{ASSIST_API_URL.rstrip('/')}/health")

    async def _probe_insight() -> None:
        """Reachability of the advisory insight service (ADR-0005). Optional:
        only probed when INSIGHT_API_URL is set."""
        if not INSIGHT_API_URL:
            services["insight-service"] = {
                "status": "disabled",
                "detail": "not configured (not deployed)",
            }
            return
        await _probe("insight-service", f"{INSIGHT_API_URL.rstrip('/')}/health")

    await asyncio.gather(
        _probe("scenario-service", f"{SCENARIO_API_URL}/health"),
        _probe("auth-service", f"{AUTH_API_URL}/health"),
        _probe_redis(),
        _probe_mcp(),
        _probe_assistant(),
        _probe_insight(),
    )
    inflight = sum(1 for r in RUNS.values() if r.task and not r.task.done())
    services["orchestrator"] = {
        "status": "ok",
        "backbone": type(backbone).__name__,
        "runs_inflight": inflight,
        "load_runs_inflight": len(load_coordinator.runs),
    }
    # "disabled" (e.g. Redis in single-node mode) is an intentional posture, not
    # a fault, so it doesn't drag the overall status to degraded.
    overall = (
        "ok"
        if all(s["status"] in ("ok", "disabled") for s in services.values())
        else "degraded"
    )
    return {"status": overall, "services": services}


# -- NATS cluster health + JetStream admin (ADR-0006) -------------------------
# The broker is external infrastructure; these endpoints observe it (per-node
# monitoring) and manage its JetStream streams/consumers. A target cluster is
# either a named registry entry (?server=<name>, from scenario-service) or an
# explicit ?servers=<comma-separated URLs>.

async def _resolve_nats_target(
    server: str | None, servers: str | None
) -> tuple[list[str], list[str] | None, dict]:
    if server:
        try:
            doc = await _http_get_json(f"{SCENARIO_API_URL}/nats-servers/{server}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"nats server '{server}' not found") from exc
        return (list(doc.get("servers") or []),
                list(doc.get("monitoring") or []) or None,
                doc.get("auth") or {})
    if servers:
        urls = [s.strip() for s in servers.split(",") if s.strip()]
        if urls:
            return urls, None, {}
    raise HTTPException(
        400, "provide ?server=<registry name> or ?servers=<comma-separated urls>")


async def _nats_fetch(url: str) -> tuple[int, Any]:
    """Fetch a NATS monitoring endpoint → (status, json-or-None)."""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(url)
        try:
            return resp.status_code, resp.json()
        except Exception:  # noqa: BLE001 — non-JSON (e.g. /healthz text)
            return resp.status_code, None


def _nats_connect_fn():
    """The adapter's lazy nats-py connect (raises ModuleNotFoundError if absent)."""
    from worker.adapters.nats.adapter import _nats_connect

    return _nats_connect


class NatsStreamCreate(BaseModel):
    name: str
    subjects: list[str]
    server: str | None = None
    servers: str | None = None


def _nats_admin_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ModuleNotFoundError):
        return HTTPException(
            500, "NATS admin needs 'nats-py', which isn't installed in this "
                 "orchestrator image — rebuild the orchestrator to manage JetStream.")
    if isinstance(exc, HTTPException):
        return exc
    return HTTPException(502, f"nats jetstream error: {exc}")


@app.get("/nats/cluster")
async def nats_cluster(server: str | None = None, servers: str | None = None) -> dict:
    """Per-node health of a NATS cluster from its monitoring ports (healthz/varz/
    jsz). Read-only — probes HTTP, never opens a client."""
    from . import nats_admin

    s, mon, _auth = await _resolve_nats_target(server, servers)
    return await nats_admin.cluster_health(s, mon, _nats_fetch)


@app.get("/nats/streams")
async def nats_list_streams(server: str | None = None, servers: str | None = None) -> dict:
    from . import nats_admin

    s, _mon, auth = await _resolve_nats_target(server, servers)
    try:
        return await nats_admin.list_streams(s, auth, _nats_connect_fn())
    except Exception as exc:  # noqa: BLE001
        raise _nats_admin_error(exc)


@app.post("/nats/streams", status_code=201)
async def nats_create_stream(body: NatsStreamCreate) -> dict:
    """Create a JetStream stream on the target cluster (operator action —
    distinct from a run's stop-ownership)."""
    from . import nats_admin

    s, _mon, auth = await _resolve_nats_target(body.server, body.servers)
    try:
        return await nats_admin.create_stream(
            s, auth, _nats_connect_fn(), name=body.name, subjects=body.subjects)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _nats_admin_error(exc)


@app.delete("/nats/streams/{name}")
async def nats_delete_stream(
    name: str, server: str | None = None, servers: str | None = None
) -> dict:
    from . import nats_admin

    s, _mon, auth = await _resolve_nats_target(server, servers)
    try:
        return await nats_admin.delete_stream(s, auth, _nats_connect_fn(), name=name)
    except Exception as exc:  # noqa: BLE001
        raise _nats_admin_error(exc)


@app.get("/nats/streams/{name}/consumers")
async def nats_list_consumers(
    name: str, server: str | None = None, servers: str | None = None
) -> dict:
    from . import nats_admin

    s, _mon, auth = await _resolve_nats_target(server, servers)
    try:
        return await nats_admin.list_consumers(s, auth, _nats_connect_fn(), stream=name)
    except Exception as exc:  # noqa: BLE001
        raise _nats_admin_error(exc)


@app.delete("/nats/streams/{name}/consumers/{durable}")
async def nats_delete_consumer(
    name: str, durable: str, server: str | None = None, servers: str | None = None
) -> dict:
    from . import nats_admin

    s, _mon, auth = await _resolve_nats_target(server, servers)
    try:
        return await nats_admin.delete_consumer(
            s, auth, _nats_connect_fn(), stream=name, durable=durable)
    except Exception as exc:  # noqa: BLE001
        raise _nats_admin_error(exc)


@app.get("/diagnostics")
async def diagnostics(
    layers: str | None = None,
    connection: str | None = None,
    environment: str | None = None,
) -> dict:
    """Platform doctor: one layered pass over every hop that can break.

    Runs services → connections (config/DNS/live-probe, with environment
    overrides applied) → listeners (bound-port verification + localhost
    cross-check) → recent-run error correlation, and returns a check tree
    with a concrete fix hint per failure. Scope with ``?layers=services,
    connections``, ``?connection=<name>`` or ``?environment=<env>``.

    Complements (doesn't replace) the narrower views: ``/status`` for a quick
    service pulse, ``/connections/test`` for one link, ``/runs/{id}/diagnose``
    for one run.
    """
    from .diagnostics import DiagContext, run_diagnostics

    def _listeners() -> list[dict]:
        out = [
            {"id": sid, "label": getattr(r, "_label", sid),
             "port": r.port, "protocol": r.protocol, "source": "simulator"}
            for sid, r in SIMULATORS.items()
        ]
        out += [
            {"id": pid, "label": getattr(r, "_flow_id", pid),
             "port": r.port, "protocol": r.protocol, "source": "participant"}
            for pid, r in PARTICIPANTS.items()
        ]
        return out

    async def _probe(cfg: dict) -> dict:
        return await test_connection(TestConnectionRequest(config=cfg))

    async def _nats_health(servers: list[str], monitoring: list[str] | None) -> dict:
        from . import nats_admin

        return await nats_admin.cluster_health(servers, monitoring, _nats_fetch)

    ctx = DiagContext(
        http_get_json=_http_get_json,
        scenario_api_url=SCENARIO_API_URL,
        auth_api_url=AUTH_API_URL,
        redis_url=os.environ.get("REDIS_URL"),
        mcp_api_url=MCP_API_URL,
        assist_api_url=ASSIST_API_URL,
        run_store=run_store,
        listeners=_listeners,
        probe_connection=_probe,
        connection_effective=_connection_effective,
        nats_health=_nats_health,
        capture_state=lambda: {
            "capturing": _CAPTURE_ENABLED,
            "buffered": sum(
                len(getattr(r, "traces", lambda: [])())
                for r in PARTICIPANTS.values()
            ),
        },
    )
    return await run_diagnostics(
        ctx,
        layers=[s.strip() for s in layers.split(",")] if layers else None,
        connection=connection,
        environment=environment,
    )


@app.get("/system")
async def system() -> dict:
    """Non-secret runtime posture, for the portal's System settings panel.

    Surfaces the operational config that's otherwise only knowable from env vars
    and the shell — security boundaries (auth gate, code sandbox), durability,
    and observability — so an admin can confirm the deployment is configured the
    way they think it is. Intentionally exposes no secrets, only on/off state.
    """
    from worker.engine.code_runner import network_isolation_active, _sandbox_mode

    redis_url = os.environ.get("REDIS_URL")
    allow_unauth_code = os.environ.get("PAYPROBE_ALLOW_UNAUTH_CODE", "").lower() in (
        "1", "true", "yes",
    )
    return {
        "environment": os.environ.get("PAYPROBE_ENV", "") or "unset",
        "version": app.version,
        "security": {
            "auth_enforced": auth_enforced(),
            "code_sandbox_mode": _sandbox_mode(),
            "code_network_isolation_active": network_isolation_active(),
            "unauth_code_execution_allowed": allow_unauth_code,
        },
        "runtime": {
            "event_backbone": type(backbone).__name__,
            "redis_configured": bool(redis_url),
            "run_store_durable": getattr(run_store, "durable", False),
            "run_control": type(run_control).__name__,
            "tracing_configured": bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
        },
        "observability": {
            "metrics": "/metrics",
            "ready": "/ready",
            "status": "/status",
            "api_reference": "/reference",
        },
    }
