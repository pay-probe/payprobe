"""PostgreSQL engine: asyncpg pool, every read inside ``READ ONLY`` transaction
with ``SET LOCAL statement_timeout``.

Config keys: ``host``, ``port`` (5432), ``dbname`` / ``database``, ``user``,
``password``, or a single ``dsn``; ``pool_size`` (capped by the adapter);
``connect_timeout_sec`` (10). ``asyncpg`` is imported at ``connect()`` so the
adapter module itself imports cleanly everywhere.
"""

from __future__ import annotations

from typing import Any

from .base import Engine, FetchResult, ReadOnlyViolation, jsonable


class PostgresEngine(Engine):
    name = "postgresql"

    def __init__(self, config: dict, *, pool_size: int) -> None:
        super().__init__(config, pool_size=pool_size)
        self._pool: Any = None

    def _connect_kwargs(self) -> dict:
        c = self.config
        if c.get("dsn"):
            return {"dsn": str(c["dsn"])}
        return {
            "host": c.get("host", "localhost"),
            "port": int(c.get("port", 5432)),
            "database": c.get("dbname") or c.get("database"),
            "user": c.get("user"),
            "password": c.get("password"),
        }

    async def connect(self) -> None:
        import asyncpg  # already a worker dependency; lazy so the module imports anywhere

        self._pool = await asyncpg.create_pool(
            min_size=1,
            max_size=max(1, self.pool_size),
            timeout=float(self.config.get("connect_timeout_sec", 10)),
            command_timeout=None,
            **self._connect_kwargs(),
        )

    async def ping(self) -> None:
        await self.fetch("SELECT 1 AS ok", [], max_rows=1, timeout_ms=2000)

    async def fetch(
        self, sql: str, params: list[Any], *, max_rows: int, timeout_ms: int
    ) -> FetchResult:
        if self._pool is None:
            raise RuntimeError("postgresql engine is not connected")
        import asyncpg

        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            try:
                stmt = await conn.prepare(sql)
                columns = [a.name for a in stmt.get_attributes()]
                cur = await stmt.cursor(*params)
                fetched = await cur.fetch(max_rows + 1)
            except asyncpg.exceptions.ReadOnlySQLTransactionError as exc:
                raise ReadOnlyViolation(
                    f"postgresql refused a write in a READ ONLY transaction: {exc}"
                ) from None
            except asyncpg.exceptions.QueryCanceledError:
                raise TimeoutError(f"statement exceeded {timeout_ms} ms") from None
        truncated = len(fetched) > max_rows
        rows = [{c: jsonable(r[c]) for c in columns} for r in fetched[:max_rows]]
        return FetchResult(rows=rows, columns=columns, truncated=truncated)

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()

    def describe(self) -> str:
        c = self.config
        if c.get("dsn"):
            return "postgresql:<dsn>"
        return f"postgresql:{c.get('host', 'localhost')}:{c.get('port', 5432)}/{c.get('dbname') or c.get('database')}"
