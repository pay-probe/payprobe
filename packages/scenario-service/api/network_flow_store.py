"""Persistence for Network flows — file-backed registry (mirrors topologies).

Includes the one-shot ADR-0004 migration: on first start (empty registry) every
legacy Topology is converted into a Network flow — participants become
``participant`` nodes (list order preserved, which *is* the legacy start
order), the initiator becomes an autostart ``scenario`` node. The legacy
``topologies.json`` is read-only input here; the `/topologies` HTTP API itself
has been removed.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.network_flow import NetworkFlow, NetworkFlowDraft
from models.scenario import Step
from models.topology import Topology


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def topology_to_network_flow(topo: Topology) -> NetworkFlowDraft:
    """Convert a legacy Topology into an equivalent NetworkFlowDraft.

    No wiring edges are fabricated — the topology's list order carries the
    start order (that was its contract), and ``plan_network_flow`` preserves
    node list order when there are no edges. Real wiring can be drawn in the
    editor afterwards.
    """
    nodes: list[Step] = []
    used: set[str] = set()
    for p in topo.participants:
        nid, n = p.flow_id, 2
        while nid in used:
            nid = f"{p.flow_id}-{n}"; n += 1
        used.add(nid)
        nodes.append(Step(
            id=nid, kind="participant",
            config={"flow_id": p.flow_id, "instances": p.instances},
        ))
    if topo.initiator is not None:
        nid, n = "initiator", 2
        while nid in used:
            nid = f"initiator-{n}"; n += 1
        nodes.append(Step(
            id=nid, kind="scenario",
            config={
                "scenario_id": topo.initiator.scenario_id,
                "environment": topo.initiator.environment,
                "autostart": True,
            },
        ))
    return NetworkFlowDraft(
        name=topo.name, description=topo.description, nodes=nodes)


class NetworkFlowStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._flows: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._flows = dict(
                    json.loads(self._path.read_text()).get("network_flows") or {})
            except (json.JSONDecodeError, OSError):
                self._flows = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"network_flows": self._flows}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[NetworkFlow]:
        return [NetworkFlow(**{**doc, "id": nid}) for nid, doc in self._flows.items()]

    def get(self, nid: str) -> NetworkFlow | None:
        doc = self._flows.get(nid)
        return NetworkFlow(**{**doc, "id": nid}) if doc else None

    # -- write ---------------------------------------------------------------

    def _unique_id(self, base: str) -> str:
        nid, n = base, 2
        while nid in self._flows:
            nid = f"{base}-{n}"; n += 1
        return nid

    def create(self, draft: NetworkFlowDraft, nid: str | None = None) -> NetworkFlow:
        new_id = nid or self._unique_id(_slug(draft.name))
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._flows[new_id] = doc
            self._save()
        return NetworkFlow(**{**doc, "id": new_id})

    def upsert(self, nid: str, draft: NetworkFlowDraft) -> NetworkFlow:
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._flows[nid] = doc
            self._save()
        return NetworkFlow(**{**doc, "id": nid})

    def delete(self, nid: str) -> None:
        with self._lock:
            if nid not in self._flows:
                raise KeyError(nid)
            self._flows.pop(nid)
            self._save()

    # -- migration -------------------------------------------------------------

    def migrate_topologies(self, topologies: list[Topology]) -> list[str]:
        """One-shot seed from legacy topologies (only into an empty registry).

        Reuses each topology's id so anything that referenced a topology id
        (``requires_topology`` gates, bookmarks) finds the same network flow.
        Returns the ids migrated.
        """
        if self._flows:
            return []
        migrated: list[str] = []
        for topo in topologies:
            self.create(topology_to_network_flow(topo), nid=topo.id)
            migrated.append(topo.id)
        return migrated
