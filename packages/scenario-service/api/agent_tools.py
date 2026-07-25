"""Agent tools — the in-process (stores) deployment of the shared toolkit.

The registry, guardrails, journal and every handler live ONCE in
``payprobe_common.agent_toolkit`` (shared with the standalone
``payprobe-assistant`` service, which plugs in a REST backend). This module
supplies the **stores backend** — the primitive resource operations over
``app.state`` — plus a backwards-compatible :class:`ToolContext` so existing
callers (``api.agent``, ``api.main``, the tests) keep constructing it from
``stores``/``run_async``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

try:  # packages/ on the path (dev: PYTHONPATH=packages; container: /app)
    from payprobe_common import agent_toolkit as _tk
except ModuleNotFoundError:  # pragma: no cover — running from the package dir
    import sys
    from pathlib import Path

    _here = Path(__file__).resolve()
    for _cand in (_here.parents[1], _here.parents[2]):
        if (_cand / "payprobe_common").is_dir():
            sys.path.insert(0, str(_cand))
            break
    from payprobe_common import agent_toolkit as _tk

from .connection_store import ConnectionDraft
from .table_store import DataTableDraft

# Re-exports: the public surface predating the extraction, unchanged.
GuardrailError = _tk.GuardrailError
ToolError = _tk.ToolError
ChangeRecord = _tk.ChangeRecord
ChangeJournal = _tk.ChangeJournal
ToolSpec = _tk.ToolSpec
REGISTRY = _tk.REGISTRY
tools_for = _tk.tools_for
schemas_for = _tk.schemas_for
dispatch = _tk.dispatch
restore_one = _tk.restore_one
restore_journal = _tk.restore_journal


def _dump(model: Any) -> dict | None:
    return model.model_dump() if model is not None else None


class StoresBackend:
    """The shared toolkit's primitive ops over the scenario-service stores.

    ``stores`` is anything exposing ``.connections .catalog .flows .formats
    .tables .variables .environments .network_flows .participant_flows
    .participant_groups .store`` (FastAPI ``app.state`` satisfies this).

    ``run_async`` bridges the (sync) handlers to the *async* scenario store:
    the endpoint runs the agent loop in a threadpool and passes a callable that
    schedules a coroutine on the main event loop and blocks for its result.
    Scenario tools raise if it's absent."""

    def __init__(self, stores: Any, run_async: Callable[[Any], Any] | None = None) -> None:
        self.stores = stores
        self.run_async = run_async

    def _await(self, coro: Any) -> Any:
        if self.run_async is None:
            raise ToolError("scenario operations are unavailable in this context")
        return self.run_async(coro)

    # -- connections -----------------------------------------------------------
    def list_connections(self) -> list[dict]:
        return [_dump(c) for c in self.stores.connections.list()]

    def get_connection(self, name: str) -> dict | None:
        return _dump(self.stores.connections.get(name))

    def put_connection(self, name: str, config: dict) -> dict:
        try:
            return _dump(self.stores.connections.upsert(name, ConnectionDraft(**config)))
        except Exception as exc:  # pydantic validation, etc.
            raise ToolError(f"invalid connection config: {exc}")

    def delete_connection(self, name: str) -> None:
        self.stores.connections.delete(name)

    # -- environments ------------------------------------------------------------
    def list_environments(self) -> list[dict]:
        return [_dump(e) for e in self.stores.environments.list()]

    def get_environment(self, name: str) -> dict | None:
        return _dump(self.stores.environments.get(name))

    # -- catalog / formats ---------------------------------------------------------
    def list_catalog(self) -> list[dict]:
        return [_dump(t) for t in self.stores.catalog.merged()]

    def list_formats(self, protocol: str | None = None) -> list[dict]:
        return [_dump(f) for f in self.stores.formats.list(protocol)]

    def get_format(self, fid: str) -> dict | None:
        return _dump(self.stores.formats.get(fid))

    # -- tables ---------------------------------------------------------------------
    def list_tables(self) -> list[dict]:
        return [_dump(t) for t in self.stores.tables.list()]

    def get_table(self, name: str) -> dict | None:
        return _dump(self.stores.tables.get(name))

    def put_table(self, name: str, draft: dict) -> dict:
        try:
            return _dump(self.stores.tables.upsert(name, DataTableDraft(**draft)))
        except Exception as exc:
            raise ToolError(f"invalid table draft: {exc}")

    def delete_table(self, name: str) -> None:
        self.stores.tables.delete(name)

    # -- global variables --------------------------------------------------------------
    def get_global_variables(self) -> dict:
        vs = self.stores.variables
        return {"variables": dict(vs.get_global()),
                "secrets": list(vs.get_global_secrets())}

    def set_global_variables(self, variables: dict, secrets: list) -> dict:
        vs = self.stores.variables
        vs.set_global(variables or {}, secrets or [])
        return {"variables": vs.get_global(), "secrets": vs.get_global_secrets()}

    # -- starter flows -------------------------------------------------------------------
    def list_starter_flows(self) -> list[dict]:
        return [_dump(f) for f in self.stores.flows.list()]

    def get_starter_flow(self, fid: str) -> dict | None:
        return _dump(self.stores.flows.get(fid))

    def create_starter_flow(self, draft: dict, fid: str | None = None) -> dict:
        from models.starter_flow import StarterFlowDraft
        try:
            return _dump(self.stores.flows.create(StarterFlowDraft(**(draft or {})), fid=fid))
        except Exception as exc:
            raise ToolError(f"invalid starter-flow draft: {exc}")

    def delete_starter_flow(self, fid: str) -> None:
        try:
            self.stores.flows.delete(fid)
        except ValueError as exc:           # builtin flows raise ValueError
            raise GuardrailError(f"cannot delete starter flow '{fid}': {exc}")

    # -- scenarios (async store, bridged via run_async) -------------------------------------
    def list_scenarios(self, project_id: str | None = None) -> list[dict]:
        return [_dump(s) for s in self._await(self.stores.store.list(project_id))]

    def get_scenario(self, sid: str) -> dict | None:
        from .store import ScenarioNotFound
        try:
            return _dump(self._await(self.stores.store.get(sid)))
        except ScenarioNotFound:
            return None

    def create_scenario(self, draft: dict, project_id: str | None, comment: str) -> dict:
        from .store import ProjectNotFound
        try:
            created = self._await(self.stores.store.create(
                self._draft(draft), project_id=project_id, comment=comment))
        except ProjectNotFound as exc:
            raise ToolError(f"project '{exc.args[0]}' not found")
        return _dump(created)

    def update_scenario(self, sid: str, draft: dict, comment: str) -> dict:
        return _dump(self._await(self.stores.store.update(
            sid, self._draft(draft), comment=comment)))

    def delete_scenario(self, sid: str) -> None:
        self._await(self.stores.store.delete(sid))

    def validate_scenario(self, draft: dict) -> dict:
        from models import validate_scenario
        return validate_scenario(self._draft(draft)).model_dump()

    @staticmethod
    def _draft(draft: dict):
        from models import ScenarioDraft
        try:
            return ScenarioDraft(**(draft or {}))
        except Exception as exc:
            raise ToolError(f"invalid scenario document: {exc}")

    # -- networks (network flows) -----------------------------------------------------------
    def list_networks(self) -> list[dict]:
        return [_dump(n) for n in self.stores.network_flows.list()]

    def get_network(self, nid: str) -> dict | None:
        return _dump(self.stores.network_flows.get(nid))

    def put_network(self, nid: str, draft: dict) -> dict:
        return _dump(self.stores.network_flows.upsert(nid, self._network_draft(draft)))

    def delete_network(self, nid: str) -> None:
        self.stores.network_flows.delete(nid)

    def validate_network(self, draft: dict) -> dict:
        """Shape validation + referential checks against the local registries
        (mirrors the ``/network-flows/validate`` endpoint)."""
        from models.network_flow import validate_network_flow
        parsed = self._network_draft(draft)
        result = validate_network_flow(parsed).model_dump()
        issues = list(result.get("issues", []))
        for node in parsed.nodes:
            cfg = node.config or {}
            if node.kind == "participant":
                fid = str(cfg.get("flow_id", "")).strip()
                if fid and self.stores.participant_flows.get(fid) is None:
                    issues.append({"severity": "error", "step_id": node.id,
                                   "message": f"participant flow '{fid}' does not exist"})
            elif node.kind == "group":
                gid = str(cfg.get("group_id", "")).strip()
                if gid and self.stores.participant_groups.get(gid) is None:
                    issues.append({"severity": "error", "step_id": node.id,
                                   "message": f"participant group '{gid}' does not exist"})
        return {"valid": not any(i.get("severity") == "error" for i in issues),
                "issues": issues}

    def plan_network(self, nid: str) -> dict | None:
        from models.network_flow import plan_network_flow
        nf = self.stores.network_flows.get(nid)
        return plan_network_flow(nf).model_dump() if nf is not None else None

    @staticmethod
    def _network_draft(draft: dict):
        from models.network_flow import NetworkFlowDraft
        try:
            return NetworkFlowDraft(**(draft or {}))
        except Exception as exc:
            raise ToolError(f"invalid network document: {exc}")

    # -- runtime visibility (orchestrator over HTTP, read-only) --------------------
    # The in-process assistant lives in the scenario-service, but runtime state
    # (runs, listeners, simulators) lives in the orchestrator — reached over
    # HTTP with the same service credential scheme the MCP server uses.

    def platform_status(self) -> dict:
        return _run_api_get("/status")

    def list_runs(self) -> list[dict]:
        return _run_api_get("/runs")

    def list_network_runs(self) -> list[dict]:
        return _run_api_get("/topology-runs")

    def list_running_participants(self) -> list[dict]:
        return _run_api_get("/participants")

    def list_running_simulators(self) -> list[dict]:
        return _run_api_get("/simulators")

    def list_load_runs(self) -> list[dict]:
        return _run_api_get("/load-runs")

    def get_load_run(self, run_id: str) -> dict | None:
        import urllib.parse
        return _run_api_get(
            f"/load-runs/{urllib.parse.quote(run_id, safe='')}")

    def start_load_run(self, spec: dict) -> dict:
        return _run_api_post("/load-runs", spec)

    # -- model insights (insight-service, ADR-0005; read-only + advisory) ------

    def get_run_insights(self, run_id: str) -> dict | None:
        import urllib.parse
        return _insight_api_get(
            f"/insights/failures/{urllib.parse.quote(run_id, safe='')}")

    def list_insight_predictions(self, environment: str | None = None) -> dict:
        import urllib.parse
        q = (f"?environment={urllib.parse.quote(environment)}"
             if environment else "")
        return _insight_api_get(f"/insights/predictions{q}") or {"predictions": []}

    # -- playground (orchestrator, ADR-0007: ad-hoc execution by reference) ----

    def playground_targets(self) -> dict:
        return _run_api_get("/playground/targets")

    def playground_execute(self, target: dict, action: str, payload: dict,
                           message_format_id: str | None = None,
                           label: str | None = None) -> dict:
        return _run_api_post("/playground/execute", {
            "target": target or {}, "action": action, "payload": payload or {},
            "message_format_id": message_format_id, "label": label})


def _insight_api_get(path: str) -> Any:
    """Authenticated GET against the insight service (INSIGHT_API_URL, default
    localhost:8500). Same credential scheme as :func:`_run_api_get`; a 404 maps
    to ``None`` (per the Backend get_* convention). The insight service is an
    OPTIONAL deployment, so unreachable raises a ToolError that says so."""
    import json as _json
    import os
    import time
    import urllib.error
    import urllib.request

    base = os.environ.get("INSIGHT_API_URL", "http://localhost:8500").rstrip("/")
    headers = {}
    token = os.environ.get("API_TOKEN")
    secret = os.environ.get("AUTH_JWT_SECRET")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif secret:
        try:
            import jwt
            now = int(time.time())
            claims: dict[str, Any] = {"sub": "scenario-service-assistant",
                                      "iat": now, "exp": now + 300}
            if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
                claims["aud"] = aud
            if iss := os.environ.get("AUTH_JWT_ISSUER"):
                claims["iss"] = iss
            headers["Authorization"] = f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"
        except ImportError:
            pass
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ToolError(f"insight service error: HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ToolError(
            f"insight service unreachable at {base} ({exc}) — it is an "
            "optional deployment; start it to get failure categorization "
            "and outcome predictions")


def _run_api_request(path: str, body: dict | None = None,
                     timeout: float = 10) -> Any:
    """Authenticated request against the orchestrator (RUN_API_URL, default
    localhost:8100) — GET, or POST when ``body`` is given. Static bearer
    (``API_TOKEN``) wins; else a short-lived JWT minted from the shared
    ``AUTH_JWT_SECRET``; else no header (open dev gate). Raises
    :class:`ToolError` with a clear message when unreachable."""
    import json as _json
    import os
    import time
    import urllib.error
    import urllib.request

    base = os.environ.get("RUN_API_URL", "http://localhost:8100").rstrip("/")
    headers = {}
    token = os.environ.get("API_TOKEN")
    secret = os.environ.get("AUTH_JWT_SECRET")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif secret:
        try:
            import jwt
            now = int(time.time())
            claims: dict[str, Any] = {"sub": "scenario-service-assistant",
                                      "iat": now, "exp": now + 300}
            if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
                claims["aud"] = aud
            if iss := os.environ.get("AUTH_JWT_ISSUER"):
                claims["iss"] = iss
            headers["Authorization"] = f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"
        except ImportError:
            pass
    data = None
    if body is not None:
        data = _json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ToolError(f"orchestrator HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ToolError(
            f"orchestrator unreachable at {base} ({exc}) — runtime state "
            "(runs/listeners/simulators) needs the orchestrator up")


def _run_api_get(path: str) -> Any:
    return _run_api_request(path)


def _run_api_post(path: str, body: dict) -> Any:
    # ad-hoc executes wait on real sockets — give them a little longer.
    return _run_api_request(path, body=body, timeout=30)


@dataclass
class ToolContext:
    """Backwards-compatible context: constructed from stores, exposes the
    shared toolkit's ``backend``/``journal``/``referenced_connections``."""

    stores: Any
    journal: ChangeJournal = field(default_factory=ChangeJournal)
    referenced_connections: set[str] = field(default_factory=set)
    run_async: Callable[[Any], Any] | None = None
    backend: Any = None

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = StoresBackend(self.stores, self.run_async)

    def await_(self, coro: Any) -> Any:
        if self.run_async is None:
            raise ToolError("scenario operations are unavailable in this context")
        return self.run_async(coro)
