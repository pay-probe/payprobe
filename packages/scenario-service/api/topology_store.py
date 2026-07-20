"""Persistence for Topologies — file-backed registry (mirrors participant flows)."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.topology import Topology, TopologyDraft


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class TopologyStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._topos: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._topos = dict(json.loads(self._path.read_text()).get("topologies") or {})
            except (json.JSONDecodeError, OSError):
                self._topos = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"topologies": self._topos}, indent=2))
        tmp.replace(self._path)

    def list(self) -> list[Topology]:
        return [Topology(**{**doc, "id": tid}) for tid, doc in self._topos.items()]

    def get(self, tid: str) -> Topology | None:
        doc = self._topos.get(tid)
        return Topology(**{**doc, "id": tid}) if doc else None

    def _unique_id(self, base: str) -> str:
        tid, n = base, 2
        while tid in self._topos:
            tid = f"{base}-{n}"; n += 1
        return tid

    def create(self, draft: TopologyDraft, tid: str | None = None) -> Topology:
        new_id = tid or self._unique_id(_slug(draft.name))
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._topos[new_id] = doc
            self._save()
        return Topology(**{**doc, "id": new_id})

    def upsert(self, tid: str, draft: TopologyDraft) -> Topology:
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._topos[tid] = doc
            self._save()
        return Topology(**{**doc, "id": tid})

    def delete(self, tid: str) -> None:
        with self._lock:
            if tid not in self._topos:
                raise KeyError(tid)
            self._topos.pop(tid)
            self._save()
