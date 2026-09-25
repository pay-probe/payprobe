"""SQLite engine: stdlib, runs in a thread, read-only by ``PRAGMA query_only``.

What examples and CI use: ``{"engine": "sqlite", "dsn": ":memory:", "init_sql":
[...]}`` gives a self-contained database seeded once at ``connect()``. ``dsn``
may also be a file path. ``init_sql`` runs before the read connection is
switched to ``query_only``, so it is the one place a probe may write without
opting in (it is the fixture that makes an example runnable, not a write path
into a customer database; the pragma is on before the first step runs).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from .base import Engine, FetchResult, ReadOnlyViolation, jsonable


class SqliteEngine(Engine):
    name = "sqlite"

    def __init__(self, config: dict, *, pool_size: int) -> None:
        super().__init__(config, pool_size=pool_size)
        self.dsn = str(config.get("dsn") or config.get("dbname") or ":memory:")
        self._read: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()  # one statement at a time on the shared connection

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.dsn, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            for stmt in self.config.get("init_sql") or []:
                conn.execute(str(stmt))
            conn.execute("PRAGMA query_only = ON")
            return conn

        self._read = await asyncio.to_thread(_open)

    async def ping(self) -> None:
        await self.fetch("SELECT 1 AS ok", [], max_rows=1, timeout_ms=2000)

    async def fetch(
        self, sql: str, params: list[Any], *, max_rows: int, timeout_ms: int
    ) -> FetchResult:
        if self._read is None:
            raise RuntimeError("sqlite engine is not connected")
        conn = self._read

        def _run() -> FetchResult:
            deadline = time.monotonic() + timeout_ms / 1000

            def _abort_when_late() -> int:
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(_abort_when_late, 1000)
            try:
                cur = conn.execute(sql, [jsonable(p) if isinstance(p, dict) else p for p in params])
                columns = [d[0] for d in cur.description] if cur.description else []
                fetched = cur.fetchmany(max_rows + 1)
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if "readonly" in msg or "query_only" in msg or "attempt to write" in msg:
                    raise ReadOnlyViolation(f"sqlite refused a write in a read-only session: {msg}")
                if "interrupted" in msg:
                    raise TimeoutError(f"statement exceeded {timeout_ms} ms") from None
                raise
            finally:
                conn.set_progress_handler(None, 0)
            truncated = len(fetched) > max_rows
            rows = [{c: jsonable(r[c]) for c in columns} for r in fetched[:max_rows]]
            return FetchResult(rows=rows, columns=columns, truncated=truncated)

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if self._read is not None:
            conn, self._read = self._read, None
            await asyncio.to_thread(conn.close)

    def describe(self) -> str:
        return f"sqlite:{self.dsn}"
