"""Persistence + merge for the editable step catalog.

The built-in catalog (``models.catalog.STEP_CATALOG``) ships in code. On top of
it users may:
- add brand-new custom targets/actions,
- override a built-in target (same id) to relabel it, tweak payload hints,
  add response fields, hide/add actions,
- hide a built-in target entirely.

Customisations live in a single JSON document (path from ``CATALOG_FILE``; an
empty/``:memory:`` path keeps them in-process, which is what the tests use). The
store is deliberately file-backed rather than SQL: it is low-write, single
service, and avoids a schema migration across the dual scenario store.

Document shape::

    {
      "targets": { "<target_id>": <TargetSpec doc>, ... },   # custom + overrides
      "hidden":  ["<built_in_target_id>", ...]               # hidden built-ins
    }
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from models.catalog import STEP_CATALOG, TargetSpec


class CatalogStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._doc: dict[str, Any] = {"targets": {}, "hidden": []}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                self._doc = {
                    "targets": dict(data.get("targets") or {}),
                    "hidden": list(data.get("hidden") or []),
                }
            except (json.JSONDecodeError, OSError):
                self._doc = {"targets": {}, "hidden": []}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._doc, indent=2))
        tmp.replace(self._path)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _builtin_ids() -> set[str]:
        return {t.target for t in STEP_CATALOG}

    # -- read ----------------------------------------------------------------

    def merged(self) -> list[TargetSpec]:
        """Built-ins (overridden / minus hidden) followed by new custom targets."""
        custom = self._doc["targets"]
        hidden = set(self._doc["hidden"])
        out: list[TargetSpec] = []
        builtin_ids = self._builtin_ids()

        for spec in STEP_CATALOG:
            if spec.target in hidden:
                continue
            if spec.target in custom:
                out.append(TargetSpec(**{**custom[spec.target], "custom": True}))
            else:
                out.append(spec)

        for tid, doc in custom.items():
            if tid not in builtin_ids:
                out.append(TargetSpec(**{**doc, "custom": True}))
        return out

    def manage_view(self) -> dict:
        """Everything the editor needs: every target plus its provenance flags."""
        custom = self._doc["targets"]
        hidden = set(self._doc["hidden"])
        builtin_ids = self._builtin_ids()
        targets: list[dict] = []

        for spec in STEP_CATALOG:
            overridden = spec.target in custom
            doc = (
                {**custom[spec.target], "custom": True}
                if overridden else spec.model_dump()
            )
            targets.append({
                **doc,
                "builtin": True,
                "overridden": overridden,
                "hidden": spec.target in hidden,
            })

        for tid, doc in custom.items():
            if tid not in builtin_ids:
                targets.append({
                    **doc, "custom": True, "builtin": False,
                    "overridden": False, "hidden": False,
                })
        return {"targets": targets}

    # -- write ---------------------------------------------------------------

    def upsert(self, spec: TargetSpec) -> TargetSpec:
        doc = spec.model_dump()
        doc.pop("custom", None)  # provenance is derived, not stored
        with self._lock:
            self._doc["targets"][spec.target] = doc
            # saving an override of a hidden built-in implicitly unhides it
            if spec.target in self._doc["hidden"]:
                self._doc["hidden"].remove(spec.target)
            self._save()
        return TargetSpec(**{**doc, "custom": True})

    def delete(self, target: str) -> None:
        """Remove a custom target; hide a built-in (so it drops from the palette)."""
        with self._lock:
            existed = self._doc["targets"].pop(target, None) is not None
            if target in self._builtin_ids():
                if target not in self._doc["hidden"]:
                    self._doc["hidden"].append(target)
            elif not existed:
                raise KeyError(target)
            self._save()

    def restore(self, target: str) -> None:
        """Drop an override and/or unhide — return a built-in to its default."""
        with self._lock:
            self._doc["targets"].pop(target, None)
            if target in self._doc["hidden"]:
                self._doc["hidden"].remove(target)
            self._save()
