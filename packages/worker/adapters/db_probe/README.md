# DB Probe Adapter

Prove what a payment did to the database: the transaction row in the core
system, the balance that moved, the audit entry the switch wrote. Registered as
`db_probe_core` and `db_probe_switch` (two default-connection types, one class).
Design and rationale: [ADR-0012](../../../../docs/adr/0012-database-probe-adapter-and-run-fixtures.md).

## Read-only, by the database

Every read runs inside a read-only session: a `READ ONLY` transaction on
PostgreSQL, `PRAGMA query_only` on SQLite. A write is refused by the database
itself, not by a regex (a statement prefilter runs first only to give a readable
error). Writes (`execute` with a declared `cleanup`) arrive in ADR-0012 phase 2
and need a connection that opts in with `writes: true`.

## Engines

| `engine` | Driver | Status |
|---|---|---|
| `postgresql` | asyncpg (already a worker dependency) | built |
| `sqlite` | stdlib `sqlite3` in a thread | built — what examples and CI use |
| `oracle`, `mssql`, `mysql` | `oracledb` / `aioodbc` / `aiomysql` | extras, not built (a clear error names the driver) |

## Config

```jsonc
{
  "engine": "postgresql",
  "host": "db-core.internal", "port": 5432,
  "dbname": "corebank", "user": "payprobe_ro", "password": "${key.CORE_DB_PASSWORD}",
  "pool_size": 2,                  // capped at 16
  "statement_timeout_ms": 5000,
  "max_rows": 100,
  "queries": {                     // named queries: schema knowledge is connection data
    "query_transaction": {"sql": "SELECT status, amount, rrn FROM txn WHERE rrn = $1", "params": ["rrn"]},
    "query_balance":     {"sql": "SELECT balance, status FROM account WHERE id = $1", "params": ["account_id"]}
  }
}
```

SQLite for examples and tests: `{"engine": "sqlite", "dsn": ":memory:",
"init_sql": ["CREATE TABLE txn (...)", "INSERT INTO txn VALUES (...)"]}`.
`init_sql` runs once at connect, before the session turns read-only. SQLite
binds positional parameters as `?`, PostgreSQL as `$1`.

`password` and `dsn` are secret-named: encrypted at rest, masked on read, and
best written as `${key.NAME}` references to the test-data registry.

## Actions

| Action | Payload | Returns |
|---|---|---|
| `query` | `sql`, `params` (positional list) | rows, read-only |
| any other name | the keys the named query's `params` list | the named query's rows |
| `execute` | (phase 2) | refused until then |

## Response shape

```jsonc
{"status": "APPROVED", "amount": 10000, "rrn": "…",     // first row, flattened
 "rows": [{"status": "APPROVED", "amount": 10000, "rrn": "…"}],
 "row_count": 1, "columns": ["status", "amount", "rrn"], "truncated": false}
```

So `{"field": "status", "operator": "eq", "expected": "APPROVED"}` on
`query_transaction` means what every pack and example already says, and
`row_count` asserts on existence. Reserved keys (`rows`, `row_count`, `columns`,
`truncated`, `rows_affected`, `duration_ms`) win over a same-named column;
secret-like column names (`password`, `pin`, …) are masked to `***`.
Downstream steps read `${probe.response.rows[0].pan}` or loop over
`${probe.response.rows}`.

## Health

Phase 1 runs `SELECT 1` under the statement timeout; a dead database blocks
every scenario that touches the probe (BLOCKED, not FAILED).
