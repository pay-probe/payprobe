"""ADR-0012 phase 1: the database probe, reads only, on SQLite (stdlib, no service).

Covers the three guarantees (read-only by the database session, schema as
connection data, bounded and redacted rows), the error surface, the registry,
mock parity, and the shipped example running for real against the bundled
SQLite connection inside the engine.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from worker.adapters.db_probe.adapter import RESERVED_KEYS, DBProbeAdapter, looks_read_only
from worker.adapters.db_probe.engines import ReadOnlyViolation, make_engine
from worker.adapters.mock.adapter import DEFAULT_RESPONSES
from worker.adapters.registry import ADAPTER_MAP
from worker.engine import BLOCKED, FAILED, PASSED, InMemorySink, WorkerEngine

ROOT = pathlib.Path(__file__).resolve().parents[3]
BUNDLED = json.loads((ROOT / "examples" / "connections" / "bundled.json").read_text())
EXAMPLE_CONN = (BUNDLED.get("connections") or BUNDLED)["example_sqlite_probe"]
EXAMPLE_SCENARIO = json.loads(
    (ROOT / "examples" / "scenarios" / "db_probe_settlement.json").read_text()
)

SEED = [
    "CREATE TABLE txn (rrn TEXT, status TEXT, amount INTEGER, card_password TEXT, rows INTEGER)",
    "INSERT INTO txn VALUES ('000000000001','APPROVED',10000,'s3cret',7)",
    "INSERT INTO txn VALUES ('000000000002','DECLINED',500,'x',8)",
    "INSERT INTO txn VALUES ('000000000003','APPROVED',700,'y',9)",
]
QUERIES = {
    "query_transaction": {
        "sql": "SELECT status, amount, rrn, card_password, rows FROM txn WHERE rrn = ?",
        "params": ["rrn"],
    }
}


def _cfg(**over) -> dict:
    return {"engine": "sqlite", "dsn": ":memory:", "init_sql": SEED, "queries": QUERIES, **over}


async def _probe(**over) -> DBProbeAdapter:
    a = DBProbeAdapter(_cfg(**over))
    await a.connect()
    return a


# -- registry + surfaces ------------------------------------------------------------


def test_registry_resolves_both_probe_keys_to_the_adapter():
    assert ADAPTER_MAP["db_probe_core"] is DBProbeAdapter
    assert ADAPTER_MAP["db_probe_switch"] is DBProbeAdapter


def test_mock_replies_carry_the_real_response_envelope():
    for action in ("query_transaction", "query_balance"):
        canned = DEFAULT_RESPONSES[action]
        assert canned["row_count"] == 1 and canned["rows"] and canned["truncated"] is False
        assert set(canned["columns"]) <= set(canned["rows"][0])
        for col in canned["columns"]:
            assert canned[col] == canned["rows"][0][col]  # flattened first row


def test_engine_names_planned_and_unknown_give_different_errors():
    with pytest.raises(ValueError, match="not built yet.*oracledb"):
        make_engine("oracle", {}, pool_size=1)
    with pytest.raises(ValueError, match="unknown db_probe engine"):
        make_engine("mongo", {}, pool_size=1)


def test_read_only_prefilter_is_friendly_not_authoritative():
    assert looks_read_only("SELECT 1")
    assert looks_read_only("  -- comment\n WITH x AS (SELECT 1) SELECT * FROM x")
    assert looks_read_only("EXPLAIN SELECT 1")
    assert not looks_read_only("DELETE FROM txn")
    assert not looks_read_only("PRAGMA query_only = OFF")


# -- reads ---------------------------------------------------------------------------


async def test_named_query_flattens_first_row_redacts_secret_columns_and_keeps_reserved_keys():
    a = await _probe()
    try:
        res = await a.execute("query_transaction", {"rrn": "000000000001"})
        assert res.success, res.error
        r = res.response_payload
        assert (r["status"], r["amount"], r["rrn"]) == ("APPROVED", 10000, "000000000001")
        assert r["card_password"] == "***" and r["rows"][0]["card_password"] == "***"
        # a column literally named `rows` cannot shadow the envelope
        assert isinstance(r["rows"], list) and r["rows"][0]["rows"] == 7
        assert r["row_count"] == 1 and r["truncated"] is False
        assert r["columns"] == ["status", "amount", "rrn", "card_password", "rows"]
        assert set(RESERVED_KEYS) >= {"rows", "row_count", "columns", "truncated"}
        assert res.request_payload["params"] == ["000000000001"]
    finally:
        await a.disconnect()


async def test_ad_hoc_query_caps_rows_and_marks_truncation():
    a = await _probe(max_rows=2)
    try:
        res = await a.execute("query", {"sql": "SELECT rrn FROM txn ORDER BY rrn", "params": []})
        assert res.success and res.response_payload["row_count"] == 2
        assert res.response_payload["truncated"] is True
        assert res.response_payload["rows"][1]["rrn"] == "000000000002"
        none = await a.execute(
            "query", {"sql": "SELECT rrn FROM txn WHERE rrn = ?", "params": ["nope"]}
        )
        assert none.success and none.response_payload == {
            "rows": [],
            "row_count": 0,
            "columns": ["rrn"],
            "truncated": False,
        }
    finally:
        await a.disconnect()


async def test_values_come_back_json_friendly():
    a = await _probe()
    try:
        res = await a.execute("query", {"sql": "SELECT X'00FF' AS blob, 1.5 AS f, NULL AS n"})
        assert res.response_payload["blob"] == "AP8=" and res.response_payload["f"] == 1.5
        assert res.response_payload["n"] is None
        json.dumps(res.response_payload)  # must serialise for runs.db / WebSocket
    finally:
        await a.disconnect()


# -- the error surface ---------------------------------------------------------------


async def test_unknown_action_missing_param_and_execute_are_clear_failures_not_crashes():
    a = await _probe()
    try:
        unknown = await a.execute("query_balance", {"account_id": "x"})
        assert not unknown.success and "not a named query" in unknown.error
        assert "query_transaction" in unknown.error  # tells the author what exists
        missing = await a.execute("query_transaction", {})
        assert not missing.success and "needs payload key(s) ['rrn']" in missing.error
        write = await a.execute("execute", {"sql": "DELETE FROM txn"})
        assert not write.success and "phase 2" in write.error
        nosql = await a.execute("query", {})
        assert not nosql.success and "needs a 'sql'" in nosql.error
    finally:
        await a.disconnect()


async def test_write_is_refused_by_the_prefilter_and_by_the_database_session():
    a = await _probe()
    try:
        res = await a.execute("query", {"sql": "DELETE FROM txn"})
        assert not res.success and res.error.startswith("ReadOnlyViolation")
        # bypass the prefilter: the session itself refuses (PRAGMA query_only)
        with pytest.raises(ReadOnlyViolation, match="read-only session"):
            await a.engine.fetch("DELETE FROM txn", [], max_rows=10, timeout_ms=1000)
        with pytest.raises(ReadOnlyViolation):
            await a.engine.fetch(
                "WITH x AS (SELECT 1) INSERT INTO txn VALUES ('9','9',9,'9',9)",
                [],
                max_rows=10,
                timeout_ms=1000,
            )
        still = await a.execute("query", {"sql": "SELECT count(*) AS n FROM txn"})
        assert still.response_payload["n"] == 3  # nothing changed
    finally:
        await a.disconnect()


async def test_statement_timeout_interrupts_a_runaway_query():
    a = await _probe(statement_timeout_ms=200)
    try:
        res = await a.execute(
            "query",
            {
                "sql": "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c"
            },
        )
        assert not res.success and "exceeded 200 ms" in res.error
        assert res.duration_ms < 5000
        ok = await a.execute("query", {"sql": "SELECT 1 AS one"})  # the connection is still usable
        assert ok.success and ok.response_payload["one"] == 1
    finally:
        await a.disconnect()


async def test_health_check_reports_and_never_raises():
    a = await _probe()
    assert await a.health_check() is True
    await a.disconnect()
    assert await a.health_check() is False


async def test_bad_named_query_config_fails_at_connect():
    a = DBProbeAdapter(_cfg(queries={"broken": {"params": ["x"]}}))
    with pytest.raises(ValueError, match="needs a 'sql'"):
        await a.connect()


# -- through the engine -------------------------------------------------------------


async def test_shipped_example_runs_for_real_against_the_bundled_sqlite_connection():
    env = {"name": "sqlite-probe", "adapters": {"db_probe_core": dict(EXAMPLE_CONN)}}
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([EXAMPLE_SCENARIO], run_id="ex-db-probe-real")
    sc = summary["scenarios"][0]
    assert sc["status"] == PASSED, sc
    by_id = {s["step_id"]: s for s in sc["steps"]}
    assert by_id["settle"]["response"]["status"] == "APPROVED"
    assert by_id["settle"]["response"]["amount"] == 10000
    assert by_id["approved_today"]["response"]["row_count"] == 2
    assert all(a["passed"] for s in sc["steps"] for a in s.get("assertions", []))


async def test_shipped_example_still_passes_under_mock():
    engine = WorkerEngine({"mode": "mock", "adapters": {}}, InMemorySink())
    summary = await engine.run_scenario_batch([EXAMPLE_SCENARIO], run_id="ex-db-probe-mock")
    assert summary["scenarios"][0]["status"] == PASSED


async def test_unreachable_database_blocks_the_scenario_with_one_signal():
    env = {
        "name": "dead-probe",
        "adapters": {
            "db_probe_core": {"engine": "sqlite", "dsn": "/nonexistent-dir/for-sure/x.db"}
        },
    }
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([EXAMPLE_SCENARIO], run_id="ex-db-probe-dead")
    sc = summary["scenarios"][0]
    assert sc["status"] == BLOCKED, sc
    assert all(s["status"] != FAILED for s in sc["steps"])
