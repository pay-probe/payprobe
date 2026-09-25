"""DBProbeAdapter: prove what a payment did to the database (ADR-0012).

Registered under ``db_probe_core`` and ``db_probe_switch``. One adapter,
pluggable engines (``postgresql``, ``sqlite``), and three guarantees:

* **Read-only by construction.** Every read runs in a read-only database
  session (``READ ONLY`` transaction on PostgreSQL, ``PRAGMA query_only`` on
  SQLite), so a write is refused by the database, not by a regex. A friendly
  prefilter runs first only to give a readable error.
* **Schema knowledge is connection data.** ``query`` takes SQL and positional
  parameters; any other action is a **named query** in the connection's
  ``queries`` map (``query_transaction``, ``query_balance``, …), whose SQL
  varies per environment through the override matrix.
* **Rows are bounded and redacted.** ``max_rows`` (100), ``statement_timeout_ms``
  (5000), secret-like column names masked before the response leaves.

Response shape (reads)::

    {"status": "APPROVED", "amount": 10000,          # first row, flattened
     "rows": [...], "row_count": 1, "columns": [...], "truncated": false}

Reserved keys win over a same-named column.

Writes (phase 2): ``execute`` runs on a connection that opted in with
``writes: true`` and must declare a ``cleanup`` statement, which the runner
executes when the scenario ends whatever the outcome. ``cleanup: null`` marks an
intentional permanent write and needs ``writes: "permanent"``. A cleanup
statement itself arrives with ``_cleanup: true`` and needs no cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from payprobe_common.crypto import is_secret_key

from ..base.base_adapter import BaseAdapter, StepResult
from .engines import Engine, FetchResult, ReadOnlyViolation, make_engine

log = logging.getLogger(__name__)

RESERVED_KEYS = ("rows", "row_count", "columns", "truncated", "rows_affected", "duration_ms")
_READ_STARTERS = ("select", "with", "explain", "values", "table", "show")
_LEADING_COMMENTS = re.compile(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*\s*", re.DOTALL)
MAX_POOL = 16


def _writes_mode(value: Any) -> str | None:
    """``None`` (read-only) | ``"cleanup"`` (writes with declared cleanup) |
    ``"permanent"`` (also allows ``cleanup: null``)."""
    if value in (None, False, "", 0):
        return None
    text = str(value).lower()
    if text in ("permanent",):
        return "permanent"
    if text in ("true", "1", "yes", "cleanup"):
        return "cleanup"
    raise ValueError(f"db_probe 'writes' must be false, true or 'permanent', got {value!r}")


def looks_read_only(sql: str) -> bool:
    """Friendly pre-check only; the database session is the real guard."""
    head = _LEADING_COMMENTS.sub("", sql or "").lstrip("(").split(None, 1)
    return bool(head) and head[0].lower() in _READ_STARTERS


class DBProbeAdapter(BaseAdapter):
    def __init__(self, config: dict, pool_size: int = 100):
        super().__init__(config, pool_size)
        self.engine: Engine | None = None
        self.engine_name = str(config.get("engine") or "postgresql").lower()
        self.max_rows = max(1, int(config.get("max_rows", 100)))
        self.timeout_ms = max(1, int(config.get("statement_timeout_ms", 5000)))
        self.queries: dict[str, dict] = dict(config.get("queries") or {})
        self.writes = _writes_mode(config.get("writes", False))
        self._pool = max(1, min(int(config.get("pool_size", 2)), pool_size, MAX_POOL))

    # -- lifecycle -------------------------------------------------------------

    async def connect(self) -> None:
        self.engine = make_engine(self.engine_name, self.config, pool_size=self._pool)
        await self.engine.connect()
        for name, spec in self.queries.items():
            if not isinstance(spec, dict) or not spec.get("sql"):
                raise ValueError(f"db_probe named query {name!r} needs a 'sql' string")

    async def health_check(self) -> bool:
        if self.engine is None:
            return False
        try:
            await asyncio.wait_for(self.engine.ping(), timeout=self.timeout_ms / 1000 + 1)
            return True
        except Exception as exc:  # noqa: BLE001 — health checks never raise
            log.warning("db_probe health check failed (%s): %s", self.engine.describe(), exc)
            return False

    async def disconnect(self) -> None:
        if self.engine is not None:
            engine, self.engine = self.engine, None
            await engine.close()

    # -- steps -----------------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        payload = payload or {}
        try:
            assert self.engine is not None, "db_probe adapter is not connected"
            if action == "execute":
                sql, params, shown, cleanup_registered = self._resolve_write(payload)
                result = await self.engine.execute_write(sql, params, timeout_ms=self.timeout_ms)
                response = self._shape(result)
                response["cleanup_registered"] = cleanup_registered
            else:
                sql, params, shown = self._resolve(action, payload)
                if not looks_read_only(sql):
                    raise ReadOnlyViolation(
                        "only read statements run through a query (SELECT / WITH / EXPLAIN …); "
                        "a write is action 'execute' on a connection with writes: true"
                    )
                result = await self.engine.fetch(
                    sql, params, max_rows=self.max_rows, timeout_ms=self.timeout_ms
                )
                response = self._shape(result)
            duration = int((time.monotonic() - start) * 1000)
            return StepResult(
                success=True,
                request_payload={"action": action, "sql": sql, "params": shown},
                response_payload=response,
                duration_ms=duration,
                raw_log=(
                    f"[db_probe:{self.engine.describe()}] {action}: {result.columns} "
                    f"{len(result.rows)} row(s){' (truncated)' if result.truncated else ''} "
                    f"in {duration} ms"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — a failed probe is a failed step, never a crash
            return StepResult(
                success=False,
                request_payload={
                    "action": action,
                    **{k: v for k, v in payload.items() if k != "params"},
                },
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _resolve(self, action: str, payload: dict) -> tuple[str, list[Any], list[Any]]:
        """(sql, bound params, params as shown in the request log)."""
        if action == "query":
            sql = str(payload.get("sql") or "")
            if not sql:
                raise ValueError("action 'query' needs a 'sql' string in the payload")
            params = list(payload.get("params") or [])
            return sql, params, params
        spec = self.queries.get(action)
        if spec is None:
            known = sorted(self.queries)
            raise ValueError(
                f"unknown db_probe action {action!r}: not 'query' and not a named query of this "
                f"connection (queries: {known or 'none configured'})"
            )
        keys = list(spec.get("params") or [])
        missing = [k for k in keys if k not in payload]
        if missing:
            raise ValueError(f"named query {action!r} needs payload key(s) {missing}")
        params = [payload[k] for k in keys]
        shown = ["***" if is_secret_key(str(k)) else payload[k] for k in keys]
        return str(spec["sql"]), params, shown

    def _resolve_write(self, payload: dict) -> tuple[str, list[Any], list[Any], bool]:
        """(sql, params, shown params, cleanup_registered) for ``execute``."""
        if self.writes is None:
            raise ReadOnlyViolation(
                "this connection is read-only (writes: false); set writes: true on the "
                "connection (per environment) to allow 'execute'"
            )
        sql = str(payload.get("sql") or "")
        if not sql:
            raise ValueError("action 'execute' needs a 'sql' string in the payload")
        params = list(payload.get("params") or [])
        is_cleanup = bool(payload.get("_cleanup"))
        if is_cleanup:
            return sql, params, params, False
        if "cleanup" not in payload:
            raise ValueError(
                "action 'execute' needs a declared cleanup ({'sql': ..., 'params': [...]}) so the "
                "runner can undo it when the scenario ends; cleanup: null marks a permanent write "
                "and needs writes: 'permanent' on the connection"
            )
        cleanup = payload.get("cleanup")
        if cleanup is None:
            if self.writes != "permanent":
                raise ReadOnlyViolation(
                    "a permanent write (cleanup: null) needs writes: 'permanent' on the connection"
                )
            return sql, params, params, False
        if not (isinstance(cleanup, dict) and cleanup.get("sql")):
            raise ValueError("cleanup must be {'sql': ..., 'params': [...]} or null")
        return sql, params, params, True

    def _shape(self, result: FetchResult) -> dict[str, Any]:
        rows = [self._redact(r) for r in result.rows]
        response: dict[str, Any] = {}
        if rows:
            response.update({k: v for k, v in rows[0].items() if k not in RESERVED_KEYS})
        response["rows"] = rows
        response["row_count"] = len(rows)
        response["columns"] = list(result.columns)
        response["truncated"] = result.truncated
        if result.rows_affected is not None:
            response["rows_affected"] = result.rows_affected
        return response

    @staticmethod
    def _redact(row: dict[str, Any]) -> dict[str, Any]:
        return {
            k: ("***" if is_secret_key(str(k)) and v not in (None, "") else v)
            for k, v in row.items()
        }


__all__ = ["RESERVED_KEYS", "DBProbeAdapter", "looks_read_only"]
