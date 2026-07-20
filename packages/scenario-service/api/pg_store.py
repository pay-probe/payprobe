"""PostgreSQL scenario store (asyncpg).

Uses the shared ``scenarios`` table from infra/postgres/init.sql — the same
table the orchestrator's ``runs.scenario_ids`` reference — plus the
``scenario_versions`` history table. Deletes are soft (``is_active = FALSE``)
so historical runs keep their foreign keys.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg

from models import (
    Project,
    ProjectDraft,
    Scenario,
    ScenarioDraft,
    ScenarioSet,
    ScenarioSetDraft,
    ScenarioSummary,
    ScenarioVersionInfo,
)

from .store import (
    DEFAULT_PROJECT_ID,
    ForeignScenario,
    ProjectNotEmpty,
    ProjectNotFound,
    ScenarioNotFound,
    SetNotFound,
    VersionConflict,
)

# Applied at startup so the service also works against a database created
# before scenario versioning existed (idempotent).
_MIGRATION = """
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS current_version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS scenario_versions (
    scenario_id UUID NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    document    JSONB NOT NULL,
    comment     TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scenario_id, version)
);
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS color TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS icon TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS scenario_sets (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS set_members (
    set_id      TEXT NOT NULL REFERENCES scenario_sets(id) ON DELETE CASCADE,
    scenario_id UUID NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    PRIMARY KEY (set_id, scenario_id)
);
INSERT INTO projects (id, name, description)
    VALUES ('prj-default', 'Default', 'Auto-created project')
    ON CONFLICT (id) DO NOTHING;
UPDATE scenarios SET project_id = 'prj-default' WHERE project_id IS NULL;
"""

class PostgresScenarioStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresScenarioStore":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        async with pool.acquire() as conn:
            await conn.execute(_MIGRATION)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _scenario(scenario_row, version_row) -> Scenario:
        doc = json.loads(version_row["document"])
        return Scenario(
            id=str(scenario_row["id"]),
            project_id=scenario_row["project_id"] or DEFAULT_PROJECT_ID,
            version=version_row["version"],
            created_at=scenario_row["created_at"],
            updated_at=version_row["created_at"],
            **doc,
        )

    async def _scenario_row(self, conn, scenario_id: str):
        try:
            row = await conn.fetchrow(
                "SELECT * FROM scenarios WHERE id = $1::uuid AND is_active", scenario_id
            )
        except (asyncpg.exceptions.DataError, ValueError):
            raise ScenarioNotFound(scenario_id)
        if row is None:
            raise ScenarioNotFound(scenario_id)
        return row

    # -- API ---------------------------------------------------------------

    async def list(self, project_id: str | None = None) -> list[ScenarioSummary]:
        sql = """
            SELECT s.id, s.project_id, s.current_version,
                   v.document, v.created_at AS v_created
            FROM scenarios s
            JOIN scenario_versions v
              ON v.scenario_id = s.id AND v.version = s.current_version
            WHERE s.is_active
            """
        args: list = []
        if project_id is not None:
            sql += " AND s.project_id = $1"
            args.append(project_id)
        sql += " ORDER BY v.created_at DESC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        out = []
        for row in rows:
            doc = json.loads(row["document"])
            out.append(
                ScenarioSummary(
                    id=str(row["id"]),
                    project_id=row["project_id"] or DEFAULT_PROJECT_ID,
                    name=doc["name"],
                    description=doc.get("description", ""),
                    category=doc.get("category", ""),
                    color=doc.get("color", ""),
                    icon=doc.get("icon", ""),
                    tags=doc.get("tags", []),
                    test_class=doc.get("test_class", "e2e"),
                    step_count=len(doc.get("steps", [])),
                    version=row["current_version"],
                    updated_at=row["v_created"],
                )
            )
        return out

    async def get(self, scenario_id: str, version: int | None = None) -> Scenario:
        async with self._pool.acquire() as conn:
            row = await self._scenario_row(conn, scenario_id)
            v_row = await conn.fetchrow(
                "SELECT * FROM scenario_versions WHERE scenario_id = $1::uuid AND version = $2",
                scenario_id,
                version or row["current_version"],
            )
            if v_row is None:
                raise ScenarioNotFound(scenario_id)
        return self._scenario(row, v_row)

    async def create(
        self,
        draft: ScenarioDraft,
        scenario_id: str | None = None,
        comment: str = "",
        project_id: str | None = None,
    ) -> Scenario:
        pid = project_id or DEFAULT_PROJECT_ID
        async with self._pool.acquire() as conn, conn.transaction():
            if await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", pid) is None:
                raise ProjectNotFound(pid)
            row = await conn.fetchrow(
                """
                INSERT INTO scenarios
                    (name, description, tags, definition, current_version, project_id)
                VALUES ($1, $2, $3, $4::jsonb, 1, $5)
                RETURNING id
                """,
                draft.name,
                draft.description,
                draft.tags,
                draft.model_dump_json(),
                pid,
            )
            await conn.execute(
                "INSERT INTO scenario_versions (scenario_id, version, document, comment)"
                " VALUES ($1, 1, $2::jsonb, $3)",
                row["id"],
                draft.model_dump_json(),
                comment or "initial version",
            )
            sid = str(row["id"])
        return await self.get(sid)

    async def move_scenario(self, scenario_id: str, project_id: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await self._scenario_row(conn, scenario_id)
            if (
                await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", project_id)
                is None
            ):
                raise ProjectNotFound(project_id)
            await conn.execute(
                "UPDATE scenarios SET project_id = $2, updated_at = NOW()"
                " WHERE id = $1::uuid",
                scenario_id,
                project_id,
            )
            await conn.execute(
                "DELETE FROM set_members WHERE scenario_id = $1::uuid", scenario_id
            )

    async def update(
        self,
        scenario_id: str,
        draft: ScenarioDraft,
        comment: str = "",
        base_version: int | None = None,
    ) -> Scenario:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT current_version FROM scenarios WHERE id = $1::uuid AND is_active"
                " FOR UPDATE",
                scenario_id,
            )
            if row is None:
                raise ScenarioNotFound(scenario_id)
            current = row["current_version"]
            if base_version is not None and base_version != current:
                raise VersionConflict(current)
            next_version = current + 1
            await conn.execute(
                "INSERT INTO scenario_versions (scenario_id, version, document, comment)"
                " VALUES ($1::uuid, $2, $3::jsonb, $4)",
                scenario_id,
                next_version,
                draft.model_dump_json(),
                comment,
            )
            await conn.execute(
                """
                UPDATE scenarios
                SET current_version = $2, name = $3, description = $4, tags = $5,
                    definition = $6::jsonb, updated_at = NOW()
                WHERE id = $1::uuid
                """,
                scenario_id,
                next_version,
                draft.name,
                draft.description,
                draft.tags,
                draft.model_dump_json(),
            )
        return await self.get(scenario_id)

    async def delete(self, scenario_id: str) -> None:
        """Soft delete — runs referencing this scenario keep their FK."""
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                "UPDATE scenarios SET is_active = FALSE, updated_at = NOW()"
                " WHERE id = $1::uuid AND is_active",
                scenario_id,
            )
            await conn.execute(
                "DELETE FROM set_members WHERE scenario_id = $1::uuid", scenario_id
            )
        if result == "UPDATE 0":
            raise ScenarioNotFound(scenario_id)

    async def versions(self, scenario_id: str) -> list[ScenarioVersionInfo]:
        async with self._pool.acquire() as conn:
            await self._scenario_row(conn, scenario_id)
            rows = await conn.fetch(
                "SELECT version, comment, created_at, document FROM scenario_versions"
                " WHERE scenario_id = $1::uuid ORDER BY version DESC",
                scenario_id,
            )
        return [
            ScenarioVersionInfo(
                version=r["version"],
                comment=r["comment"],
                created_at=r["created_at"],
                step_count=len(json.loads(r["document"]).get("steps", [])),
            )
            for r in rows
        ]

    async def restore(self, scenario_id: str, version: int) -> Scenario:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT document FROM scenario_versions WHERE scenario_id = $1::uuid"
                " AND version = $2",
                scenario_id,
                version,
            )
        if row is None:
            raise ScenarioNotFound(scenario_id)
        draft = ScenarioDraft(**json.loads(row["document"]))
        return await self.update(scenario_id, draft, comment=f"restored from v{version}")

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM scenarios WHERE is_active")

    async def seed_from_dir(self, directory: Path) -> int:
        imported = 0
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text())
            data.pop("id", None)
            await self.create(ScenarioDraft(**data), comment=f"seeded from {path.name}")
            imported += 1
        return imported

    # -- projects ------------------------------------------------------------

    async def _project_model(self, conn, row) -> Project:
        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM scenarios
                WHERE project_id = $1 AND is_active) AS scenarios,
              (SELECT COUNT(*) FROM scenario_sets WHERE project_id = $1) AS sets
            """,
            row["id"],
        )
        return Project(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            category=row["category"] if "category" in row.keys() else "",
            color=row["color"] if "color" in row.keys() else "",
            icon=row["icon"] if "icon" in row.keys() else "",
            scenario_count=counts["scenarios"],
            set_count=counts["sets"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _project_row(self, conn, project_id: str):
        row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
        if row is None:
            raise ProjectNotFound(project_id)
        return row

    async def list_projects(self) -> list[Project]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM projects ORDER BY name")
            return [await self._project_model(conn, r) for r in rows]

    async def get_project(self, project_id: str) -> Project:
        async with self._pool.acquire() as conn:
            return await self._project_model(
                conn, await self._project_row(conn, project_id)
            )

    async def create_project(self, draft: ProjectDraft) -> Project:

        pid = f"prj-{uuid.uuid4().hex[:8]}"
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO projects (id, name, description, category, color, icon)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                pid,
                draft.name,
                draft.description,
                draft.category,
                draft.color,
                draft.icon,
            )
        return await self.get_project(pid)

    async def update_project(self, project_id: str, draft: ProjectDraft) -> Project:
        async with self._pool.acquire() as conn:
            await self._project_row(conn, project_id)
            await conn.execute(
                "UPDATE projects SET name = $2, description = $3, category = $4,"
                " color = $5, icon = $6, updated_at = NOW() WHERE id = $1",
                project_id,
                draft.name,
                draft.description,
                draft.category,
                draft.color,
                draft.icon,
            )
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await self._project_row(conn, project_id)
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM scenarios WHERE project_id = $1 AND is_active",
                project_id,
            )
            if active:
                raise ProjectNotEmpty(project_id)
            await conn.execute("DELETE FROM projects WHERE id = $1", project_id)

    # -- named sets ------------------------------------------------------------

    async def _set_model(self, conn, row) -> ScenarioSet:
        ids = await conn.fetch(
            """
            SELECT m.scenario_id FROM set_members m
            JOIN scenarios s ON s.id = m.scenario_id AND s.is_active
            WHERE m.set_id = $1
            ORDER BY m.scenario_id
            """,
            row["id"],
        )
        return ScenarioSet(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            scenario_ids=[str(r["scenario_id"]) for r in ids],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _set_row(self, conn, set_id: str):
        row = await conn.fetchrow("SELECT * FROM scenario_sets WHERE id = $1", set_id)
        if row is None:
            raise SetNotFound(set_id)
        return row

    async def _check_members(self, conn, project_id: str, scenario_ids: list[str]):
        for sid in scenario_ids:
            try:
                row = await conn.fetchrow(
                    "SELECT project_id FROM scenarios WHERE id = $1::uuid AND is_active",
                    sid,
                )
            except (asyncpg.exceptions.DataError, ValueError):
                raise ScenarioNotFound(sid)
            if row is None:
                raise ScenarioNotFound(sid)
            if (row["project_id"] or DEFAULT_PROJECT_ID) != project_id:
                raise ForeignScenario(sid)

    async def list_sets(self, project_id: str | None = None) -> list[ScenarioSet]:
        async with self._pool.acquire() as conn:
            if project_id is not None:
                await self._project_row(conn, project_id)
                rows = await conn.fetch(
                    "SELECT * FROM scenario_sets WHERE project_id = $1 ORDER BY name",
                    project_id,
                )
            else:
                rows = await conn.fetch("SELECT * FROM scenario_sets ORDER BY name")
            return [await self._set_model(conn, r) for r in rows]

    async def get_set(self, set_id: str) -> ScenarioSet:
        async with self._pool.acquire() as conn:
            return await self._set_model(conn, await self._set_row(conn, set_id))

    async def create_set(self, project_id: str, draft: ScenarioSetDraft) -> ScenarioSet:

        set_id = f"set-{uuid.uuid4().hex[:8]}"
        unique_ids = list(dict.fromkeys(draft.scenario_ids))
        async with self._pool.acquire() as conn, conn.transaction():
            await self._project_row(conn, project_id)
            await self._check_members(conn, project_id, unique_ids)
            await conn.execute(
                "INSERT INTO scenario_sets (id, project_id, name, description)"
                " VALUES ($1, $2, $3, $4)",
                set_id,
                project_id,
                draft.name,
                draft.description,
            )
            await conn.executemany(
                "INSERT INTO set_members (set_id, scenario_id) VALUES ($1, $2::uuid)",
                [(set_id, sid) for sid in unique_ids],
            )
        return await self.get_set(set_id)

    async def update_set(self, set_id: str, draft: ScenarioSetDraft) -> ScenarioSet:
        unique_ids = list(dict.fromkeys(draft.scenario_ids))
        async with self._pool.acquire() as conn, conn.transaction():
            row = await self._set_row(conn, set_id)
            await self._check_members(conn, row["project_id"], unique_ids)
            await conn.execute(
                "UPDATE scenario_sets SET name = $2, description = $3,"
                " updated_at = NOW() WHERE id = $1",
                set_id,
                draft.name,
                draft.description,
            )
            await conn.execute("DELETE FROM set_members WHERE set_id = $1", set_id)
            await conn.executemany(
                "INSERT INTO set_members (set_id, scenario_id) VALUES ($1, $2::uuid)",
                [(set_id, sid) for sid in unique_ids],
            )
        return await self.get_set(set_id)

    async def delete_set(self, set_id: str) -> None:
        async with self._pool.acquire() as conn:
            await self._set_row(conn, set_id)
            await conn.execute("DELETE FROM scenario_sets WHERE id = $1", set_id)

    async def add_to_set(self, set_id: str, scenario_id: str) -> ScenarioSet:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await self._set_row(conn, set_id)
            await self._check_members(conn, row["project_id"], [scenario_id])
            await conn.execute(
                "INSERT INTO set_members (set_id, scenario_id) VALUES ($1, $2::uuid)"
                " ON CONFLICT DO NOTHING",
                set_id,
                scenario_id,
            )
            await conn.execute(
                "UPDATE scenario_sets SET updated_at = NOW() WHERE id = $1", set_id
            )
        return await self.get_set(set_id)

    async def remove_from_set(self, set_id: str, scenario_id: str) -> ScenarioSet:
        async with self._pool.acquire() as conn, conn.transaction():
            await self._set_row(conn, set_id)
            await conn.execute(
                "DELETE FROM set_members WHERE set_id = $1 AND scenario_id = $2::uuid",
                set_id,
                scenario_id,
            )
            await conn.execute(
                "UPDATE scenario_sets SET updated_at = NOW() WHERE id = $1", set_id
            )
        return await self.get_set(set_id)
