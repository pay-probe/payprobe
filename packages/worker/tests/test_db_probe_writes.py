"""ADR-0012 phase 2: opt-in writes with declared cleanup.

A connection opts in with ``writes: true``; every ``execute`` declares a
``cleanup`` the runner executes when the scenario ends (pass, fail,
stop_on_failure, error), in reverse order, as ``cleanup`` step outcomes that
never change the verdict. ``cleanup: null`` is a permanent write and needs
``writes: "permanent"``.
"""

from __future__ import annotations

import pytest

from worker.adapters.db_probe.adapter import DBProbeAdapter
from worker.adapters.db_probe.engines import ReadOnlyViolation
from worker.engine import FAILED, PASSED, InMemorySink, WorkerEngine

# IF NOT EXISTS: the engine tears adapters down after a run, so the post-run
# checks open a fresh adapter on the same *file* database and re-run the seed.
SEED = ["CREATE TABLE IF NOT EXISTS account (id TEXT PRIMARY KEY, balance INTEGER NOT NULL)"]


def _env(tmp_path, writes=True, **over) -> dict:
    cfg = {
        "engine": "sqlite",
        "dsn": str(tmp_path / "writes.db"),
        "init_sql": SEED,
        "writes": writes,
        **over,
    }
    return {"name": "sqlite-writes", "adapters": {"db_probe_core": cfg}}


def _seed_step(step_id="seed", acct="ACC-9", cleanup=...):
    payload = {
        "sql": "INSERT INTO account (id, balance) VALUES (?, ?)",
        "params": [acct, 50000],
    }
    if cleanup is ...:
        payload["cleanup"] = {"sql": "DELETE FROM account WHERE id = ?", "params": [acct]}
    else:
        payload["cleanup"] = cleanup
    return {
        "id": step_id,
        "kind": "action",
        "target": "db_probe_core",
        "action": "execute",
        "payload": payload,
    }


def _count_step(step_id="count", expected=1):
    return {
        "id": step_id,
        "kind": "action",
        "target": "db_probe_core",
        "action": "query",
        "payload": {"sql": "SELECT count(*) AS n FROM account"},
        "assertions": [{"field": "n", "operator": "eq", "expected": expected}],
    }


def _scenario(steps, **over) -> dict:
    return {"id": "w", "name": "writes", "steps": steps, **over}


async def _run(env, scenario):
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="writes")
    sc = summary["scenarios"][0]
    return sc, {s["step_id"]: s for s in sc["steps"]}, engine


# -- adapter contract ------------------------------------------------------------------


async def test_execute_requires_opt_in_and_a_declared_cleanup():
    ro = DBProbeAdapter({"engine": "sqlite", "dsn": ":memory:", "init_sql": SEED})
    await ro.connect()
    try:
        res = await ro.execute(
            "execute", {"sql": "INSERT INTO account VALUES ('a', 1)", "cleanup": None}
        )
        assert not res.success and "writes: false" in res.error
    finally:
        await ro.disconnect()
    rw = DBProbeAdapter({"engine": "sqlite", "dsn": ":memory:", "init_sql": SEED, "writes": True})
    await rw.connect()
    try:
        res = await rw.execute("execute", {"sql": "INSERT INTO account VALUES ('a', 1)"})
        assert not res.success and "declared cleanup" in res.error
        res = await rw.execute(
            "execute", {"sql": "INSERT INTO account VALUES ('a', 1)", "cleanup": None}
        )
        assert not res.success and "writes: 'permanent'" in res.error
        res = await rw.execute(
            "execute", {"sql": "INSERT INTO account VALUES ('a', 1)", "cleanup": {"params": []}}
        )
        assert not res.success and "cleanup must be" in res.error
        ok = await rw.execute(
            "execute",
            {
                "sql": "INSERT INTO account VALUES ('a', 1)",
                "cleanup": {"sql": "DELETE FROM account WHERE id = 'a'"},
            },
        )
        assert ok.success and ok.response_payload["rows_affected"] == 1
        assert ok.response_payload["cleanup_registered"] is True
        # the read session still refuses writes even on a writes-enabled connection
        with pytest.raises(ReadOnlyViolation):
            await rw.engine.fetch("DELETE FROM account", [], max_rows=1, timeout_ms=1000)
        seen = await rw.execute("query", {"sql": "SELECT count(*) AS n FROM account"})
        assert seen.response_payload["n"] == 1  # both connections see the same in-memory database
    finally:
        await rw.disconnect()


async def test_permanent_write_needs_the_second_opt_in():
    perm = DBProbeAdapter(
        {"engine": "sqlite", "dsn": ":memory:", "init_sql": SEED, "writes": "permanent"}
    )
    await perm.connect()
    try:
        res = await perm.execute(
            "execute", {"sql": "INSERT INTO account VALUES ('p', 1)", "cleanup": None}
        )
        assert res.success and res.response_payload["cleanup_registered"] is False
    finally:
        await perm.disconnect()
    with pytest.raises(ValueError, match="writes"):
        DBProbeAdapter({"engine": "sqlite", "writes": "sometimes"})


async def test_returning_rows_come_back_from_a_write():
    rw = DBProbeAdapter({"engine": "sqlite", "dsn": ":memory:", "init_sql": SEED, "writes": True})
    await rw.connect()
    try:
        res = await rw.execute(
            "execute",
            {
                "sql": "INSERT INTO account VALUES ('r', 7) RETURNING id, balance",
                "cleanup": {"sql": "DELETE FROM account WHERE id = 'r'"},
            },
        )
        assert (
            res.success
            and res.response_payload["id"] == "r"
            and res.response_payload["balance"] == 7
        )
    finally:
        await rw.disconnect()


# -- the runner's cleanup ------------------------------------------------------------


async def test_cleanup_runs_after_a_passing_scenario_in_reverse_order(tmp_path):
    steps = [_seed_step("seed_a", "A"), _seed_step("seed_b", "B"), _count_step(expected=2)]
    sc, by_id, engine = await _run(_env(tmp_path), _scenario(steps))
    assert sc["status"] == PASSED, sc
    assert [s["step_id"] for s in sc["steps"]] == [
        "seed_a",
        "seed_b",
        "count",
        "seed_b.cleanup",
        "seed_a.cleanup",
    ]
    assert all(s["status"] == PASSED for s in sc["steps"])
    assert by_id["seed_a.cleanup"]["action"] == "cleanup"
    assert by_id["seed_a.cleanup"]["response"]["rows_affected"] == 1
    assert "_cleanup" not in by_id["seed_a.cleanup"]["request"]
    assert sc["notes"] == []
    # the rows are gone
    probe = await engine.registry.get("db_probe_core")
    after = await probe.execute("query", {"sql": "SELECT count(*) AS n FROM account"})
    assert after.response_payload["n"] == 0


async def test_cleanup_runs_when_the_scenario_fails_and_stops_early(tmp_path):
    steps = [
        _seed_step("seed", "A"),
        _count_step("wrong", expected=99),  # fails; stop_on_failure (default) halts the walk
        _seed_step("never", "B"),
    ]
    sc, by_id, engine = await _run(_env(tmp_path), _scenario(steps))
    assert sc["status"] == FAILED
    ids = [s["step_id"] for s in sc["steps"]]
    assert "never" not in ids and "seed.cleanup" in ids and "never.cleanup" not in ids
    assert by_id["seed.cleanup"]["status"] == PASSED
    probe = await engine.registry.get("db_probe_core")
    assert (
        await probe.execute("query", {"sql": "SELECT count(*) AS n FROM account"})
    ).response_payload["n"] == 0


async def test_failed_write_registers_no_cleanup_and_failed_cleanup_is_a_note_not_a_verdict(
    tmp_path,
):
    steps = [
        _seed_step(
            "seed", "A", cleanup={"sql": "DELETE FROM no_such_table WHERE id = ?", "params": ["A"]}
        ),
        {
            "id": "dup",
            "kind": "action",
            "target": "db_probe_core",
            "action": "execute",
            "payload": {
                "sql": "INSERT INTO account VALUES ('A', 1)",  # primary key clash: the write fails
                "cleanup": {"sql": "DELETE FROM account WHERE id = 'A'"},
            },
        },
    ]
    sc, by_id, _ = await _run(_env(tmp_path), _scenario(steps, stop_on_failure=False))
    assert by_id["dup"]["status"] == FAILED and "dup.cleanup" not in by_id  # nothing to undo
    assert (
        by_id["seed.cleanup"]["status"] == FAILED
        and "no_such_table" in by_id["seed.cleanup"]["error"]
    )
    assert sc["notes"] == ["cleanup_failed:seed"]
    assert sc["status"] == FAILED  # because of dup, not because of the cleanup


async def test_cleanup_is_generic_and_only_registered_on_success(tmp_path):
    """A read step with a stray cleanup key registers it too (the mechanism is
    the runner's, not the probe's); a failed step never does."""
    steps = [
        {
            "id": "read",
            "kind": "action",
            "target": "db_probe_core",
            "action": "query",
            "payload": {
                "sql": "SELECT 1 AS one",
                "cleanup": {"sql": "INSERT INTO account VALUES ('from-read', 1)"},
            },
        },
    ]
    _, by_id, engine = await _run(_env(tmp_path), _scenario(steps))
    assert by_id["read.cleanup"]["status"] == PASSED
    probe = await engine.registry.get("db_probe_core")
    assert (await probe.execute("query", {"sql": "SELECT id FROM account"})).response_payload[
        "id"
    ] == "from-read"
