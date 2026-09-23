"""Registry store — PostgreSQL via asyncpg, the platform's durable tier.

Same pattern as ``scenario-service/api/pg_store.py``: a DSN from the
environment, a connection pool created in the app lifespan, and idempotent
migrations applied at startup. agent-hub owns its own tables in the shared
``payprobe`` database (prefixed ``agent_hub_``), tracked by a numbered
migrations table so later phases (runs, approvals, budgets) add versions
instead of editing DDL in place. There is no file or in-memory fallback: a
missing DSN fails startup loudly (ADR-0010: durable, cross-replica state).

One pair of tables serves both kinds (``agent`` and ``workflow``): a
*definition* row (identity, status, builtin flag) and N *version* rows. The
lifecycle is the provenance discipline ADR-0003 uses for certify, applied to
prompts:

    draft ──publish──▶ active ──(next publish)──▶ superseded
                         │
                       retire ──▶ retired

* A published version is **immutable**; editing it is a conflict — create a
  new draft instead. ``spec_sha256`` is the provenance hash a run record pins.
* Exactly one version of a definition is *active* (a partial unique index
  enforces it); a bare reference (``"triage"``) resolves to it. A pinned
  reference (``"triage@2"``) resolves to that version while it is active or
  superseded, so workflows that pinned it keep working after a newer publish.
  Retired versions resolve to nothing.
* Builtins (the seeds) cannot be retired (guardrail, invariant #6 spirit).

Environment:
    AGENT_HUB_DATABASE_URL   postgres DSN (falls back to DATABASE_URL)
    AGENT_HUB_POOL_MAX       pool size (default 5)
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Literal

import asyncpg

Kind = Literal["agent", "workflow"]

#: (version, DDL). Append-only: never edit a shipped entry, add the next one.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE IF NOT EXISTS agent_hub_definitions (
  kind        TEXT NOT NULL,
  name        TEXT NOT NULL,
  owner       TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'draft',
  builtin     BOOLEAN NOT NULL DEFAULT FALSE,
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (kind, name)
);
CREATE TABLE IF NOT EXISTS agent_hub_versions (
  kind         TEXT NOT NULL,
  name         TEXT NOT NULL,
  version      INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft',
  spec         JSONB NOT NULL,
  spec_sha256  TEXT NOT NULL,
  created_by   TEXT NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_by TEXT,
  published_at TIMESTAMPTZ,
  PRIMARY KEY (kind, name, version),
  FOREIGN KEY (kind, name) REFERENCES agent_hub_definitions (kind, name) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_hub_versions_one_active
  ON agent_hub_versions (kind, name) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS agent_hub_meta (
  key   TEXT PRIMARY KEY,
  value JSONB NOT NULL
);
""",
    ),
    (
        2,
        """
CREATE TABLE IF NOT EXISTS agent_hub_heartbeats (
  id               TEXT PRIMARY KEY,
  agent            TEXT NOT NULL,
  version          INTEGER NOT NULL,
  spec_sha256      TEXT NOT NULL,
  wake             TEXT NOT NULL,
  invoked_by       TEXT NOT NULL DEFAULT '',
  principal        JSONB NOT NULL DEFAULT '{}'::jsonb,
  input            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'running',
  steps            JSONB NOT NULL DEFAULT '[]'::jsonb,
  proposed         JSONB NOT NULL DEFAULT '[]'::jsonb,
  journal          JSONB NOT NULL DEFAULT '[]'::jsonb,
  tokens_in        INTEGER NOT NULL DEFAULT 0,
  tokens_out       INTEGER NOT NULL DEFAULT 0,
  model            TEXT NOT NULL DEFAULT '',
  result           TEXT,
  error            TEXT,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  reverted_at      TIMESTAMPTZ,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_hub_heartbeats_agent_started
  ON agent_hub_heartbeats (agent, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS agent_hub_heartbeats_one_running
  ON agent_hub_heartbeats (agent) WHERE status = 'running';
""",
    ),
    (
        3,
        """
CREATE TABLE IF NOT EXISTS agent_hub_runs (
  id               TEXT PRIMARY KEY,
  workflow         TEXT NOT NULL,
  version          INTEGER NOT NULL,
  spec_sha256      TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'running',
  inputs           JSONB NOT NULL DEFAULT '{}'::jsonb,
  node_states      JSONB NOT NULL DEFAULT '{}'::jsonb,
  results          JSONB NOT NULL DEFAULT '{}'::jsonb,
  invoked_by       TEXT NOT NULL DEFAULT '',
  principal        JSONB NOT NULL DEFAULT '{}'::jsonb,
  error            TEXT,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_hub_runs_workflow_started
  ON agent_hub_runs (workflow, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_hub_runs_active
  ON agent_hub_runs (status) WHERE status IN ('running', 'waiting');
CREATE TABLE IF NOT EXISTS agent_hub_approvals (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES agent_hub_runs (id) ON DELETE CASCADE,
  node_id       TEXT NOT NULL,
  workflow      TEXT NOT NULL,
  roles         JSONB NOT NULL DEFAULT '[]'::jsonb,
  context       JSONB NOT NULL DEFAULT '{}'::jsonb,
  status        TEXT NOT NULL DEFAULT 'pending',
  decided_by    TEXT,
  note          TEXT,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ,
  decided_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_hub_approvals_status
  ON agent_hub_approvals (status, requested_at DESC);
CREATE TABLE IF NOT EXISTS agent_hub_plans (
  id            TEXT PRIMARY KEY,
  run_id        TEXT REFERENCES agent_hub_runs (id) ON DELETE SET NULL,
  node_id       TEXT,
  heartbeat_id  TEXT,
  agent         TEXT NOT NULL,
  version       INTEGER NOT NULL,
  spec_sha256   TEXT NOT NULL,
  proposed      JSONB NOT NULL DEFAULT '[]'::jsonb,
  result        TEXT,
  authored_by   TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_hub_plans_run ON agent_hub_plans (run_id, created_at DESC);
""",
    ),
]


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class Guardrail(RuntimeError):
    """A change the registry refuses regardless of caller intent."""


def spec_hash(spec: dict) -> str:
    canon = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def dsn_from_env() -> str:
    dsn = os.environ.get("AGENT_HUB_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise RuntimeError(
            "agent-hub needs a PostgreSQL DSN in AGENT_HUB_DATABASE_URL "
            "(or DATABASE_URL); there is no file-backed fallback by design (ADR-0010)"
        )
    return dsn


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _loads(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


class RegistryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str | None = None, *, pool_max: int | None = None) -> RegistryStore:
        dsn = dsn or dsn_from_env()
        size = pool_max or int(os.environ.get("AGENT_HUB_POOL_MAX", "5"))
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=max(1, size))
        store = cls(pool)
        await store.migrate()
        return store

    async def close(self) -> None:
        await self._pool.close()

    # -- migrations ------------------------------------------------------------------

    async def migrate(self) -> list[int]:
        """Apply any migration newer than the recorded schema version."""
        applied: list[int] = []
        async with self._pool.acquire() as con:
            await con.execute(
                "CREATE TABLE IF NOT EXISTS agent_hub_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            current = await con.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM agent_hub_migrations"
            )
            for version, ddl in MIGRATIONS:
                if version <= current:
                    continue
                async with con.transaction():
                    await con.execute(ddl)
                    await con.execute(
                        "INSERT INTO agent_hub_migrations (version) VALUES ($1)", version
                    )
                applied.append(version)
        return applied

    async def schema_version(self) -> int:
        return await self._pool.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM agent_hub_migrations"
        )

    async def reset(self) -> None:
        """Test helper: wipe registry data (schema stays)."""
        await self._pool.execute(
            "TRUNCATE agent_hub_approvals, agent_hub_plans, agent_hub_runs, "
            "agent_hub_heartbeats, agent_hub_versions, agent_hub_definitions, agent_hub_meta"
        )

    # -- row shaping -------------------------------------------------------------------

    @staticmethod
    def _def_row(r: asyncpg.Record) -> dict:
        return {
            "kind": r["kind"],
            "name": r["name"],
            "owner": r["owner"],
            "status": r["status"],
            "builtin": bool(r["builtin"]),
            "created_by": r["created_by"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        }

    @staticmethod
    def _ver_row(r: asyncpg.Record, with_spec: bool = True) -> dict:
        d = {
            "name": r["name"],
            "version": r["version"],
            "status": r["status"],
            "spec_sha256": r["spec_sha256"],
            "created_by": r["created_by"],
            "created_at": _iso(r["created_at"]),
            "published_by": r["published_by"],
            "published_at": _iso(r["published_at"]),
        }
        if with_spec:
            d["spec"] = _loads(r["spec"])
        return d

    @staticmethod
    async def _definition(con: asyncpg.Connection, kind: Kind, name: str) -> asyncpg.Record:
        r = await con.fetchrow(
            "SELECT * FROM agent_hub_definitions WHERE kind=$1 AND name=$2", kind, name
        )
        if r is None:
            raise NotFound(f"{kind} '{name}' not found")
        return r

    @staticmethod
    async def _version(
        con: asyncpg.Connection, kind: Kind, name: str, version: int
    ) -> asyncpg.Record:
        r = await con.fetchrow(
            "SELECT * FROM agent_hub_versions WHERE kind=$1 AND name=$2 AND version=$3",
            kind,
            name,
            version,
        )
        if r is None:
            raise NotFound(f"{kind} '{name}' has no version {version}")
        return r

    @staticmethod
    async def _active(con: asyncpg.Connection, kind: Kind, name: str) -> asyncpg.Record | None:
        return await con.fetchrow(
            "SELECT * FROM agent_hub_versions WHERE kind=$1 AND name=$2 AND status='active'",
            kind,
            name,
        )

    @staticmethod
    async def _touch(
        con: asyncpg.Connection, kind: Kind, name: str, status: str | None = None
    ) -> None:
        if status:
            await con.execute(
                "UPDATE agent_hub_definitions SET status=$3, updated_at=NOW() "
                "WHERE kind=$1 AND name=$2",
                kind,
                name,
                status,
            )
        else:
            await con.execute(
                "UPDATE agent_hub_definitions SET updated_at=NOW() WHERE kind=$1 AND name=$2",
                kind,
                name,
            )

    # -- definitions -------------------------------------------------------------------

    async def create(
        self,
        kind: Kind,
        name: str,
        spec: dict,
        by: str = "",
        owner: str = "",
        builtin: bool = False,
    ) -> dict:
        """Create a definition with version 1 as a draft."""
        async with self._pool.acquire() as con, con.transaction():
            try:
                await con.execute(
                    "INSERT INTO agent_hub_definitions (kind, name, owner, status, builtin, "
                    "created_by) VALUES ($1,$2,$3,'draft',$4,$5)",
                    kind,
                    name,
                    owner or by,
                    builtin,
                    by,
                )
            except asyncpg.UniqueViolationError as exc:
                raise Conflict(f"{kind} '{name}' already exists") from exc
            await con.execute(
                "INSERT INTO agent_hub_versions (kind, name, version, status, spec, "
                "spec_sha256, created_by) VALUES ($1,$2,1,'draft',$3::jsonb,$4,$5)",
                kind,
                name,
                json.dumps(spec),
                spec_hash(spec),
                by,
            )
        return await self.get(kind, name)

    async def exists(self, kind: Kind, name: str) -> bool:
        return bool(
            await self._pool.fetchval(
                "SELECT 1 FROM agent_hub_definitions WHERE kind=$1 AND name=$2", kind, name
            )
        )

    async def get(self, kind: Kind, name: str) -> dict:
        async with self._pool.acquire() as con:
            d = self._def_row(await self._definition(con, kind, name))
            vers = await con.fetch(
                "SELECT * FROM agent_hub_versions WHERE kind=$1 AND name=$2 ORDER BY version",
                kind,
                name,
            )
            d["versions"] = [self._ver_row(v, with_spec=False) for v in vers]
            active = await self._active(con, kind, name)
            d["active_version"] = active["version"] if active else None
            d["latest_version"] = vers[-1]["version"] if vers else None
            d["spec"] = _loads(active["spec"]) if active else None
            return d

    async def list(self, kind: Kind, status: str | None = None) -> list[dict]:
        q = """
        SELECT d.*, a.version AS active_version, a.spec AS active_spec,
               (SELECT MAX(version) FROM agent_hub_versions v
                 WHERE v.kind=d.kind AND v.name=d.name) AS latest_version
          FROM agent_hub_definitions d
          LEFT JOIN agent_hub_versions a
            ON a.kind=d.kind AND a.name=d.name AND a.status='active'
         WHERE d.kind=$1 AND ($2::text IS NULL OR d.status=$2)
         ORDER BY d.name"""
        out = []
        for r in await self._pool.fetch(q, kind, status):
            d = self._def_row(r)
            d["active_version"] = r["active_version"]
            d["latest_version"] = r["latest_version"]
            if r["active_spec"] is not None:
                spec = _loads(r["active_spec"])
                d["role"] = spec.get("role") or spec.get("description")
                d["mode"] = spec.get("mode")
            out.append(d)
        return out

    # -- versions ------------------------------------------------------------------------

    async def add_version(self, kind: Kind, name: str, spec: dict, by: str = "") -> dict:
        async with self._pool.acquire() as con, con.transaction():
            d = await self._definition(con, kind, name)
            if d["status"] == "retired":
                raise Conflict(f"{kind} '{name}' is retired; new versions are refused")
            ver = (
                await con.fetchval(
                    "SELECT COALESCE(MAX(version), 0) FROM agent_hub_versions "
                    "WHERE kind=$1 AND name=$2",
                    kind,
                    name,
                )
            ) + 1
            await con.execute(
                "INSERT INTO agent_hub_versions (kind, name, version, status, spec, "
                "spec_sha256, created_by) VALUES ($1,$2,$3,'draft',$4::jsonb,$5,$6)",
                kind,
                name,
                ver,
                json.dumps(spec),
                spec_hash(spec),
                by,
            )
            await self._touch(con, kind, name)
            return self._ver_row(await self._version(con, kind, name, ver))

    async def get_version(self, kind: Kind, name: str, version: int) -> dict:
        async with self._pool.acquire() as con:
            await self._definition(con, kind, name)
            return self._ver_row(await self._version(con, kind, name, version))

    async def update_draft(
        self, kind: Kind, name: str, version: int, spec: dict, by: str = ""
    ) -> dict:
        async with self._pool.acquire() as con, con.transaction():
            await self._definition(con, kind, name)
            v = await self._version(con, kind, name, version)
            if v["status"] != "draft":
                raise Conflict(
                    f"{kind} '{name}' v{version} is {v['status']} and immutable; "
                    "create a new draft version instead"
                )
            await con.execute(
                "UPDATE agent_hub_versions SET spec=$4::jsonb, spec_sha256=$5, created_by=$6 "
                "WHERE kind=$1 AND name=$2 AND version=$3",
                kind,
                name,
                version,
                json.dumps(spec),
                spec_hash(spec),
                by or v["created_by"],
            )
            await self._touch(con, kind, name)
            return self._ver_row(await self._version(con, kind, name, version))

    async def publish(self, kind: Kind, name: str, version: int, by: str = "") -> dict:
        async with self._pool.acquire() as con, con.transaction():
            d = await self._definition(con, kind, name)
            if d["status"] == "retired":
                raise Conflict(f"{kind} '{name}' is retired")
            v = await self._version(con, kind, name, version)
            if v["status"] != "draft":
                raise Conflict(f"{kind} '{name}' v{version} is already {v['status']}")
            await con.execute(
                "UPDATE agent_hub_versions SET status='superseded' "
                "WHERE kind=$1 AND name=$2 AND status='active'",
                kind,
                name,
            )
            await con.execute(
                "UPDATE agent_hub_versions SET status='active', published_by=$4, "
                "published_at=NOW() WHERE kind=$1 AND name=$2 AND version=$3",
                kind,
                name,
                version,
                by,
            )
            await self._touch(con, kind, name, status="active")
            return self._ver_row(await self._version(con, kind, name, version))

    async def retire(self, kind: Kind, name: str, by: str = "") -> dict:
        async with self._pool.acquire() as con, con.transaction():
            d = await self._definition(con, kind, name)
            if d["builtin"]:
                raise Guardrail(f"builtin {kind} '{name}' cannot be retired")
            if d["status"] != "retired":
                await con.execute(
                    "UPDATE agent_hub_versions SET status='retired' WHERE kind=$1 AND name=$2 "
                    "AND status IN ('active','superseded','draft')",
                    kind,
                    name,
                )
                await self._touch(con, kind, name, status="retired")
        return await self.get(kind, name)

    async def resolve(self, kind: Kind, ref: str) -> tuple[str, int, dict] | None:
        """``name`` → the active version; ``name@N`` → that version if it is
        active or superseded (pins survive newer publishes). None otherwise."""
        name, ver = ref, None
        if "@" in ref:
            name, v = ref.split("@", 1)
            if not v.isdigit():
                return None
            ver = int(v)
        async with self._pool.acquire() as con:
            status = await con.fetchval(
                "SELECT status FROM agent_hub_definitions WHERE kind=$1 AND name=$2", kind, name
            )
            if status is None or status == "retired":
                return None
            if ver is None:
                r = await self._active(con, kind, name)
            else:
                r = await con.fetchrow(
                    "SELECT * FROM agent_hub_versions WHERE kind=$1 AND name=$2 AND version=$3 "
                    "AND status IN ('active','superseded')",
                    kind,
                    name,
                    ver,
                )
            if r is None:
                return None
            return name, r["version"], _loads(r["spec"])

    # -- pause flag ----------------------------------------------------------------------

    async def paused(self) -> dict:
        v = await self._pool.fetchval("SELECT value FROM agent_hub_meta WHERE key='paused'")
        return _loads(v) if v is not None else {"paused": False, "by": None, "at": None}

    async def set_paused(self, paused: bool, by: str = "") -> dict:
        row = await self._pool.fetchrow(
            "INSERT INTO agent_hub_meta (key, value) VALUES ('paused', "
            "jsonb_build_object('paused', $1::boolean, 'by', $2::text, 'at', NOW())) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value RETURNING value",
            bool(paused),
            by or None,
        )
        return _loads(row["value"])

    # -- heartbeats (phase 2) ----------------------------------------------------------

    @staticmethod
    def _hb_row(r: asyncpg.Record) -> dict:
        d = dict(r)
        for k in ("principal", "steps", "proposed", "journal"):
            d[k] = _loads(d[k])
        for k in ("started_at", "finished_at", "reverted_at"):
            d[k] = _iso(d[k])
        return d

    async def start_heartbeat(self, hb: dict) -> dict:
        """Insert a ``running`` row. Raises :class:`Conflict` when the agent
        already has one (wakeups coalesce instead of stacking)."""
        try:
            await self._pool.execute(
                "INSERT INTO agent_hub_heartbeats (id, agent, version, spec_sha256, wake, "
                "invoked_by, principal, input, status, model) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,'running',$9)",
                hb["id"],
                hb["agent"],
                hb["version"],
                hb["spec_sha256"],
                hb["wake"],
                hb.get("invoked_by", ""),
                json.dumps(hb.get("principal") or {}),
                hb.get("input", ""),
                hb.get("model", ""),
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict(f"agent '{hb['agent']}' already has a running heartbeat") from exc
        return await self.get_heartbeat(hb["id"])

    async def record_heartbeat(
        self,
        hb_id: str,
        status: str,
        *,
        steps: list,
        proposed: list,
        journal: list,
        tokens: dict,
        result: str | None,
        error: str | None,
    ) -> dict:
        """Persist the outcome of a finished wake (from a running row) or of a
        refused wake (status other than running, inserted directly)."""
        await self._pool.execute(
            "UPDATE agent_hub_heartbeats SET status=$2, steps=$3::jsonb, proposed=$4::jsonb, "
            "journal=$5::jsonb, tokens_in=$6, tokens_out=$7, result=$8, error=$9, "
            "finished_at=NOW() WHERE id=$1",
            hb_id,
            status,
            json.dumps(steps),
            json.dumps(proposed),
            json.dumps(journal),
            int(tokens.get("input", 0)),
            int(tokens.get("output", 0)),
            result,
            error,
        )
        return await self.get_heartbeat(hb_id)

    async def refuse_heartbeat(self, hb: dict, status: str, error: str) -> dict:
        """A wake that never ran (paused, budget): recorded, never 'running'."""
        await self._pool.execute(
            "INSERT INTO agent_hub_heartbeats (id, agent, version, spec_sha256, wake, "
            "invoked_by, principal, input, status, error, finished_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,NOW())",
            hb["id"],
            hb["agent"],
            hb["version"],
            hb["spec_sha256"],
            hb["wake"],
            hb.get("invoked_by", ""),
            json.dumps(hb.get("principal") or {}),
            hb.get("input", ""),
            status,
            error,
        )
        return await self.get_heartbeat(hb["id"])

    async def get_heartbeat(self, hb_id: str) -> dict:
        r = await self._pool.fetchrow("SELECT * FROM agent_hub_heartbeats WHERE id=$1", hb_id)
        if r is None:
            raise NotFound(f"heartbeat '{hb_id}' not found")
        return self._hb_row(r)

    async def list_heartbeats(self, agent: str | None = None, limit: int = 50) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT id, agent, version, spec_sha256, wake, invoked_by, status, "
            "tokens_in, tokens_out, model, error, started_at, finished_at, reverted_at, "
            "jsonb_array_length(steps) AS n_steps, jsonb_array_length(proposed) AS n_proposed, "
            "jsonb_array_length(journal) AS n_writes "
            "FROM agent_hub_heartbeats WHERE ($1::text IS NULL OR agent=$1) "
            "ORDER BY started_at DESC LIMIT $2",
            agent,
            limit,
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("started_at", "finished_at", "reverted_at"):
                d[k] = _iso(d[k])
            out.append(d)
        return out

    async def running_heartbeat(self, agent: str) -> dict | None:
        r = await self._pool.fetchrow(
            "SELECT * FROM agent_hub_heartbeats WHERE agent=$1 AND status='running'", agent
        )
        return self._hb_row(r) if r else None

    async def request_cancel(self, hb_id: str) -> dict:
        n = await self._pool.execute(
            "UPDATE agent_hub_heartbeats SET cancel_requested=TRUE "
            "WHERE id=$1 AND status='running'",
            hb_id,
        )
        if n == "UPDATE 0":
            hb = await self.get_heartbeat(hb_id)  # raises NotFound
            raise Conflict(f"heartbeat '{hb_id}' is {hb['status']}, not running")
        return await self.get_heartbeat(hb_id)

    async def cancel_requested(self, hb_id: str) -> bool:
        return bool(
            await self._pool.fetchval(
                "SELECT cancel_requested FROM agent_hub_heartbeats WHERE id=$1", hb_id
            )
        )

    async def mark_reverted(self, hb_id: str) -> dict:
        await self._pool.execute(
            "UPDATE agent_hub_heartbeats SET reverted_at=NOW(), journal='[]'::jsonb WHERE id=$1",
            hb_id,
        )
        return await self.get_heartbeat(hb_id)

    async def tokens_today(self, agent: str) -> int:
        return int(
            await self._pool.fetchval(
                "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM agent_hub_heartbeats "
                "WHERE agent=$1 AND started_at >= date_trunc('day', NOW())",
                agent,
            )
        )

    async def reconcile_running(self, grace_s: int = 60) -> list[dict]:
        """Restart watchdog. A row still ``running`` past its version's
        ``wall_clock_s`` (+ grace) belongs to a process that is gone: no runner
        will ever finish it, and while it stands the agent's wakes coalesce onto
        it forever. Mark such rows ``failed`` and return them (for alerts)."""
        rows = await self._pool.fetch(
            "UPDATE agent_hub_heartbeats h SET status='failed', "
            "error='orphaned by restart', finished_at=NOW() "
            "FROM agent_hub_versions v "
            "WHERE h.status='running' AND v.kind='agent' AND v.name=h.agent "
            "AND v.version=h.version "
            "AND h.started_at + make_interval(secs => "
            "COALESCE((v.spec->'limits'->>'wall_clock_s')::int, 300) + $1) < NOW() "
            "RETURNING h.*",
            int(grace_s),
        )
        return [self._hb_row(r) for r in rows]

    # -- workflow runs (phase 3) -------------------------------------------------------

    @staticmethod
    def _run_row(r: asyncpg.Record) -> dict:
        d = dict(r)
        for k in ("inputs", "node_states", "results", "principal"):
            d[k] = _loads(d[k])
        for k in ("started_at", "updated_at", "finished_at"):
            d[k] = _iso(d[k])
        return d

    async def create_run(self, run: dict) -> dict:
        await self._pool.execute(
            "INSERT INTO agent_hub_runs (id, workflow, version, spec_sha256, status, inputs, "
            "node_states, results, invoked_by, principal) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10::jsonb)",
            run["id"],
            run["workflow"],
            run["version"],
            run["spec_sha256"],
            run.get("status", "running"),
            json.dumps(run.get("inputs") or {}),
            json.dumps(run.get("node_states") or {}),
            json.dumps(run.get("results") or {}),
            run.get("invoked_by", ""),
            json.dumps(run.get("principal") or {}),
        )
        return await self.get_run(run["id"])

    async def get_run(self, run_id: str) -> dict:
        r = await self._pool.fetchrow("SELECT * FROM agent_hub_runs WHERE id=$1", run_id)
        if r is None:
            raise NotFound(f"run '{run_id}' not found")
        return self._run_row(r)

    async def list_runs(
        self, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT id, workflow, version, spec_sha256, status, invoked_by, error, "
            "cancel_requested, started_at, updated_at, finished_at, "
            "(SELECT COUNT(*) FROM jsonb_each(node_states) e "
            " WHERE e.value->>'status' = 'done') AS n_done, "
            "(SELECT COUNT(*) FROM jsonb_object_keys(node_states)) AS n_nodes "
            "FROM agent_hub_runs "
            "WHERE ($1::text IS NULL OR workflow=$1) AND ($2::text IS NULL OR status=$2) "
            "ORDER BY started_at DESC LIMIT $3",
            workflow,
            status,
            limit,
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("started_at", "updated_at", "finished_at"):
                d[k] = _iso(d[k])
            out.append(d)
        return out

    async def active_runs(self) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM agent_hub_runs WHERE status IN ('running', 'waiting') "
            "ORDER BY started_at"
        )
        return [self._run_row(r) for r in rows]

    async def save_run(
        self,
        run_id: str,
        *,
        status: str,
        node_states: dict,
        results: dict,
        error: str | None = None,
    ) -> dict:
        """Persist one engine step. Terminal statuses stamp ``finished_at``."""
        terminal = status in ("done", "failed", "cancelled", "rejected")
        await self._pool.execute(
            "UPDATE agent_hub_runs SET status=$2, node_states=$3::jsonb, results=$4::jsonb, "
            "error=$5, updated_at=NOW(), "
            "finished_at = CASE WHEN $6 THEN COALESCE(finished_at, NOW()) ELSE finished_at END "
            "WHERE id=$1",
            run_id,
            status,
            json.dumps(node_states, default=str),
            json.dumps(results, default=str),
            error,
            terminal,
        )
        return await self.get_run(run_id)

    async def request_run_cancel(self, run_id: str) -> dict:
        n = await self._pool.execute(
            "UPDATE agent_hub_runs SET cancel_requested=TRUE, updated_at=NOW() "
            "WHERE id=$1 AND status IN ('running', 'waiting')",
            run_id,
        )
        if n == "UPDATE 0":
            run = await self.get_run(run_id)  # raises NotFound
            raise Conflict(f"run '{run_id}' is {run['status']}, not active")
        return await self.get_run(run_id)

    # -- approvals ---------------------------------------------------------------------

    @staticmethod
    def _approval_row(r: asyncpg.Record) -> dict:
        d = dict(r)
        for k in ("roles", "context"):
            d[k] = _loads(d[k])
        for k in ("requested_at", "expires_at", "decided_at"):
            d[k] = _iso(d[k])
        return d

    async def create_approval(self, ap: dict) -> dict:
        await self._pool.execute(
            "INSERT INTO agent_hub_approvals (id, run_id, node_id, workflow, roles, context, "
            "expires_at) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb, "
            "CASE WHEN $7::int IS NULL THEN NULL ELSE NOW() + make_interval(secs => $7) END)",
            ap["id"],
            ap["run_id"],
            ap["node_id"],
            ap["workflow"],
            json.dumps(ap.get("roles") or []),
            json.dumps(ap.get("context") or {}, default=str),
            ap.get("timeout_s"),
        )
        return await self.get_approval(ap["id"])

    async def get_approval(self, ap_id: str) -> dict:
        r = await self._pool.fetchrow("SELECT * FROM agent_hub_approvals WHERE id=$1", ap_id)
        if r is None:
            raise NotFound(f"approval '{ap_id}' not found")
        return self._approval_row(r)

    async def list_approvals(
        self, status: str | None = "pending", run_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM agent_hub_approvals "
            "WHERE ($1::text IS NULL OR status=$1) AND ($2::text IS NULL OR run_id=$2) "
            "ORDER BY requested_at DESC LIMIT $3",
            status,
            run_id,
            limit,
        )
        return [self._approval_row(r) for r in rows]

    async def decide_approval(self, ap_id: str, decision: str, by: str, note: str = "") -> dict:
        """``approved`` / ``rejected``; only a pending approval can be decided."""
        n = await self._pool.execute(
            "UPDATE agent_hub_approvals SET status=$2, decided_by=$3, note=$4, decided_at=NOW() "
            "WHERE id=$1 AND status='pending'",
            ap_id,
            decision,
            by,
            note or None,
        )
        if n == "UPDATE 0":
            ap = await self.get_approval(ap_id)  # raises NotFound
            raise Conflict(f"approval '{ap_id}' is {ap['status']}, not pending")
        return await self.get_approval(ap_id)

    async def expire_approvals(self) -> list[dict]:
        rows = await self._pool.fetch(
            "UPDATE agent_hub_approvals SET status='expired', decided_at=NOW() "
            "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at < NOW() "
            "RETURNING *"
        )
        return [self._approval_row(r) for r in rows]

    async def close_pending_approvals(self, run_id: str, status: str) -> int:
        n = await self._pool.execute(
            "UPDATE agent_hub_approvals SET status=$2, decided_at=NOW() "
            "WHERE run_id=$1 AND status='pending'",
            run_id,
            status,
        )
        return int(n.split()[-1]) if n else 0

    # -- plans -------------------------------------------------------------------------

    @staticmethod
    def _plan_row(r: asyncpg.Record) -> dict:
        d = dict(r)
        d["proposed"] = _loads(d["proposed"])
        d["created_at"] = _iso(d["created_at"])
        return d

    async def create_plan(self, plan: dict) -> dict:
        await self._pool.execute(
            "INSERT INTO agent_hub_plans (id, run_id, node_id, heartbeat_id, agent, version, "
            "spec_sha256, proposed, result, authored_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)",
            plan["id"],
            plan.get("run_id"),
            plan.get("node_id"),
            plan.get("heartbeat_id"),
            plan["agent"],
            plan["version"],
            plan["spec_sha256"],
            json.dumps(plan.get("proposed") or [], default=str),
            plan.get("result"),
            plan.get("authored_by", ""),
        )
        return await self.get_plan(plan["id"])

    async def get_plan(self, plan_id: str) -> dict:
        r = await self._pool.fetchrow("SELECT * FROM agent_hub_plans WHERE id=$1", plan_id)
        if r is None:
            raise NotFound(f"plan '{plan_id}' not found")
        return self._plan_row(r)

    async def list_plans(self, run_id: str | None = None, limit: int = 50) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM agent_hub_plans WHERE ($1::text IS NULL OR run_id=$1) "
            "ORDER BY created_at DESC LIMIT $2",
            run_id,
            limit,
        )
        return [self._plan_row(r) for r in rows]
