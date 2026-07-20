"""Persistence for Participant groups — file-backed registry (mirrors flows)."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.participant_group import ParticipantGroup, ParticipantGroupDraft


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class ParticipantGroupStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._groups: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._groups = dict(json.loads(self._path.read_text()).get("groups") or {})
            except (json.JSONDecodeError, OSError):
                self._groups = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"groups": self._groups}, indent=2))
        tmp.replace(self._path)

    def list(self) -> list[ParticipantGroup]:
        return [ParticipantGroup(**{**doc, "id": gid}) for gid, doc in self._groups.items()]

    def get(self, gid: str) -> ParticipantGroup | None:
        doc = self._groups.get(gid)
        return ParticipantGroup(**{**doc, "id": gid}) if doc else None

    def _unique_id(self, base: str) -> str:
        gid, n = base, 2
        while gid in self._groups:
            gid = f"{base}-{n}"; n += 1
        return gid

    def create(self, draft: ParticipantGroupDraft, gid: str | None = None) -> ParticipantGroup:
        new_id = gid or self._unique_id(_slug(draft.name))
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._groups[new_id] = doc
            self._save()
        return ParticipantGroup(**{**doc, "id": new_id})

    def upsert(self, gid: str, draft: ParticipantGroupDraft) -> ParticipantGroup:
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._groups[gid] = doc
            self._save()
        return ParticipantGroup(**{**doc, "id": gid})

    def delete(self, gid: str) -> None:
        with self._lock:
            if gid not in self._groups:
                raise KeyError(gid)
            self._groups.pop(gid)
            self._save()
