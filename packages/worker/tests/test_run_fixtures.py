"""ADR-0012 phase 3: run-level ``before`` / ``after`` fixtures in the engine.

Fixtures are ordinary scenarios run once around the whole run: ``before`` after
phase 1 (a failure BLOCKs every scenario and fails the run), ``after`` after
phase 3 in a ``finally`` (also when scenarios fail), recorded under
``summary["fixtures"]`` and never counted among the scenarios.
"""

from __future__ import annotations

from worker.engine import BLOCKED, FAILED, PASSED, InMemorySink, WorkerEngine

SEED = ["CREATE TABLE IF NOT EXISTS account (id TEXT PRIMARY KEY, balance INTEGER NOT NULL)"]


def _env(tmp_path) -> dict:
    return {
        "name": "fixtures",
        "adapters": {
            "db_probe_core": {
                "engine": "sqlite",
                "dsn": str(tmp_path / "fixtures.db"),
                "init_sql": SEED,
                "writes": "permanent",  # fixtures may leave state for the run; the after-fixture purges it
            }
        },
    }


def _seed_fixture(sid="seed-accounts", acct="ACC-1", fail=False):
    return {
        "id": sid,
        "name": sid,
        "steps": [
            {
                "id": "seed",
                "kind": "action",
                "target": "db_probe_core",
                "action": "execute",
                "payload": {
                    "sql": (
                        "INSERT INTO account VALUES (?, ?)"
                        if not fail
                        else "INSERT INTO nowhere VALUES (1)"
                    ),
                    "params": [acct, 50000] if not fail else [],
                    "cleanup": None,
                },
            }
        ],
    }


def _purge_fixture(sid="purge"):
    return {
        "id": sid,
        "name": sid,
        "steps": [
            {
                "id": "purge",
                "kind": "action",
                "target": "db_probe_core",
                "action": "execute",
                "payload": {"sql": "DELETE FROM account", "cleanup": None},
            }
        ],
    }


def _verify_scenario(sid="verify", expected=1, test_class="e2e"):
    return {
        "id": sid,
        "name": sid,
        "test_class": test_class,
        "steps": [
            {
                "id": "count",
                "kind": "action",
                "target": "db_probe_core",
                "action": "query",
                "payload": {"sql": "SELECT count(*) AS n FROM account"},
                "assertions": [{"field": "n", "operator": "eq", "expected": expected}],
            }
        ],
    }


async def _run(tmp_path, scenarios, fixtures):
    engine = WorkerEngine(_env(tmp_path), InMemorySink())
    return await engine.run_scenario_batch(scenarios, run_id="fx", fixtures=fixtures)


async def test_before_fixture_seeds_state_the_scenarios_see_and_after_fixture_purges_it(tmp_path):
    summary = await _run(
        tmp_path,
        [_verify_scenario(expected=1)],
        {"before": [_seed_fixture()], "after": [_purge_fixture()]},
    )
    assert summary["status"] == PASSED, summary
    assert [s["status"] for s in summary["scenarios"]] == [PASSED]
    fx = summary["fixtures"]
    assert [f["scenario_id"] for f in fx["before"]] == ["seed-accounts"]
    assert [f["scenario_id"] for f in fx["after"]] == ["purge"]
    assert fx["before"][0]["status"] == PASSED and fx["after"][0]["status"] == PASSED
    assert fx["after"][0]["steps"][0]["response"]["rows_affected"] == 1
    # fixtures are not scenarios: counts and gates never see them
    assert len(summary["scenarios"]) == 1
    assert summary["phases"]["phase_3"]["total"] == 1


async def test_failed_before_fixture_blocks_every_scenario_and_fails_the_run(tmp_path):
    summary = await _run(
        tmp_path,
        [_verify_scenario("a"), _verify_scenario("b", test_class="integration")],
        {"before": [_seed_fixture(fail=True)], "after": [_purge_fixture()]},
    )
    assert summary["status"] == FAILED
    assert {s["status"] for s in summary["scenarios"]} == {BLOCKED}
    assert summary["phases"]["phase_1"]["status"] == FAILED
    assert summary["phases"]["phase_1"]["fixture_failed"] == ["seed-accounts"]
    assert summary["fixtures"]["before"][0]["status"] == FAILED
    # the after fixture still ran
    assert summary["fixtures"]["after"][0]["status"] == PASSED


async def test_after_fixture_runs_when_scenarios_fail_and_cannot_flip_the_verdict(tmp_path):
    summary = await _run(
        tmp_path,
        [_verify_scenario(expected=42)],  # wrong expectation: the scenario fails
        {"after": [_purge_fixture()]},
    )
    assert summary["status"] == FAILED
    assert summary["fixtures"]["after"][0]["status"] == PASSED
    assert "before" not in summary["fixtures"]
    # and a failing after fixture never rescues or worsens a verdict
    summary = await _run(
        tmp_path,
        [_verify_scenario(expected=0)],
        {"after": [_seed_fixture("broken-after", fail=True)]},
    )
    assert summary["status"] == PASSED
    assert summary["fixtures"]["after"][0]["status"] == FAILED


async def test_no_fixtures_means_no_fixtures_key(tmp_path):
    summary = await _run(tmp_path, [_verify_scenario(expected=0)], None)
    assert "fixtures" not in summary and summary["status"] == PASSED


async def test_before_fixtures_are_skipped_when_phase_1_already_failed(tmp_path):
    env = _env(tmp_path)
    env["adapters"]["dead"] = {"engine": "sqlite", "dsn": "/nonexistent/x/y.db"}
    engine = WorkerEngine(env, InMemorySink())
    dead_scenario = {
        "id": "d",
        "name": "d",
        "steps": [
            {
                "id": "q",
                "kind": "action",
                "target": "dead",
                "action": "query",
                "payload": {"sql": "SELECT 1"},
            }
        ],
    }
    summary = await engine.run_scenario_batch(
        [dead_scenario], run_id="fx-dead", fixtures={"before": [_seed_fixture()]}
    )
    assert summary["phases"]["phase_1"]["status"] == FAILED
    assert summary["fixtures"]["before"] == []  # not run
    assert summary["scenarios"][0]["status"] == BLOCKED
