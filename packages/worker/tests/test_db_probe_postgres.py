"""ADR-0012 phase 1: the probe against a real PostgreSQL.

Gated the way the agent-hub suite is: ``PAYPROBE_TEST_PG_DSN`` (default: the
compose ``postgres`` forwarded to localhost, database ``payprobe_test``). When
the database is unreachable the module is **skipped with a reason**, never
silently green. Reads only; nothing here creates or changes objects (the
read-only test proves the session refuses to).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from worker.adapters.db_probe.adapter import DBProbeAdapter
from worker.adapters.db_probe.engines import ReadOnlyViolation

DSN = os.environ.get(
    "PAYPROBE_TEST_PG_DSN", "postgresql://payprobe:payprobe@127.0.0.1:5432/payprobe_test"
)


def _reachable() -> str | None:
    try:
        import asyncpg
    except ImportError:
        return "asyncpg not installed"

    async def probe() -> None:
        conn = await asyncio.wait_for(asyncpg.connect(DSN), 3)
        await conn.close()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ skip with reason
        return f"{type(exc).__name__}: {exc}"
    return None


_REASON = _reachable()
pytestmark = pytest.mark.skipif(
    _REASON is not None,
    reason=f"PostgreSQL not reachable at PAYPROBE_TEST_PG_DSN ({_REASON}); set it to run",
)


async def _probe(**over) -> DBProbeAdapter:
    a = DBProbeAdapter({"engine": "postgresql", "dsn": DSN, **over})
    await a.connect()
    return a


async def test_typed_values_flatten_json_friendly():
    a = await _probe()
    try:
        res = await a.execute(
            "query",
            {
                "sql": "SELECT $1::text AS status, 12345.67::numeric AS amount, 42::numeric AS whole, "
                "now()::timestamptz AS at, gen_random_uuid() AS id, decode('00ff', 'hex') AS blob",
                "params": ["APPROVED"],
            },
        )
        assert res.success, res.error
        r = res.response_payload
        assert r["status"] == "APPROVED" and r["amount"] == 12345.67 and r["whole"] == 42
        assert "T" in r["at"] and len(r["id"]) == 36 and r["blob"] == "AP8="
        assert r["row_count"] == 1 and r["columns"][:2] == ["status", "amount"]
    finally:
        await a.disconnect()


async def test_named_query_binds_positionally_with_dollar_params():
    a = await _probe(
        queries={"echo": {"sql": "SELECT $1::int AS a, $2::text AS b", "params": ["a", "b"]}}
    )
    try:
        res = await a.execute("echo", {"a": 7, "b": "x"})
        assert res.success and (res.response_payload["a"], res.response_payload["b"]) == (7, "x")
    finally:
        await a.disconnect()


async def test_session_refuses_writes_even_when_the_statement_looks_harmless():
    """A scratch table is created and dropped through a *separate*, ordinary
    connection; the probe's READ ONLY session must refuse to touch it even when
    the statement passes the friendly prefilter (a data-modifying CTE)."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    await admin.execute("CREATE TABLE IF NOT EXISTS adr0012_probe_scratch (n int)")
    try:
        a = await _probe()
        try:
            with pytest.raises(ReadOnlyViolation, match="READ ONLY"):
                await a.engine.fetch(
                    "WITH x AS (SELECT 1 AS n) INSERT INTO adr0012_probe_scratch SELECT n FROM x",
                    [],
                    max_rows=1,
                    timeout_ms=2000,
                )
            with pytest.raises(ReadOnlyViolation):
                await a.engine.fetch(
                    "DELETE FROM adr0012_probe_scratch", [], max_rows=1, timeout_ms=2000
                )
            with pytest.raises(ReadOnlyViolation):
                await a.engine.fetch(
                    "CREATE TEMP TABLE adr0012_tmp (n int)", [], max_rows=1, timeout_ms=2000
                )
            assert await admin.fetchval("SELECT count(*) FROM adr0012_probe_scratch") == 0
        finally:
            await a.disconnect()
    finally:
        await admin.execute("DROP TABLE IF EXISTS adr0012_probe_scratch")
        await admin.close()


async def test_row_cap_and_statement_timeout():
    a = await _probe(max_rows=5, statement_timeout_ms=200)
    try:
        many = await a.execute("query", {"sql": "SELECT g FROM generate_series(1, 50) AS g"})
        assert many.success and many.response_payload["row_count"] == 5
        assert many.response_payload["truncated"] is True
        slow = await a.execute("query", {"sql": "SELECT pg_sleep(1)"})
        assert not slow.success and "exceeded 200 ms" in slow.error
        assert await a.health_check() is True  # the pool survived the cancel
    finally:
        await a.disconnect()
