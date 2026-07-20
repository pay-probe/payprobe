"""Opening databases created by older service versions must not crash."""

import asyncio
import json
import sqlite3

from api.store import SQLiteScenarioStore


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_pre_softdelete_db(tmp_path):
    """DB with current_version but no is_active / projects (constructor-init era)."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE scenarios (
            id TEXT PRIMARY KEY,
            current_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE scenario_versions (
            scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            document TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (scenario_id, version)
        );
        INSERT INTO scenarios VALUES
            ('scn-old1', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.execute(
        "INSERT INTO scenario_versions VALUES ('scn-old1', 1, ?, '', "
        "'2026-01-01T00:00:00+00:00')",
        (json.dumps({"name": "legacy case", "steps": []}),),
    )
    conn.commit()
    conn.close()

    store = SQLiteScenarioStore(db)
    assert run(store.count()) == 1
    summaries = run(store.list())
    assert summaries[0].name == "legacy case"
    assert summaries[0].project_id == "prj-default"
    assert summaries[0].test_class == "e2e"
    # soft delete works after migration
    run(store.delete("scn-old1"))
    assert run(store.count()) == 0


def test_pre_versioning_db(tmp_path):
    """Earliest scaffold: document stored on the scenario row, no history table."""
    db = tmp_path / "ancient.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE scenarios (
            id TEXT PRIMARY KEY,
            name TEXT,
            definition TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO scenarios VALUES ('scn-anc1', 'ancient', ?, "
        "'2025-12-01T00:00:00+00:00', '2025-12-01T00:00:00+00:00')",
        (json.dumps({"name": "ancient case", "steps": []}),),
    )
    conn.commit()
    conn.close()

    store = SQLiteScenarioStore(db)
    assert run(store.count()) == 1
    sc = run(store.get("scn-anc1"))
    assert sc.name == "ancient case"
    assert sc.version == 1
    assert sc.project_id == "prj-default"


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    SQLiteScenarioStore(db)
    store = SQLiteScenarioStore(db)  # reopen — must not fail or duplicate
    assert run(store.list_projects())[0].id == "prj-default"
