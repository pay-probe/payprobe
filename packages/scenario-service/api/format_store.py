"""Persistence for the Message Format registry (versioned ISO specs).

Built-in seeds live in code (``BUILTIN_FORMATS``); user-created formats are kept
in a JSON document (``FORMATS_FILE``; ``:memory:`` keeps them in-process for
tests). Like the catalog store this is deliberately file-backed — low write,
single service, no schema migration.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.message_format import BUILTIN_FORMATS, MessageFormat, MessageFormatDraft


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class FormatStore:
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
                self._custom = dict(json.loads(self._path.read_text()).get("formats") or {})
            except (json.JSONDecodeError, OSError):
                self._custom = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"formats": self._custom}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self, protocol: str | None = None) -> list[MessageFormat]:
        builtin_ids = {f.id for f in BUILTIN_FORMATS}
        out: list[MessageFormat] = []
        for f in BUILTIN_FORMATS:
            if f.id in self._custom:  # an override of a builtin id
                out.append(MessageFormat(**{**self._custom[f.id], "id": f.id}))
            else:
                out.append(f)
        for fid, doc in self._custom.items():
            if fid not in builtin_ids:
                out.append(MessageFormat(**{**doc, "id": fid}))
        if protocol:
            out = [f for f in out if f.protocol == protocol]
        return out

    def get(self, fid: str) -> MessageFormat | None:
        return next((f for f in self.list() if f.id == fid), None)

    # -- write ---------------------------------------------------------------

    def _unique_id(self, base: str) -> str:
        existing = {f.id for f in self.list()}
        fid, n = base, 2
        while fid in existing:
            fid = f"{base}-{n}"; n += 1
        return fid

    def create(self, draft: MessageFormatDraft, fid: str | None = None) -> MessageFormat:
        new_id = fid or self._unique_id(_slug(f"{draft.name}-{draft.version}"))
        doc = draft.model_dump()
        doc["builtin"] = False
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._custom[new_id] = doc
            self._save()
        return MessageFormat(**{**doc, "id": new_id})

    def update(self, fid: str, draft: MessageFormatDraft) -> MessageFormat:
        doc = draft.model_dump()
        doc["builtin"] = False
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._custom[fid] = doc
            self._save()
        return MessageFormat(**{**doc, "id": fid})

    def clone(self, fid: str, name: str | None, version: str | None) -> MessageFormat:
        src = self.get(fid)
        if src is None:
            raise KeyError(fid)
        draft = MessageFormatDraft(
            protocol=src.protocol,
            name=name or f"{src.name} (copy)",
            version=version or "1",
            group=src.group,
            description=src.description,
            definition=src.definition,
        )
        return self.create(draft)

    def delete(self, fid: str) -> None:
        if fid in {f.id for f in BUILTIN_FORMATS} and fid not in self._custom:
            raise ValueError("built-in formats cannot be deleted — clone instead")
        with self._lock:
            if self._custom.pop(fid, None) is None:
                raise KeyError(fid)
            self._save()
