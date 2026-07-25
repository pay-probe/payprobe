"""Agent tools — the standalone (REST) deployment of the shared toolkit.

The registry, guardrails, journal and every handler live ONCE in
``payprobe_common.agent_toolkit`` (shared with scenario-service's in-process
``/agent/chat``). This module supplies the **REST backend** — the primitive
resource operations over the PayProbe services' HTTP APIs
(:mod:`assistant_service.rest`) — plus thin compatibility wrappers so the
loop/session/main modules keep their existing imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

from . import rest

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


def _get_or_none(url: str) -> Any:
    """GET that returns None on a 404 (used to capture prior state)."""
    try:
        return rest.request("GET", url)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


class RestBackend:
    """The shared toolkit's primitive ops over the PayProbe REST APIs."""

    # -- connections -----------------------------------------------------------
    def list_connections(self) -> list[dict]:
        return rest.request("GET", rest.s("/connections"))

    def get_connection(self, name: str) -> dict | None:
        return _get_or_none(rest.s(f"/connections/{name}"))

    def put_connection(self, name: str, config: dict) -> dict:
        return rest.request("PUT", rest.s(f"/connections/{name}"), config or {})

    def delete_connection(self, name: str) -> None:
        rest.request("DELETE", rest.s(f"/connections/{name}"))

    # -- environments ------------------------------------------------------------
    def list_environments(self) -> list[dict]:
        return rest.request("GET", rest.s("/environments"))

    def get_environment(self, name: str) -> dict | None:
        return _get_or_none(rest.s(f"/environments/{name}"))

    # -- catalog / formats ---------------------------------------------------------
    def list_catalog(self) -> list[dict]:
        return rest.request("GET", rest.s("/catalog"))

    def list_formats(self, protocol: str | None = None) -> list[dict]:
        url = rest.s("/formats" + (f"?protocol={protocol}" if protocol else ""))
        return rest.request("GET", url)

    def get_format(self, fid: str) -> dict | None:
        return _get_or_none(rest.s(f"/formats/{fid}"))

    # -- tables ---------------------------------------------------------------------
    def list_tables(self) -> list[dict]:
        return rest.request("GET", rest.s("/tables"))

    def get_table(self, name: str) -> dict | None:
        return _get_or_none(rest.s(f"/tables/{name}"))

    def put_table(self, name: str, draft: dict) -> dict:
        return rest.request("PUT", rest.s(f"/tables/{name}"), draft or {})

    def delete_table(self, name: str) -> None:
        rest.request("DELETE", rest.s(f"/tables/{name}"))

    # -- global variables --------------------------------------------------------------
    def get_global_variables(self) -> dict:
        return rest.request("GET", rest.s("/variables/global"))

    def set_global_variables(self, variables: dict, secrets: list) -> dict:
        return rest.request("PUT", rest.s("/variables/global"),
                            {"variables": variables or {}, "secrets": secrets or []})

    # -- starter flows -------------------------------------------------------------------
    def list_starter_flows(self) -> list[dict]:
        return rest.request("GET", rest.s("/starter-flows"))

    def get_starter_flow(self, fid: str) -> dict | None:
        return _get_or_none(rest.s(f"/starter-flows/{fid}"))

    def create_starter_flow(self, draft: dict, fid: str | None = None) -> dict:
        # NOTE: the REST surface mints ids server-side; ``fid`` (used when
        # restoring a deleted flow) can't be forced — the flow returns under a
        # fresh id, which is the pre-unification behaviour too.
        return rest.request("POST", rest.s("/starter-flows"), draft or {})

    def delete_starter_flow(self, fid: str) -> None:
        try:
            rest.request("DELETE", rest.s(f"/starter-flows/{fid}"))
        except RuntimeError as exc:
            if "HTTP 400" in str(exc):            # builtin flows are refused
                raise GuardrailError(f"cannot delete starter flow '{fid}' (builtin)")
            raise

    # -- scenarios --------------------------------------------------------------------------
    def list_scenarios(self, project_id: str | None = None) -> list[dict]:
        url = rest.s("/scenarios" + (f"?project_id={project_id}" if project_id else ""))
        return rest.request("GET", url)

    def get_scenario(self, sid: str) -> dict | None:
        return _get_or_none(rest.s(f"/scenarios/{sid}"))

    def create_scenario(self, draft: dict, project_id: str | None, comment: str) -> dict:
        return rest.request("POST", rest.s("/scenarios"), {
            "scenario": draft, "project_id": project_id, "comment": comment})

    def update_scenario(self, sid: str, draft: dict, comment: str) -> dict:
        return rest.request("PUT", rest.s(f"/scenarios/{sid}"), {
            "scenario": draft, "comment": comment})

    def delete_scenario(self, sid: str) -> None:
        rest.request("DELETE", rest.s(f"/scenarios/{sid}"))

    def validate_scenario(self, draft: dict) -> dict:
        return rest.request("POST", rest.s("/validate"), draft or {})

    # -- networks (network flows) -----------------------------------------------------------
    def list_networks(self) -> list[dict]:
        return rest.request("GET", rest.s("/network-flows"))

    def get_network(self, nid: str) -> dict | None:
        return _get_or_none(rest.s(f"/network-flows/{nid}"))

    def put_network(self, nid: str, draft: dict) -> dict:
        return rest.request("PUT", rest.s(f"/network-flows/{nid}"), draft or {})

    def delete_network(self, nid: str) -> None:
        rest.request("DELETE", rest.s(f"/network-flows/{nid}"))

    def validate_network(self, draft: dict) -> dict:
        return rest.request("POST", rest.s("/network-flows/validate"), draft or {})

    def plan_network(self, nid: str) -> dict | None:
        return _get_or_none(rest.s(f"/network-flows/{nid}/plan"))

    # -- runtime visibility (orchestrator, read-only) -----------------------------
    def platform_status(self) -> dict:
        return rest.request("GET", rest.r("/status"))

    def list_runs(self) -> list[dict]:
        return rest.request("GET", rest.r("/runs"))

    def list_network_runs(self) -> list[dict]:
        return rest.request("GET", rest.r("/topology-runs"))

    def list_running_participants(self) -> list[dict]:
        return rest.request("GET", rest.r("/participants"))

    def list_running_simulators(self) -> list[dict]:
        return rest.request("GET", rest.r("/simulators"))

    def list_load_runs(self) -> list[dict]:
        return rest.request("GET", rest.r("/load-runs"))

    def get_load_run(self, run_id: str) -> dict | None:
        import urllib.parse
        return _get_or_none(
            rest.r(f"/load-runs/{urllib.parse.quote(run_id, safe='')}"))

    def start_load_run(self, spec: dict) -> dict:
        return rest.request("POST", rest.r("/load-runs"), spec)

    # -- model insights (insight-service, ADR-0005; read-only + advisory) ------

    def get_run_insights(self, run_id: str) -> dict | None:
        import urllib.parse
        return _get_or_none(
            rest.i(f"/insights/failures/{urllib.parse.quote(run_id, safe='')}"))

    def list_insight_predictions(self, environment: str | None = None) -> dict:
        import urllib.parse
        q = (f"?environment={urllib.parse.quote(environment)}"
             if environment else "")
        return rest.request("GET", rest.i(f"/insights/predictions{q}"))

    # -- playground (orchestrator, ADR-0007: ad-hoc execution by reference) ----

    def playground_targets(self) -> dict:
        return rest.request("GET", rest.r("/playground/targets"))

    def playground_execute(self, target: dict, action: str, payload: dict,
                           message_format_id: str | None = None,
                           label: str | None = None) -> dict:
        return rest.request("POST", rest.r("/playground/execute"), {
            "target": target or {}, "action": action, "payload": payload or {},
            "message_format_id": message_format_id, "label": label})


@dataclass
class ToolContext:
    """Backwards-compatible context: no-arg construction, REST backend."""

    journal: ChangeJournal = field(default_factory=ChangeJournal)
    referenced_connections: set[str] = field(default_factory=set)
    backend: Any = field(default_factory=RestBackend)


def restore_one(rec: dict) -> None:
    """Pre-unification signature (record only) — kept for callers/tests."""
    _tk.restore_one(ToolContext(), rec)


def restore_journal(records: list[dict]) -> int:
    """Pre-unification signature (records only) — kept for callers/tests."""
    return _tk.restore_journal(ToolContext(), records)


def referenced_connection_names() -> set[str]:
    """Connection names a scenario currently references (best-effort), for the
    delete guardrail. Scans each scenario's serialized JSON for quoted names."""
    import json

    try:
        conns = {c.get("name") for c in rest.request("GET", rest.s("/connections"))}
        conns.discard(None)
        if not conns:
            return set()
        referenced: set[str] = set()
        for row in rest.request("GET", rest.s("/scenarios")):
            sid = row.get("id")
            if not sid:
                continue
            doc = _get_or_none(rest.s(f"/scenarios/{sid}"))
            if not doc:
                continue
            blob = json.dumps(doc)
            for name in conns:
                if f'"{name}"' in blob:
                    referenced.add(name)
        return referenced
    except RuntimeError:
        return set()
