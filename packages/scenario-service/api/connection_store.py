"""Persistence for adapter Connections (named, reusable target connections).

A *connection* is a named, configured link to a real system the worker talks to
— a switch, an acquirer, an HSM, etc. — built on the universal TCP adapter
(``packages/worker/adapters/tcp``). Each connection records which adapter
implementation and wire protocol to use plus its host/port and protocol-specific
settings; scenarios reference a connection by name (the step ``target``), and an
environment's ``adapters`` block is assembled from them.

Stored as one JSON document (``CONNECTIONS_FILE``; ``:memory:`` for tests),
keyed by connection name — same low-write, single-service, file-backed approach
as the Message Format and Tables registries::

    {"connections": {name: {adapter, protocol, host, port, ...}}}

The model is intentionally permissive (``extra="allow"``): the universal TCP
adapter accepts many optional, protocol-specific keys (framing, correlation,
header_echo, sign_on, keepalive, …) and we persist whatever the builder sends
without having to mirror every field here.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .crypto import SecretBox, default_box, fingerprint, is_secret_key

_ALLOWED_PROTOCOLS = {"iso8583", "header_echo"}
# Adapter implementations a connection may target. The first two are the
# original wire connections — "tcp" (universal ISO 8583 / header-echo) and
# "grpc" (descriptor-driven). The rest are the other network endpoints the
# worker already supports as named adapter instances, now registerable as
# first-class connections too: "http" (generic REST), "payshield"/"hsm_client"
# (payShield host-command client), and the read-only DB probes. The worker
# AdapterRegistry resolves all of these; the model stays permissive
# (``extra="allow"``) so each carries its own adapter-specific keys (base_url,
# header, engine/dbname, …) without a schema change here.
_ALLOWED_ADAPTERS = {
    "tcp",
    "grpc",
    "http",
    "nats",
    "payshield",
    "hsm_client",
    "db_probe_core",
    "db_probe_switch",
}
# Adapters for which ``protocol`` (an ISO 8583 / header-echo wire concept) is
# meaningful. For the others ``protocol`` is irrelevant and not required.
_TCP_LIKE_ADAPTERS = {"tcp"}


class ConnectionDraft(BaseModel):
    """Editable fields of a connection (everything except its name)."""

    model_config = ConfigDict(extra="allow")  # keep protocol-specific keys

    adapter: str = "tcp"
    protocol: str = "iso8583"
    host: str = ""
    port: int = 0
    description: str = ""
    #: Direction this connection operates in. ``outbound`` (default) = client:
    #: the worker connects to ``host:port``. ``inbound`` = server: a participant
    #: flow binds and listens on the *same* ``port`` (0 = pick a free port at
    #: bind time). One port for both directions.
    mode: str = "outbound"
    #: Administratively disabled. A disabled connection is skipped as a group
    #: member (taken out of rotation) without deleting it, so it can be
    #: re-enabled later. Toggled from the Topology Map / Connections page.
    disabled: bool = False
    #: Deprecated — legacy field. A single ``port`` now serves both directions;
    #: any value here is folded into ``port`` on load (see ``_unify_port``) and
    #: never persisted going forward. Kept only to read older records.
    listen_port: int | None = None
    # NB: the old per-connection ``endpoints[]`` + ``selection`` multiplicity
    # model was removed (it overlapped confusingly with participant groups, which
    # are the supported way to fan across several targets). The model is
    # ``extra="allow"``, so any legacy values on old records are tolerated and
    # simply ignored.
    #: Per-environment parameter overrides for this connection, keyed by
    #: environment name/slug. Each value is a partial, worker-shaped adapter
    #: config that is shallow-merged over the connection's base config when the
    #: connection runs under that environment (see orchestrator
    #: ``_attach_connections``). Editable from both the Connection and the
    #: Environment editors (one source of truth — the connection). This is
    #: metadata, not adapter config, and is stripped from the base before merge.
    environment_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Marks this connection as the **default instance for its adapter type**
    #: (see docs/history/DEFAULT-CONNECTION-MODEL-SPEC.md). A step that names no connection
    #: resolves to the default of its type. At most one default per type is kept:
    #: setting ``default=True`` demotes the previous default of the same type
    #: (enforced by :class:`ConnectionStore`, not here). Metadata, not adapter
    #: config — stripped before the worker-shaped config is built.
    default: bool = False
    #: Marks a connection that points at **real external infrastructure** — a
    #: provider sandbox or live API (Stripe test mode, Adyen test, PayPal
    #: sandbox, …) rather than something PayProbe hosts. External connections
    #: are refused by the load engine at ``POST /load-runs`` — functional
    #: runs, the playground and scenarios may use them, but load-testing a
    #: real provider violates its terms of service, so the registry is the
    #: bouncer, not the docs (ADR-0009; invariant #6). Deliberately NOT
    #: stripped from the worker-shaped config: the orchestrator reads it off
    #: the resolved adapter config to enforce the refusal.
    external: bool = False

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, v: int) -> int:
        if v < 0 or v > 65535:
            raise ValueError("port must be between 0 and 65535")
        return v

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, v: str) -> str:
        if v and v not in _ALLOWED_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_ALLOWED_PROTOCOLS)}")
        return v

    @field_validator("adapter")
    @classmethod
    def _known_adapter(cls, v: str) -> str:
        if v and v not in _ALLOWED_ADAPTERS:
            raise ValueError(f"adapter must be one of {sorted(_ALLOWED_ADAPTERS)}")
        return v

    @model_validator(mode="after")
    def _unify_port(self):
        """Collapse the legacy ``listen_port`` into the single ``port``. An older
        inbound connection stored ``port`` (dial) + ``listen_port`` (bind); now
        ``port`` is the one port for both directions, so an inbound bind uses it.
        Runs on every load, so existing records self-heal without a re-save."""
        if self.listen_port:
            self.port = self.listen_port
        self.listen_port = None
        return self

    @model_validator(mode="after")
    def _check_framing(self):
        framing = (self.model_extra or {}).get("framing")
        if isinstance(framing, dict) and "length_prefix_bytes" in framing:
            try:
                lpb = int(framing["length_prefix_bytes"])
            except (TypeError, ValueError):
                raise ValueError("framing.length_prefix_bytes must be an integer")
            if lpb < 1:
                raise ValueError("framing.length_prefix_bytes must be >= 1")
        return self


class Connection(ConnectionDraft):
    name: str


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return s or "connection"


def type_key(adapter: str, protocol: str) -> str:
    """Grouping key for the 'one default per adapter type' rule. The universal
    TCP adapter serves two wire protocols (ISO 8583 vs header-echo), which are
    distinct *types* a step targets — so they get separate default slots. Every
    other adapter is its own type.
    """
    a = (adapter or "tcp").lower()
    if a == "tcp":
        return f"tcp/{(protocol or 'iso8583').lower()}"
    return a


class ConnectionStore:
    def __init__(self, path: str | None = None, secret_box: SecretBox | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        #: Secret values are encrypted at rest (see api/crypto.py). The in-memory
        #: dict always holds *plaintext* so env assembly / the worker get usable
        #: credentials; encryption happens only at the file boundary.
        self._box = secret_box or default_box
        self._connections: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                raw = dict(json.loads(self._path.read_text()).get("connections") or {})
            except (json.JSONDecodeError, OSError):
                raw = {}
            # decrypt secret fields into plaintext for in-memory/runtime use
            self._connections = {n: self._box.decrypt_doc(doc) for n, doc in raw.items()}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # encrypt secret-named fields just before they hit disk
        on_disk = {n: self._box.encrypt_doc(doc) for n, doc in self._connections.items()}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"connections": on_disk}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[Connection]:
        return [
            Connection(name=n, **doc)
            for n, doc in sorted(self._connections.items())
        ]

    def get(self, name: str) -> Connection | None:
        doc = self._connections.get(name)
        return Connection(name=name, **doc) if doc is not None else None

    def get_default(self, adapter: str = "tcp", protocol: str = "iso8583") -> Connection | None:
        """The default connection for an adapter type, or ``None``. A step that
        names no connection resolves to this (see the default-connection model)."""
        tk = type_key(adapter, protocol)
        for n, doc in sorted(self._connections.items()):
            if doc.get("default") and type_key(
                doc.get("adapter", "tcp"), doc.get("protocol", "iso8583")
            ) == tk:
                return Connection(name=n, **doc)
        return None

    def defaults(self) -> list[Connection]:
        """Every connection currently flagged as a type default."""
        return [
            Connection(name=n, **doc)
            for n, doc in sorted(self._connections.items())
            if doc.get("default")
        ]

    # -- seeding -------------------------------------------------------------

    def seed_from_dir(self, dir_path: str | Path) -> int:
        """Import bundled connection examples when the store is empty (mirrors
        ``EnvironmentStore.seed_from_dir``). Each ``*.json`` file may hold a single
        flat connection (keyed by filename) or a ``{"connections": {name: doc}}``
        map. No-op once any connection is stored. Returns the number imported.
        """
        if self._connections:
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
            if isinstance(doc, dict) and isinstance(doc.get("connections"), dict):
                items = doc["connections"]
            else:
                items = {slug(p.stem): doc}
            for name, cdoc in items.items():
                if not isinstance(cdoc, dict):
                    continue
                cdoc = dict(cdoc)
                cdoc.pop("name", None)
                self._connections[slug(name)] = cdoc
                count += 1
        if count:
            self._save()
        return count

    # -- write ---------------------------------------------------------------

    def _demote_other_defaults(self, keep_key: str, doc: dict) -> None:
        """Clear ``default`` on any *other* connection of the same adapter type,
        enforcing at most one default per type (setting a new default moves it)."""
        tk = type_key(doc.get("adapter", "tcp"), doc.get("protocol", "iso8583"))
        for other, odoc in self._connections.items():
            if (
                other != keep_key
                and odoc.get("default")
                and type_key(odoc.get("adapter", "tcp"), odoc.get("protocol", "iso8583")) == tk
            ):
                odoc["default"] = False

    def upsert(self, name: str, draft: ConnectionDraft) -> Connection:
        key = slug(name)
        # Persist only the keys actually supplied — defaulted-but-unset model
        # fields (e.g. protocol on an `extends` connection) must not be baked in,
        # or they'd override an inherited value at resolve time.
        doc = draft.model_dump(exclude_unset=True)
        doc.pop("name", None)  # name is the key, never a body field
        with self._lock:
            existing = self._connections.get(key)
            # An edit that doesn't mention ``default`` must not silently un-set it;
            # preserve the stored flag. (Sending ``default=False`` clears it.)
            if "default" not in doc and existing and existing.get("default"):
                doc["default"] = True
            # Setting a default demotes the previous default of the same type, so
            # there is always at most one default per adapter type.
            if doc.get("default"):
                self._demote_other_defaults(key, doc)
            self._connections[key] = doc
            self._save()
        return Connection(name=key, **doc)

    def rename(self, old: str, new: str) -> Connection:
        key = slug(new)
        with self._lock:
            if old not in self._connections:
                raise KeyError(old)
            doc = self._connections.pop(old)
            self._connections[key] = doc
            self._save()
        return Connection(name=key, **doc)

    def delete(self, name: str) -> None:
        with self._lock:
            if self._connections.pop(name, None) is None:
                raise KeyError(name)
            self._save()

    # -- secrets inventory ---------------------------------------------------

    def secret_refs(self) -> list[dict]:
        """Masked references to every secret-named field across connections.

        Returns ``{owner, field, fingerprint}`` per secret value — never the
        value itself. Used by the Secrets Vault inventory.
        """
        refs: list[dict] = []
        for name in sorted(self._connections):
            for field, value in self._connections[name].items():
                if is_secret_key(str(field)) and isinstance(value, str) and value:
                    refs.append(
                        {"owner": name, "field": field, "fingerprint": fingerprint(value)}
                    )
        return refs
