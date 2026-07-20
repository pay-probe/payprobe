"""Global named data tables.

A *table* is a named set of rows (with named columns) you can maintain once and
read from many steps — e.g. card profiles, BIN ranges, response-code maps. Tables
are **global** (Phase 2 scope). At run time they are injected into the worker as
``context["tables"]`` (``{table_name: [row, ...]}``) so steps can look rows up,
and the whole table resolves via ``${tables.NAME}``.

Persisted as one JSON document (``TABLES_FILE``; ``:memory:`` for tests)::

    {"tables": {name: {"description", "columns": [...], "rows": [{col: val}, ...]}}}
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DataTableDraft(BaseModel):
    description: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class DataTable(DataTableDraft):
    name: str


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return s or "table"


class TableStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._tables: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._tables = dict(json.loads(self._path.read_text()).get("tables") or {})
            except (json.JSONDecodeError, OSError):
                self._tables = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"tables": self._tables}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[DataTable]:
        return [DataTable(name=n, **doc) for n, doc in sorted(self._tables.items())]

    def get(self, name: str) -> DataTable | None:
        doc = self._tables.get(name)
        return DataTable(name=name, **doc) if doc is not None else None

    def runtime(self) -> dict[str, list]:
        """The injectable ``{table_name: rows}`` map for the worker."""
        return {n: list(doc.get("rows") or []) for n, doc in self._tables.items()}

    # -- write ---------------------------------------------------------------

    def upsert(self, name: str, draft: DataTableDraft) -> DataTable:
        key = slug(name)
        with self._lock:
            self._tables[key] = draft.model_dump()
            self._save()
        return DataTable(name=key, **self._tables[key])

    def delete(self, name: str) -> None:
        with self._lock:
            if self._tables.pop(name, None) is None:
                raise KeyError(name)
            self._save()
