"""Persistence for Participant flows — file-backed registry with version history.

A participant flow is a named graph (trigger → logic → reply) the worker runs as
a listening service. Stored as a JSON document (``:memory:`` for tests), same
low-write, single-service pattern as connections / environments / starter flows.

Versioning (ADR-0004 Phase 0): every save bumps ``version`` and appends the
previous document to a bounded per-flow history, so a network flow can pin or
diff a participant's graph the way scenario runs pin scenario versions. The
history lives in the same JSON file under ``"history"`` — pre-versioning files
load fine (flows surface as v1 with empty history).
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.participant_flow import (
    ParticipantFlow,
    ParticipantFlowDraft,
    ParticipantFlowVersionInfo,
)

#: Retained history rows per flow (oldest dropped first). Bounds file growth;
#: generous for a hand-authored registry.
MAX_VERSIONS = 50


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class ParticipantFlowStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._flows: dict[str, dict] = {}
        #: fid → list of {version, comment, updated_at, document} (ascending).
        self._history: dict[str, list[dict]] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                self._flows = dict(data.get("flows") or {})
                self._history = dict(data.get("history") or {})
            except (json.JSONDecodeError, OSError):
                self._flows = {}
                self._history = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"flows": self._flows, "history": self._history}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[ParticipantFlow]:
        return [ParticipantFlow(**{**doc, "id": fid}) for fid, doc in self._flows.items()]

    def get(self, fid: str, version: int | None = None) -> ParticipantFlow | None:
        doc = self._flows.get(fid)
        if doc is None:
            return None
        current = int(doc.get("version", 1))
        if version is None or version == current:
            return ParticipantFlow(**{**doc, "id": fid})
        for row in self._history.get(fid, []):
            if row.get("version") == version:
                return ParticipantFlow(
                    **{**row["document"], "id": fid,
                       "version": version, "updated_at": row.get("updated_at")})
        return None

    def versions(self, fid: str) -> list[ParticipantFlowVersionInfo] | None:
        """Version metadata, newest first (current version included). ``None``
        if the flow does not exist."""
        doc = self._flows.get(fid)
        if doc is None:
            return None
        rows = [ParticipantFlowVersionInfo(
            version=int(doc.get("version", 1)),
            updated_at=doc.get("updated_at"),
            comment="current",
            node_count=len(doc.get("nodes") or []),
        )]
        for row in reversed(self._history.get(fid, [])):
            rows.append(ParticipantFlowVersionInfo(
                version=row.get("version", 1),
                updated_at=row.get("updated_at"),
                comment=row.get("comment", ""),
                node_count=len((row.get("document") or {}).get("nodes") or []),
            ))
        return rows

    # -- write ---------------------------------------------------------------

    def _unique_id(self, base: str) -> str:
        fid, n = base, 2
        while fid in self._flows:
            fid = f"{base}-{n}"; n += 1
        return fid

    def create(self, draft: ParticipantFlowDraft, fid: str | None = None) -> ParticipantFlow:
        # A flow's id is a stable, unique handle independent of its (mutable,
        # possibly blank/duplicate) name — so a new flow never collides or lands
        # on an ugly slug like "untitled-scenario". An explicit ``fid`` is honoured
        # for imports/round-trips.
        new_id = fid or self._unique_id(f"flow-{uuid.uuid4().hex[:8]}")
        doc = draft.model_dump()
        doc["version"] = 1
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._flows[new_id] = doc
            self._save()
        return ParticipantFlow(**{**doc, "id": new_id})

    def upsert(self, fid: str, draft: ParticipantFlowDraft, comment: str = "") -> ParticipantFlow:
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            prev = self._flows.get(fid)
            if prev is None:
                doc["version"] = 1
            else:
                doc["version"] = int(prev.get("version", 1)) + 1
                snapshot = {k: v for k, v in prev.items()
                            if k not in ("version", "updated_at")}
                rows = self._history.setdefault(fid, [])
                rows.append({
                    "version": int(prev.get("version", 1)),
                    "comment": comment,
                    "updated_at": prev.get("updated_at"),
                    "document": snapshot,
                })
                del rows[:-MAX_VERSIONS]
            self._flows[fid] = doc
            self._save()
        return ParticipantFlow(**{**doc, "id": fid})

    def delete(self, fid: str) -> None:
        with self._lock:
            if fid not in self._flows:
                raise KeyError(fid)
            self._flows.pop(fid)
            self._history.pop(fid, None)
            self._save()
