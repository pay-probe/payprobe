"""Persistence for run *environments* (named adapter-target profiles).

An *environment* is the configuration a run executes against: which adapter
instances exist (``adapters``), their connection budget, and an optional
``adapter_defaults`` / ``mode``. PayProbe ships bundled environments as JSON
files (``examples/environments/*.json``); this registry makes them
**first-class and editable** — create dev/staging/prod profiles, clone one,
tweak a host, all from the UI — instead of hand-editing files.

Stored as one JSON document (``ENVIRONMENTS_FILE``; ``:memory:`` for tests),
keyed by environment name — the same low-write, single-service, file-backed
approach as the Connections / Tables / Test-Data registries::

    {"environments": {name: {name, description, adapters, ...}}}

The model is intentionally permissive (``extra="allow"``): an environment's
adapter config carries arbitrary, adapter-specific keys (base_url, framing,
pkcs11_lib_path, …) and we persist whatever the editor sends. Secret-looking
fields (api_key, password, …) are encrypted at rest via :class:`SecretBox`,
exactly like Connections; the in-memory copy stays plaintext for run assembly.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .crypto import SecretBox, default_box


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", (text or "").lower()).strip("-_.")
    return s or "environment"


class EnvironmentDraft(BaseModel):
    """Editable fields of an environment (everything except its registry key)."""

    model_config = ConfigDict(extra="allow")  # keep arbitrary adapter config

    #: Human label / description (``name`` here is the display label; the
    #: registry key is the slug passed to upsert).
    name: str = ""
    description: str = ""
    adapters: dict[str, Any] = {}


class Environment(EnvironmentDraft):
    #: Registry key (slug). Distinct from the ``name`` label field.
    key: str = ""


class EnvironmentStore:
    def __init__(self, path: str | None = None, secret_box: SecretBox | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._box = secret_box or default_box
        self._envs: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                raw = dict(json.loads(self._path.read_text()).get("environments") or {})
            except (json.JSONDecodeError, OSError):
                raw = {}
            self._envs = {n: self._box.decrypt_doc(doc) for n, doc in raw.items()}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        on_disk = {n: self._box.encrypt_doc(doc) for n, doc in self._envs.items()}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"environments": on_disk}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._envs)

    def list(self) -> list[Environment]:
        return [Environment(key=n, **doc) for n, doc in sorted(self._envs.items())]

    def get(self, name: str) -> Environment | None:
        doc = self._envs.get(name)
        return Environment(key=name, **doc) if doc is not None else None

    def get_config(self, name: str) -> dict | None:
        """The raw env config doc (worker-shaped) for run assembly, or None."""
        doc = self._envs.get(name)
        return dict(doc) if doc is not None else None

    # -- write ---------------------------------------------------------------

    def upsert(self, name: str, draft: EnvironmentDraft) -> Environment:
        key = slug(name)
        doc = draft.model_dump(exclude_unset=False)
        doc.pop("key", None)
        if not doc.get("name"):
            doc["name"] = key
        with self._lock:
            self._envs[key] = doc
            self._save()
        return Environment(key=key, **doc)

    def delete(self, name: str) -> None:
        with self._lock:
            if self._envs.pop(name, None) is None:
                raise KeyError(name)
            self._save()

    # -- seeding -------------------------------------------------------------

    def seed_from_dir(self, dir_path: str | Path) -> int:
        """Import bundled ``*.json`` environments when the store is empty.

        Lets the management UI show the shipped environments (mock, grpc, …) the
        first time, so users can clone/edit them. No-op if any environment is
        already stored. Returns the number imported.
        """
        if self._envs:
            return 0
        d = Path(dir_path)
        if not d.is_dir():
            return 0
        count = 0
        for p in sorted(d.glob("*.json")):
            try:
                doc = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            doc.pop("_comment", None)
            key = slug(p.stem)
            doc.setdefault("name", doc.get("name") or key)
            self._envs[key] = doc
            count += 1
        if count:
            self._save()
        return count
