"""Persistence for Starter Flows (built-in seeds + user flows).

Built-in flows live in code (``BUILTIN_FLOWS``); user-created flows — and
overrides of a built-in id — are kept in a JSON document (``FLOWS_FILE``;
``:memory:`` for tests). Same file-backed, builtin-plus-custom approach as the
Message Format registry.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.starter_flow import BUILTIN_FLOWS, StarterFlow, StarterFlowDraft


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class StarterFlowStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._custom: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._custom = dict(json.loads(self._path.read_text()).get("flows") or {})
            except (json.JSONDecodeError, OSError):
                self._custom = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"flows": self._custom}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[StarterFlow]:
        builtin_ids = {f.id for f in BUILTIN_FLOWS}
        out: list[StarterFlow] = []
        for f in BUILTIN_FLOWS:
            if f.id in self._custom:  # an override of a builtin id
                out.append(StarterFlow(**{**self._custom[f.id], "id": f.id, "builtin": True}))
            else:
                out.append(f)
        for fid, doc in self._custom.items():
            if fid not in builtin_ids:
                out.append(StarterFlow(**{**doc, "id": fid, "builtin": False}))
        return out

    def get(self, fid: str) -> StarterFlow | None:
        return next((f for f in self.list() if f.id == fid), None)

    # -- write ---------------------------------------------------------------

    def _unique_id(self, base: str) -> str:
        existing = {f.id for f in self.list()}
        fid, n = base, 2
        while fid in existing:
            fid = f"{base}-{n}"; n += 1
        return fid

    def _is_builtin(self, fid: str) -> bool:
        return fid in {f.id for f in BUILTIN_FLOWS}

    def create(self, draft: StarterFlowDraft, fid: str | None = None) -> StarterFlow:
        new_id = fid or self._unique_id(_slug(draft.label))
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._custom[new_id] = doc
            self._save()
        return StarterFlow(**{**doc, "id": new_id, "builtin": self._is_builtin(new_id)})

    def update(self, fid: str, draft: StarterFlowDraft) -> StarterFlow:
        doc = draft.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._custom[fid] = doc
            self._save()
        return StarterFlow(**{**doc, "id": fid, "builtin": self._is_builtin(fid)})

    def delete(self, fid: str) -> None:
        """Delete a custom flow, or reset a builtin override back to its seed."""
        with self._lock:
            if fid in self._custom:
                self._custom.pop(fid)
                self._save()
                return
            if self._is_builtin(fid):
                raise ValueError("built-in flows cannot be deleted — edit to override")
            raise KeyError(fid)
