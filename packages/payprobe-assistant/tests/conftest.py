"""Test fixtures for payprobe-assistant.

Defines the in-memory fake of the PayProbe REST surface here (not a separate
importable module) so it works whether tests run alone or alongside the other
packages' ``tests`` dirs (no cross-package module-name clash).
"""
import os
import sys
from pathlib import Path

# The caller gate fails closed outside dev/test; unit tests drive endpoints
# without tokens, so mark the environment (auth itself is tested explicitly
# with monkeypatched env in test_assistant_auth.py).
os.environ.setdefault("PAYPROBE_ENV", "test")

# Keep tests hermetic: never let resolve_llm() reach out to a live
# scenario-service for the Settings-stored LLM config (on a dev machine with
# the stack running, real config would leak into the needs_config tests).
# test_settings_llm.py re-enables it with a monkeypatched transport.
os.environ.setdefault("ASSIST_SETTINGS_LLM", "0")

# make the assistant_service package importable when running from packages/,
# and packages/ itself importable (payprobe_common) when running from the
# package dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from assistant_service import rest


def _path(url: str) -> str:
    return url.replace(rest.SCENARIO_API, "").replace(rest.RUN_API, "")


class FakeBackend:
    """Routes (method, path) over dicts; raises RuntimeError('HTTP 404/400 …')
    exactly like the real transport so error/guardrail paths are exercised."""

    def __init__(self) -> None:
        self.connections: dict[str, dict] = {}
        self.tables: dict[str, dict] = {}
        self.variables = {"variables": {}, "secrets": []}
        self.flows: dict[str, dict] = {
            "builtin_demo": {"id": "builtin_demo", "builtin": True,
                             "label": "Builtin", "steps": []}}
        self.scenarios: dict[str, dict] = {}
        self.environments: dict[str, dict] = {
            "mock": {"name": "mock", "description": "Full mock", "source": "bundled"}}
        self.network_flows: dict[str, dict] = {}
        #: participant-flow ids the fake /network-flows/validate treats as real
        self.known_flows: set[str] = {"issuer-flow"}
        self._sid = 0

    def __call__(self, method: str, url: str, body=None, raw: bool = False):
        p = _path(url)

        if p == "/connections" and method == "GET":
            return [{"name": k, **v} for k, v in self.connections.items()]
        if p.startswith("/connections/"):
            name = p.split("/", 2)[2]
            if method == "GET":
                if name not in self.connections:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return {"name": name, **self.connections[name]}
            if method == "PUT":
                self.connections[name] = dict(body or {})
                return {"name": name, **self.connections[name]}
            if method == "DELETE":
                self.connections.pop(name, None)
                return None

        if p == "/tables" and method == "GET":
            return [{"name": k, **v} for k, v in self.tables.items()]
        if p.startswith("/tables/"):
            name = p.split("/", 2)[2]
            if method == "GET":
                if name not in self.tables:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return {"name": name, **self.tables[name]}
            if method == "PUT":
                self.tables[name] = dict(body or {})
                return {"name": name, **self.tables[name]}
            if method == "DELETE":
                self.tables.pop(name, None)
                return None

        if p == "/variables/global":
            if method == "GET":
                return dict(self.variables)
            if method == "PUT":
                self.variables = {"variables": dict((body or {}).get("variables", {})),
                                  "secrets": list((body or {}).get("secrets", []))}
                return dict(self.variables)

        if p == "/starter-flows" and method == "GET":
            return list(self.flows.values())
        if p == "/starter-flows" and method == "POST":
            self._sid += 1
            fid = f"flow_{self._sid}"
            self.flows[fid] = {"id": fid, "builtin": False, **(body or {})}
            return self.flows[fid]
        if p.startswith("/starter-flows/"):
            fid = p.split("/", 2)[2]
            if method == "GET":
                if fid not in self.flows:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return self.flows[fid]
            if method == "DELETE":
                if self.flows.get(fid, {}).get("builtin"):
                    raise RuntimeError(f"HTTP 400 {url}: cannot delete a builtin")
                self.flows.pop(fid, None)
                return None

        if p == "/environments" and method == "GET":
            return list(self.environments.values())
        if p.startswith("/environments/"):
            name = p.split("/", 2)[2]
            if method == "GET":
                if name not in self.environments:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return self.environments[name]

        if p == "/network-flows" and method == "GET":
            return [{"id": k, **v} for k, v in self.network_flows.items()]
        if p == "/network-flows/validate" and method == "POST":
            issues = []
            for n in (body or {}).get("nodes", []):
                cfg = n.get("config") or {}
                if n.get("kind") == "participant":
                    fid = cfg.get("flow_id", "")
                    if not fid:
                        issues.append({"severity": "error",
                                       "message": "participant node needs a flow_id"})
                    elif fid not in self.known_flows:
                        issues.append({"severity": "error",
                                       "message": f"participant flow '{fid}' does not exist"})
            return {"valid": not issues, "issues": issues}
        if p.startswith("/network-flows/"):
            rest_path = p.split("/", 2)[2]
            if rest_path.endswith("/plan") and method == "GET":
                nid = rest_path[: -len("/plan")]
                if nid not in self.network_flows:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                doc = self.network_flows[nid]
                parts = [{"node_id": n["id"], "flow_id": n["config"].get("flow_id"),
                          "instances": n["config"].get("instances", 1), "port": None}
                         for n in doc.get("nodes", []) if n.get("kind") == "participant"]
                return {"participants": parts, "initiators": [],
                        "simulators": [], "warnings": []}
            nid = rest_path
            if method == "GET":
                if nid not in self.network_flows:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return {"id": nid, **self.network_flows[nid]}
            if method == "PUT":
                self.network_flows[nid] = dict(body or {})
                return {"id": nid, **self.network_flows[nid]}
            if method == "DELETE":
                self.network_flows.pop(nid, None)
                return None

        # -- run API (orchestrator) runtime reads --
        if p == "/status" and method == "GET":
            return {"status": "ok", "services": {"orchestrator": "up"}}
        if p == "/runs" and method == "GET":
            return [{"id": "run-1", "label": "nightly", "status": "completed"}]
        if p == "/topology-runs" and method == "GET":
            return [{"id": "tr-1", "topology_id": "hsm-simulators",
                     "kind": "network_flow",
                     "health": {"total": 2, "live": 2, "ready": True}}]
        if p == "/participants" and method == "GET":
            return [{"id": "p-1", "flow_id": "issuer", "port": 8201}]
        if p == "/simulators" and method == "GET":
            return [{"id": "hsm-8000", "port": 8000, "received": 42}]
        if p == "/load-runs" and method == "GET":
            return [{"id": "lr-1", "status": "completed", "tps": 1200}]

        if p == "/catalog" and method == "GET":
            return [{"target": "http", "actions": [{"name": "send_auth_request"}]}]
        if p == "/formats" and method == "GET":
            return []

        if p == "/validate" and method == "POST":
            steps = (body or {}).get("steps", [])
            ids = [s.get("id") for s in steps]
            valid = len(ids) == len(set(ids))
            return {"valid": valid, "issues": [] if valid else ["duplicate step id"]}

        if p == "/scenarios" and method == "GET":
            return [{"id": k, "name": v.get("name")} for k, v in self.scenarios.items()]
        if p == "/scenarios" and method == "POST":
            self._sid += 1
            sid = f"sc_{self._sid}"
            doc = dict((body or {}).get("scenario", {}))
            doc.update(id=sid, version=1)
            self.scenarios[sid] = doc
            return doc
        if p.startswith("/scenarios/"):
            sid = p.split("/", 2)[2]
            if method == "GET":
                if sid not in self.scenarios:
                    raise RuntimeError(f"HTTP 404 {url}: not found")
                return self.scenarios[sid]
            if method == "PUT":
                doc = dict((body or {}).get("scenario", {}))
                prev = self.scenarios.get(sid, {})
                doc.update(id=sid, version=prev.get("version", 1) + 1)
                self.scenarios[sid] = doc
                return doc
            if method == "DELETE":
                self.scenarios.pop(sid, None)
                return None

        raise RuntimeError(f"HTTP 404 {url}: unhandled in fake ({method})")


@pytest.fixture()
def backend(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(rest, "request", fb)
    return fb
