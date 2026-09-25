# Proving state with the database probe

> **How-to** — assert on what a payment did to a database: the transaction row
> in the core system, the balance that moved, the audit entry the switch wrote.
> Design and rationale: [ADR-0012](../adr/0012-database-probe-adapter-and-run-fixtures.md).

The probe is a connection of adapter type `db_probe_core` or `db_probe_switch`
(two default-connection types, one worker class). It is **read-only by the
database session**: every statement runs in a `READ ONLY` transaction on
PostgreSQL or under `PRAGMA query_only` on SQLite, so a write is refused by the
database itself. Writes exist only where a connection opts in, and every
write declares its cleanup (section 5).

## 1. Create the connection

Schema knowledge belongs to you, not to PayProbe, so the SQL lives on the
connection as **named queries** and can differ per environment through the
override matrix.

```jsonc
{
  "name": "core_db",
  "adapter": "db_probe_core",
  "engine": "postgresql",                       // or "sqlite"
  "host": "db-core.internal", "port": 5432,
  "dbname": "corebank", "user": "payprobe_ro",
  "password": "${key.CORE_DB_PASSWORD}",        // a test-data registry key; never inline in a repo
  "statement_timeout_ms": 5000,
  "max_rows": 100,
  "queries": {
    "query_transaction": { "sql": "SELECT status, amount, rrn FROM txn WHERE rrn = $1", "params": ["rrn"] },
    "query_balance":     { "sql": "SELECT status, balance FROM account WHERE id = $1", "params": ["account_id"] }
  },
  "environment_overrides": {
    "staging": { "host": "db-staging",
                 "queries": { "query_transaction": { "sql": "SELECT status, amount, rrn FROM txn_stg WHERE rrn = $1", "params": ["rrn"] } } }
  }
}
```

Use a **read-only database role** as well. The session guard is what PayProbe
can prove; the role is defence in depth.

PostgreSQL binds parameters as `$1`, SQLite as `?`. `password` and `dsn` are
secret-named: encrypted at rest, masked (`••••` + fingerprint) on every read.

## 2. Assert on the row

The response flattens the **first row** to the top level and adds an envelope
(`rows`, `row_count`, `columns`, `truncated`), so the assertion operators you
already use apply directly:

```jsonc
{ "id": "settle", "kind": "action", "target": "db_probe_core", "action": "query_transaction",
  "payload": { "rrn": "${auth.response.rrn}" },
  "assertions": [
    { "field": "status",    "operator": "eq", "expected": "APPROVED" },
    { "field": "row_count", "operator": "eq", "expected": 1 }
  ] }
```

Ad hoc reads take SQL and positional parameters (never string formatting):

```jsonc
{ "action": "query", "payload": { "sql": "SELECT pan, expiry FROM test_cards WHERE active = $1 LIMIT 5", "params": [true] } }
```

Downstream steps read `${cards.response.rows[0].pan}`, or a `loop` node
iterates `${cards.response.rows}`. Secret-like column names (`password`,
`pin`, …) come back as `***`.

## 3. Run it without a database

The bundled connection `example_sqlite_probe`
(`examples/connections/bundled.json`) is a self-contained SQLite database
seeded by `init_sql` at connect and then read-only. The example scenario
`examples/scenarios/db_probe_settlement.json` runs against it for real in the
test suite and under `mode: mock` in CI. Copy both as a starting point.

## 4. What a failure looks like

- **Database down or credentials wrong**: phase 1 health fails and every
  scenario touching the probe is **BLOCKED** with one signal, not a wall of
  failed steps.
- **A write, however disguised**: `ReadOnlyViolation` on the step, the
  database untouched.
- **Runaway statement**: `TimeoutError: statement exceeded N ms`; the
  connection stays usable.
- **Unknown action**: the error names the connection's named queries.
- **More rows than `max_rows`**: `truncated: true`; assert on it if a probe
  must be exact.

## 5. Seed, verify, clean up (writes)

A connection opts into writes per environment (`writes: true`, typically in a
staging override, never in production). Every `execute` declares a `cleanup`
statement; the runner executes the declared cleanups when the scenario ends,
in reverse order, **whatever the outcome** (pass, fail, `stop_on_failure`,
error), and records each as a `cleanup` step. A failed cleanup adds a
`cleanup_failed:<step>` note to the scenario and never changes its verdict.

```jsonc
{ "id": "seed", "kind": "action", "target": "db_probe_core", "action": "execute",
  "payload": {
    "sql": "INSERT INTO account (id, balance) VALUES ($1, $2)", "params": ["${vars.acct}", 50000],
    "cleanup": { "sql": "DELETE FROM account WHERE id = $1", "params": ["${vars.acct}"] } } }
```

The response carries `rows_affected` (and any `RETURNING` columns) plus
`cleanup_registered: true`. A **permanent** write (`cleanup: null`) is a
second opt-in: it needs `writes: "permanent"` on the connection. The read
session stays read-only on a writes-enabled connection; only `execute` uses
the write session.

**Load runs refuse probes.** A load run executes the whole scenario per
transaction, so a probe step would query the database at the target TPS; the
request is refused with a 400 unless the connection carries `load_ok: true`
(per environment), mirroring the `external: true` rule for provider sandboxes.
Mocked probes (`mode: mock`, or `mock: true` on the adapter) are exempt.

**Diagnostics.** `GET /diagnostics?layers=databases` reports, per probe
connection: reachable, whether the session refused a write, whether writes are
enabled, and which named queries fail to parse.

## Engines

| `engine` | Driver | Status |
|---|---|---|
| `postgresql` | asyncpg (worker dependency) | built |
| `sqlite` | stdlib | built; examples and CI |
| `oracle` / `mssql` / `mysql` | `oracledb` / `aioodbc` / `aiomysql` | extras, not built; the error names the driver |
