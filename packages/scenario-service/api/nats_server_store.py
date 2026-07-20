"""Persistence for named NATS *servers* / clusters (ADR-0006).

A NATS **server registry entry** is a named broker target: the client URLs a
connection dials (``servers[]``), the monitoring endpoints its nodes expose
(``monitoring[]`` — the ``:8222`` HTTP ports used for ``/healthz`` and ``/varz``),
and optional auth. It is the messaging-fabric analog of the Environments /
Connections registries: point every NATS connection at ``payprobe-cluster`` once,
and the Cluster-health and Streams pages know which nodes to probe and manage.

Stored as one JSON document (``NATS_SERVERS_FILE``; ``:memory:`` for tests),
keyed by slug — the same low-write, single-service, file-backed approach as the
other registries::

    {"nats_servers": {slug: {name, servers, monitoring, description, auth, ...}}}

``extra="allow"`` keeps any forward-compatible keys. Secret-looking auth fields
(token, nkey_seed, creds, password) are SecretBox-encrypted at rest, exactly like
Connections; the in-memory copy stays plaintext for probing/admin.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .crypto import SecretBox, default_box, fingerprint, is_secret_key


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", (text or "").lower()).strip("-_.")
    return s or "cluster"


class NatsServerDraft(BaseModel):
    """Editable fields of a NATS server registry entry (everything but the key)."""

    model_config = ConfigDict(extra="allow")  # keep forward-compatible keys

    #: Human label (the registry key is the slug passed to upsert).
    name: str = ""
    description: str = ""
    #: Client URLs, e.g. ["nats://nats-1:4222", "nats://nats-2:4223"].
    servers: list[str] = []
    #: Monitoring HTTP bases per node, e.g. ["http://nats-1:8222", …] — used for
    #: /healthz and /varz. When empty, the health page derives them from the
    #: client URLs (host + default monitoring port 8222).
    monitoring: list[str] = []
    #: Optional auth applied when this cluster is probed for JetStream admin.
    #: token / user / password / nkey_seed / creds (secrets encrypted at rest).
    auth: dict[str, Any] = {}


class NatsServer(NatsServerDraft):
    #: Registry key (slug). Distinct from the ``name`` label field.
    key: str = ""


class NatsServerStore:
    def __init__(self, path: str | None = None, secret_box: SecretBox | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._box = secret_box or default_box
        self._servers: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                raw = dict(json.loads(self._path.read_text()).get("nats_servers") or {})
            except (json.JSONDecodeError, OSError):
                raw = {}
            self._servers = {n: self._box.decrypt_doc(doc) for n, doc in raw.items()}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        on_disk = {n: self._box.encrypt_doc(doc) for n, doc in self._servers.items()}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"nats_servers": on_disk}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._servers)

    def list(self) -> list[NatsServer]:
        return [NatsServer(key=n, **doc) for n, doc in sorted(self._servers.items())]

    def get(self, name: str) -> NatsServer | None:
        doc = self._servers.get(name)
        return NatsServer(key=name, **doc) if doc is not None else None

    def get_config(self, name: str) -> dict | None:
        """Raw doc (with decrypted auth) for probing/admin, or None."""
        doc = self._servers.get(name)
        return dict(doc) if doc is not None else None

    # -- secrets inventory ---------------------------------------------------

    def secret_refs(self) -> list[dict]:
        """Masked references to secret-named auth fields across clusters.

        Returns ``{owner, field, fingerprint}`` per secret value — never the
        value itself. Used by the Secrets Vault inventory.
        """
        refs: list[dict] = []
        for name in sorted(self._servers):
            auth = self._servers[name].get("auth") or {}
            for field, value in auth.items():
                if is_secret_key(str(field)) and isinstance(value, str) and value:
                    refs.append({"owner": name, "field": f"auth.{field}",
                                 "fingerprint": fingerprint(value)})
        return refs

    # -- write ---------------------------------------------------------------

    def upsert(self, name: str, draft: NatsServerDraft) -> NatsServer:
        key = slug(name)
        doc = draft.model_dump(exclude_unset=False)
        doc.pop("key", None)
        if not doc.get("name"):
            doc["name"] = key
        with self._lock:
            self._servers[key] = doc
            self._save()
        return NatsServer(key=key, **doc)

    def delete(self, name: str) -> None:
        with self._lock:
            if self._servers.pop(name, None) is None:
                raise KeyError(name)
            self._save()

    # -- seeding -------------------------------------------------------------

    def seed_default(
        self, servers: list[str], monitoring: list[str] | None = None,
        *, key: str = "payprobe-cluster", name: str = "PayProbe cluster",
        description: str = "Default NATS cluster (from docker-compose)",
    ) -> bool:
        """Seed one default cluster entry when the registry is empty, so the NATS
        page can connect out of the box against the bundled compose broker. No-op
        if any cluster is already registered (never clobbers user edits). Returns
        True if it seeded."""
        if self._servers or not servers:
            return False
        with self._lock:
            self._servers[key] = {
                "name": name, "description": description,
                "servers": list(servers), "monitoring": list(monitoring or []),
                "auth": {},
            }
            self._save()
        return True
