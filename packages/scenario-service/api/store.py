"""Scenario stores with full version history.

Two backends behind the same async interface:

- :class:`SQLiteScenarioStore` — zero-config local development and tests
  (``SCENARIO_DB`` path or ``:memory:``).
- :class:`PostgresScenarioStore` (api/pg_store.py) — production, selected
  automatically when ``DATABASE_URL`` points at PostgreSQL.

Every save creates an immutable version row; the scenarios table tracks the
current version. The SQLite calls run inline on the event loop — fine for the
~30-user portal this serves; the Postgres store is fully async via asyncpg.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

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

#: Every scenario belongs to a project; this one is created automatically.
DEFAULT_PROJECT_ID = "prj-default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS scenario_versions (
    scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    document TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, version)
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_sets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS set_members (
    set_id TEXT NOT NULL REFERENCES scenario_sets(id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    PRIMARY KEY (set_id, scenario_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScenarioNotFound(KeyError):
    pass


class ProjectNotFound(KeyError):
    pass


class SetNotFound(KeyError):
    pass


class ProjectNotEmpty(Exception):
    """Raised when deleting a project that still contains scenarios."""


class ForeignScenario(Exception):
    """Raised when a set references a scenario from another project."""


class VersionConflict(Exception):
    """Raised when an update's base_version no longer matches the stored one."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"scenario is now at v{current_version}")


class SQLiteScenarioStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent upgrades for databases created by older service versions.

        ``CREATE TABLE IF NOT EXISTS`` keeps whatever shape an existing
        ``scenarios`` table has, so every column added since the first release
        must be back-filled here.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(scenarios)")}
        missing = {
            "current_version": "INTEGER NOT NULL DEFAULT 1",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "project_id": "TEXT",
        }
        now = _now()
        # Presentation columns added to ``projects`` after first release.
        proj_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(projects)")}
        proj_missing = {
            "category": "TEXT NOT NULL DEFAULT ''",
            "color": "TEXT NOT NULL DEFAULT ''",
            "icon": "TEXT NOT NULL DEFAULT ''",
        }
        with self._conn:
            for col, ddl in missing.items():
                if col not in cols:
                    self._conn.execute(
                        f"ALTER TABLE scenarios ADD COLUMN {col} {ddl}"
                    )
            for col, ddl in proj_missing.items():
                if col not in proj_cols:
                    self._conn.execute(
                        f"ALTER TABLE projects ADD COLUMN {col} {ddl}"
                    )
            # Pre-versioning databases stored the document on the scenario row
            # ("definition"); copy it into the history table as version 1.
            if "definition" in cols:
                orphans = self._conn.execute(
                    """
                    SELECT s.id, s.definition, s.created_at FROM scenarios s
                    LEFT JOIN scenario_versions v ON v.scenario_id = s.id
                    WHERE v.scenario_id IS NULL AND s.definition IS NOT NULL
                    """
                ).fetchall()
                self._conn.executemany(
                    "INSERT INTO scenario_versions (scenario_id, version, document,"
                    " comment, created_at) VALUES (?, 1, ?, 'migrated', ?)",
                    [(r["id"], r["definition"], r["created_at"] or now) for r in orphans],
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO projects (id, name, description, created_at,"
                " updated_at) VALUES (?, 'Default', 'Auto-created project', ?, ?)",
                (DEFAULT_PROJECT_ID, now, now),
            )
            self._conn.execute(
                "UPDATE scenarios SET project_id = ? WHERE project_id IS NULL",
                (DEFAULT_PROJECT_ID,),
            )

    async def close(self) -> None:
        self._conn.close()

    # -- helpers -----------------------------------------------------------

    def _row_to_scenario(self, row: sqlite3.Row, doc_row: sqlite3.Row) -> Scenario:
        doc = json.loads(doc_row["document"])
        return Scenario(
            id=row["id"],
            project_id=row["project_id"] or DEFAULT_PROJECT_ID,
            version=doc_row["version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(doc_row["created_at"]),
            **doc,
        )

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return row

    def _set_row(self, set_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scenario_sets WHERE id = ?", (set_id,)
        ).fetchone()
        if row is None:
            raise SetNotFound(set_id)
        return row

    def _version_row(self, scenario_id: str, version: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scenario_versions WHERE scenario_id = ? AND version = ?",
            (scenario_id, version),
        ).fetchone()
        if row is None:
            raise ScenarioNotFound(scenario_id)
        return row

    def _scenario_row(self, scenario_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scenarios WHERE id = ? AND is_active = 1", (scenario_id,)
        ).fetchone()
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
            WHERE s.is_active = 1
            """
        params: tuple = ()
        if project_id is not None:
            sql += " AND s.project_id = ?"
            params = (project_id,)
        sql += " ORDER BY v.created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            doc = json.loads(row["document"])
            out.append(
                ScenarioSummary(
                    id=row["id"],
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
                    updated_at=datetime.fromisoformat(row["v_created"]),
                )
            )
        return out

    async def get(self, scenario_id: str, version: int | None = None) -> Scenario:
        row = self._scenario_row(scenario_id)
        doc_row = self._version_row(scenario_id, version or row["current_version"])
        return self._row_to_scenario(row, doc_row)

    async def create(
        self,
        draft: ScenarioDraft,
        scenario_id: str | None = None,
        comment: str = "",
        project_id: str | None = None,
    ) -> Scenario:
        pid = project_id or DEFAULT_PROJECT_ID
        self._project_row(pid)
        sid = scenario_id or f"scn-{uuid.uuid4().hex[:8]}"
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO scenarios (id, project_id, current_version, created_at,"
                " updated_at) VALUES (?, ?, 1, ?, ?)",
                (sid, pid, now, now),
            )
            self._conn.execute(
                "INSERT INTO scenario_versions (scenario_id, version, document, comment,"
                " created_at) VALUES (?, 1, ?, ?, ?)",
                (sid, draft.model_dump_json(), comment or "initial version", now),
            )
        return await self.get(sid)

    async def move_scenario(self, scenario_id: str, project_id: str) -> None:
        """Move a scenario to another project, dropping old set memberships."""
        self._scenario_row(scenario_id)
        self._project_row(project_id)
        with self._conn:
            self._conn.execute(
                "UPDATE scenarios SET project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, _now(), scenario_id),
            )
            self._conn.execute(
                "DELETE FROM set_members WHERE scenario_id = ?", (scenario_id,)
            )

    async def update(
        self,
        scenario_id: str,
        draft: ScenarioDraft,
        comment: str = "",
        base_version: int | None = None,
    ) -> Scenario:
        row = self._scenario_row(scenario_id)
        current = row["current_version"]
        if base_version is not None and base_version != current:
            raise VersionConflict(current)
        next_version = current + 1
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO scenario_versions (scenario_id, version, document, comment,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (scenario_id, next_version, draft.model_dump_json(), comment, now),
            )
            self._conn.execute(
                "UPDATE scenarios SET current_version = ?, updated_at = ? WHERE id = ?",
                (next_version, now, scenario_id),
            )
        return await self.get(scenario_id)

    async def delete(self, scenario_id: str) -> None:
        """Soft delete — versions are retained, scenario disappears from lists."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE scenarios SET is_active = 0, updated_at = ?"
                " WHERE id = ? AND is_active = 1",
                (_now(), scenario_id),
            )
            self._conn.execute(
                "DELETE FROM set_members WHERE scenario_id = ?", (scenario_id,)
            )
        if cur.rowcount == 0:
            raise ScenarioNotFound(scenario_id)

    async def versions(self, scenario_id: str) -> list[ScenarioVersionInfo]:
        self._scenario_row(scenario_id)
        rows = self._conn.execute(
            "SELECT version, comment, created_at, document FROM scenario_versions"
            " WHERE scenario_id = ? ORDER BY version DESC",
            (scenario_id,),
        ).fetchall()
        return [
            ScenarioVersionInfo(
                version=r["version"],
                comment=r["comment"],
                created_at=datetime.fromisoformat(r["created_at"]),
                step_count=len(json.loads(r["document"]).get("steps", [])),
            )
            for r in rows
        ]

    async def restore(self, scenario_id: str, version: int) -> Scenario:
        """Restore an old version by copying it forward as a new version."""
        doc_row = self._version_row(scenario_id, version)
        draft = ScenarioDraft(**json.loads(doc_row["document"]))
        return await self.update(scenario_id, draft, comment=f"restored from v{version}")

    async def count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM scenarios WHERE is_active = 1"
        ).fetchone()[0]

    async def seed_from_dir(self, directory: Path) -> int:
        """Load example scenario JSON files. Returns number imported."""
        imported = 0
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text())
            data.pop("id", None)
            draft = ScenarioDraft(**data)
            await self.create(draft, comment=f"seeded from {path.name}")
            imported += 1
        return imported

    # -- projects ------------------------------------------------------------

    def _project_model(self, row: sqlite3.Row) -> Project:
        counts = self._conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM scenarios
                WHERE project_id = :id AND is_active = 1) AS scenarios,
              (SELECT COUNT(*) FROM scenario_sets WHERE project_id = :id) AS sets
            """,
            {"id": row["id"]},
        ).fetchone()
        return Project(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            category=row["category"] if "category" in row.keys() else "",
            color=row["color"] if "color" in row.keys() else "",
            icon=row["icon"] if "icon" in row.keys() else "",
            scenario_count=counts["scenarios"],
            set_count=counts["sets"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [self._project_model(r) for r in rows]

    async def get_project(self, project_id: str) -> Project:
        return self._project_model(self._project_row(project_id))

    async def create_project(self, draft: ProjectDraft) -> Project:
        pid = f"prj-{uuid.uuid4().hex[:8]}"
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO projects (id, name, description, category, color, icon,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, draft.name, draft.description, draft.category, draft.color,
                 draft.icon, now, now),
            )
        return await self.get_project(pid)

    async def update_project(self, project_id: str, draft: ProjectDraft) -> Project:
        self._project_row(project_id)
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET name = ?, description = ?, category = ?,"
                " color = ?, icon = ?, updated_at = ? WHERE id = ?",
                (draft.name, draft.description, draft.category, draft.color,
                 draft.icon, _now(), project_id),
            )
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and its sets. Refuses if it still holds scenarios."""
        self._project_row(project_id)
        active = self._conn.execute(
            "SELECT COUNT(*) FROM scenarios WHERE project_id = ? AND is_active = 1",
            (project_id,),
        ).fetchone()[0]
        if active:
            raise ProjectNotEmpty(project_id)
        with self._conn:
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # -- named sets ------------------------------------------------------------

    def _set_model(self, row: sqlite3.Row) -> ScenarioSet:
        ids = [
            r["scenario_id"]
            for r in self._conn.execute(
                """
                SELECT m.scenario_id FROM set_members m
                JOIN scenarios s ON s.id = m.scenario_id AND s.is_active = 1
                WHERE m.set_id = ?
                ORDER BY m.scenario_id
                """,
                (row["id"],),
            )
        ]
        return ScenarioSet(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            scenario_ids=ids,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _check_members(self, project_id: str, scenario_ids: list[str]) -> None:
        """Every member must be an active scenario of the same project."""
        for sid in scenario_ids:
            row = self._conn.execute(
                "SELECT project_id FROM scenarios WHERE id = ? AND is_active = 1",
                (sid,),
            ).fetchone()
            if row is None:
                raise ScenarioNotFound(sid)
            if (row["project_id"] or DEFAULT_PROJECT_ID) != project_id:
                raise ForeignScenario(sid)

    async def list_sets(self, project_id: str | None = None) -> list[ScenarioSet]:
        if project_id is not None:
            self._project_row(project_id)
            rows = self._conn.execute(
                "SELECT * FROM scenario_sets WHERE project_id = ? ORDER BY name",
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM scenario_sets ORDER BY name"
            ).fetchall()
        return [self._set_model(r) for r in rows]

    async def get_set(self, set_id: str) -> ScenarioSet:
        return self._set_model(self._set_row(set_id))

    async def create_set(self, project_id: str, draft: ScenarioSetDraft) -> ScenarioSet:
        self._project_row(project_id)
        unique_ids = list(dict.fromkeys(draft.scenario_ids))
        self._check_members(project_id, unique_ids)
        set_id = f"set-{uuid.uuid4().hex[:8]}"
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO scenario_sets (id, project_id, name, description,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (set_id, project_id, draft.name, draft.description, now, now),
            )
            self._conn.executemany(
                "INSERT INTO set_members (set_id, scenario_id) VALUES (?, ?)",
                [(set_id, sid) for sid in unique_ids],
            )
        return await self.get_set(set_id)

    async def update_set(self, set_id: str, draft: ScenarioSetDraft) -> ScenarioSet:
        row = self._set_row(set_id)
        unique_ids = list(dict.fromkeys(draft.scenario_ids))
        self._check_members(row["project_id"], unique_ids)
        with self._conn:
            self._conn.execute(
                "UPDATE scenario_sets SET name = ?, description = ?, updated_at = ?"
                " WHERE id = ?",
                (draft.name, draft.description, _now(), set_id),
            )
            self._conn.execute("DELETE FROM set_members WHERE set_id = ?", (set_id,))
            self._conn.executemany(
                "INSERT INTO set_members (set_id, scenario_id) VALUES (?, ?)",
                [(set_id, sid) for sid in unique_ids],
            )
        return await self.get_set(set_id)

    async def delete_set(self, set_id: str) -> None:
        self._set_row(set_id)
        with self._conn:
            self._conn.execute("DELETE FROM scenario_sets WHERE id = ?", (set_id,))

    async def add_to_set(self, set_id: str, scenario_id: str) -> ScenarioSet:
        row = self._set_row(set_id)
        self._check_members(row["project_id"], [scenario_id])
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO set_members (set_id, scenario_id) VALUES (?, ?)",
                (set_id, scenario_id),
            )
            self._conn.execute(
                "UPDATE scenario_sets SET updated_at = ? WHERE id = ?",
                (_now(), set_id),
            )
        return await self.get_set(set_id)

    async def remove_from_set(self, set_id: str, scenario_id: str) -> ScenarioSet:
        self._set_row(set_id)
        with self._conn:
            self._conn.execute(
                "DELETE FROM set_members WHERE set_id = ? AND scenario_id = ?",
                (set_id, scenario_id),
            )
            self._conn.execute(
                "UPDATE scenario_sets SET updated_at = ? WHERE id = ?",
                (_now(), set_id),
            )
        return await self.get_set(set_id)


async def create_store(database_url: str):
    """Pick a store backend from the connection string."""
    if database_url.startswith(("postgres://", "postgresql://")):
        from .pg_store import PostgresScenarioStore

        return await PostgresScenarioStore.connect(database_url)
    return SQLiteScenarioStore(database_url)
