"""REST backend for the shared toolkit — ONCE, for every out-of-process caller.

The toolkit (:mod:`payprobe_common.agent_toolkit`) needs a :class:`Backend`
that supplies ~30 primitive resource operations. In-process callers plug in a
stores backend; every out-of-process caller (the standalone assistant, and
agent-hub's heartbeat runner from ADR-0010 phase 2) talks to the PayProbe
services over HTTP. Historically the assistant carried that REST backend
privately; a second service needing it is exactly the "per-service copies"
drift CLAUDE.md invariant #3 records, so it lives here now and both import it.

Transport is injected: ``request(method, url, body=None, raw=False,
timeout=30)`` returns parsed JSON (or ``None`` for empty bodies) and raises
``RuntimeError("HTTP <code> <url>: <detail>")`` on failure. Base URLs are
injected too, so the caller decides where scenario-service, the orchestrator
and insight-service live (env names differ per deployment).
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

from .agent_toolkit import GuardrailError

Request = Callable[..., Any]


class RestBackend:
    """The shared toolkit's primitive ops over the PayProbe REST APIs."""

    def __init__(self, request: Request, scenario_api: str, run_api: str,
                 insight_api: str = "") -> None:
        self._request = request
        self._s = scenario_api.rstrip("/")
        self._r = run_api.rstrip("/")
        self._i = insight_api.rstrip("/")

    # -- url helpers -------------------------------------------------------------
    def s(self, path: str) -> str:
        return f"{self._s}{path}"

    def r(self, path: str) -> str:
        return f"{self._r}{path}"

    def i(self, path: str) -> str:
        return f"{self._i}{path}"

    @staticmethod
    def seg(value: Any) -> str:
        """One percent-encoded path segment. Ids come from a model: a run label
        with spaces passed as a scenario id must become a clean 404 from the
        service, not an ``InvalidURL`` escaping the tool layer."""
        return urllib.parse.quote(str(value), safe="")

    def request(self, method: str, url: str, body: dict | None = None, **kw: Any) -> Any:
        return self._request(method, url, body, **kw)

    def _get_or_none(self, url: str) -> Any:
        """GET that returns None on a 404 (used to capture prior state)."""
        try:
            return self._request("GET", url)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    # -- connections -------------------------------------------------------------
    def list_connections(self) -> list[dict]:
        return self.request("GET", self.s("/connections"))

    def get_connection(self, name: str) -> dict | None:
        return self._get_or_none(self.s(f"/connections/{self.seg(name)}"))

    def put_connection(self, name: str, config: dict) -> dict:
        return self.request("PUT", self.s(f"/connections/{self.seg(name)}"), config or {})

    def delete_connection(self, name: str) -> None:
        self.request("DELETE", self.s(f"/connections/{self.seg(name)}"))

    # -- environments ------------------------------------------------------------
    def list_environments(self) -> list[dict]:
        return self.request("GET", self.s("/environments"))

    def get_environment(self, name: str) -> dict | None:
        return self._get_or_none(self.s(f"/environments/{self.seg(name)}"))

    # -- catalog / formats -------------------------------------------------------
    def list_catalog(self) -> list[dict]:
        return self.request("GET", self.s("/catalog"))

    def list_formats(self, protocol: str | None = None) -> list[dict]:
        url = self.s("/formats" + (f"?protocol={self.seg(protocol)}" if protocol else ""))
        return self.request("GET", url)

    def get_format(self, fid: str) -> dict | None:
        return self._get_or_none(self.s(f"/formats/{self.seg(fid)}"))

    # -- tables ------------------------------------------------------------------
    def list_tables(self) -> list[dict]:
        return self.request("GET", self.s("/tables"))

    def get_table(self, name: str) -> dict | None:
        return self._get_or_none(self.s(f"/tables/{self.seg(name)}"))

    def put_table(self, name: str, draft: dict) -> dict:
        return self.request("PUT", self.s(f"/tables/{self.seg(name)}"), draft or {})

    def delete_table(self, name: str) -> None:
        self.request("DELETE", self.s(f"/tables/{self.seg(name)}"))

    # -- global variables --------------------------------------------------------
    def get_global_variables(self) -> dict:
        return self.request("GET", self.s("/variables/global"))

    def set_global_variables(self, variables: dict, secrets: list) -> dict:
        return self.request("PUT", self.s("/variables/global"),
                            {"variables": variables or {}, "secrets": secrets or []})

    # -- starter flows -----------------------------------------------------------
    def list_starter_flows(self) -> list[dict]:
        return self.request("GET", self.s("/starter-flows"))

    def get_starter_flow(self, fid: str) -> dict | None:
        return self._get_or_none(self.s(f"/starter-flows/{self.seg(fid)}"))

    def create_starter_flow(self, draft: dict, fid: str | None = None) -> dict:
        # NOTE: the REST surface mints ids server-side; ``fid`` (used when
        # restoring a deleted flow) can't be forced — the flow returns under a
        # fresh id, which is the pre-unification behaviour too.
        return self.request("POST", self.s("/starter-flows"), draft or {})

    def delete_starter_flow(self, fid: str) -> None:
        try:
            self.request("DELETE", self.s(f"/starter-flows/{self.seg(fid)}"))
        except RuntimeError as exc:
            if "HTTP 400" in str(exc):            # builtin flows are refused
                raise GuardrailError(f"cannot delete starter flow '{fid}' (builtin)")
            raise

    # -- scenarios ---------------------------------------------------------------
    def list_scenarios(self, project_id: str | None = None) -> list[dict]:
        url = self.s("/scenarios" + (f"?project_id={self.seg(project_id)}" if project_id else ""))
        return self.request("GET", url)

    def get_scenario(self, sid: str) -> dict | None:
        return self._get_or_none(self.s(f"/scenarios/{self.seg(sid)}"))

    def create_scenario(self, draft: dict, project_id: str | None, comment: str) -> dict:
        return self.request("POST", self.s("/scenarios"), {
            "scenario": draft, "project_id": project_id, "comment": comment})

    def update_scenario(self, sid: str, draft: dict, comment: str) -> dict:
        return self.request("PUT", self.s(f"/scenarios/{self.seg(sid)}"), {
            "scenario": draft, "comment": comment})

    def delete_scenario(self, sid: str) -> None:
        self.request("DELETE", self.s(f"/scenarios/{self.seg(sid)}"))

    def validate_scenario(self, draft: dict) -> dict:
        return self.request("POST", self.s("/validate"), draft or {})

    # -- networks (network flows) ------------------------------------------------
    def list_networks(self) -> list[dict]:
        return self.request("GET", self.s("/network-flows"))

    def get_network(self, nid: str) -> dict | None:
        return self._get_or_none(self.s(f"/network-flows/{self.seg(nid)}"))

    def put_network(self, nid: str, draft: dict) -> dict:
        return self.request("PUT", self.s(f"/network-flows/{self.seg(nid)}"), draft or {})

    def delete_network(self, nid: str) -> None:
        self.request("DELETE", self.s(f"/network-flows/{self.seg(nid)}"))

    def validate_network(self, draft: dict) -> dict:
        return self.request("POST", self.s("/network-flows/validate"), draft or {})

    def plan_network(self, nid: str) -> dict | None:
        return self._get_or_none(self.s(f"/network-flows/{self.seg(nid)}/plan"))

    # -- runtime visibility (orchestrator, read-only) ----------------------------
    def platform_status(self) -> dict:
        return self.request("GET", self.r("/status"))

    def list_runs(self) -> list[dict]:
        return self.request("GET", self.r("/runs"))

    def get_run_regression(self, run_id: str) -> dict | None:
        return self._get_or_none(self.r(f"/runs/{self.seg(run_id)}/regression"))

    def list_network_runs(self) -> list[dict]:
        return self.request("GET", self.r("/topology-runs"))

    def list_running_participants(self) -> list[dict]:
        return self.request("GET", self.r("/participants"))

    def list_running_simulators(self) -> list[dict]:
        return self.request("GET", self.r("/simulators"))

    def list_load_runs(self) -> list[dict]:
        return self.request("GET", self.r("/load-runs"))

    def get_load_run(self, run_id: str) -> dict | None:
        return self._get_or_none(
            self.r(f"/load-runs/{urllib.parse.quote(run_id, safe='')}"))

    def start_load_run(self, spec: dict) -> dict:
        return self.request("POST", self.r("/load-runs"), spec)

    # -- model insights (insight-service, ADR-0005; read-only + advisory) --------
    def get_run_insights(self, run_id: str) -> dict | None:
        return self._get_or_none(
            self.i(f"/insights/failures/{urllib.parse.quote(run_id, safe='')}"))

    def list_insight_predictions(self, environment: str | None = None) -> dict:
        q = f"?environment={urllib.parse.quote(environment)}" if environment else ""
        return self.request("GET", self.i(f"/insights/predictions{q}"))

    # -- playground (orchestrator, ADR-0007: ad-hoc execution by reference) ------
    def playground_targets(self) -> dict:
        return self.request("GET", self.r("/playground/targets"))

    def playground_execute(self, target: dict, action: str, payload: dict,
                           message_format_id: str | None = None,
                           label: str | None = None) -> dict:
        return self.request("POST", self.r("/playground/execute"), {
            "target": target or {}, "action": action, "payload": payload or {},
            "message_format_id": message_format_id, "label": label})
