# ADR-0012: A real database probe adapter, and data fixtures before and after execution

**Status:** Proposed — phases 0 and 1 built 2026-09-25 on
`feature/adr-0012-db-probe` (reads only; the worker class exists, the README
row is Beta, `PLANNED_ADAPTERS` is empty). Phases 2 to 4 owed.
**Date:** 2026-09-24
**Deciders:** PayProbe maintainers (David + reviewers)
**Extends:** the "DB probe" adapter promised in `README.md` since the initial
public release (2026-07-20); `docs/history/CONNECTION-ADAPTER-EXTENSION-SCOPE.md`
(which made `db_probe_core` / `db_probe_switch` first-class connections);
ADR-0009's "specifics live in data, not in a per-target class" pattern.

## Context

PayProbe's job is to prove what a payment did, and a payment is only proven
when its *state* landed: the transaction row in the core banking database, the
balance that moved, the audit entry the switch wrote. The platform has
advertised exactly this capability since day one and has never had it. All
facts below were verified against the working tree on 2026-09-24 and
re-verified on 2026-09-25 against `main` after ADR-0011 and ADR-0013 merged
(registry import path, empty package, README row, the four action vocabularies,
`_ALLOWED_ADAPTERS`, `_target_type_key`, the three examples, nine portal files,
`asyncpg` present and `oracledb` / `aioodbc` / `aiomysql` absent: all unchanged).

### The DB probe is a phantom

`db_probe_core` and `db_probe_switch` are referenced across the whole product
surface, and the worker class every reference points at does not exist.

| Surface | What it says or does | Files |
|---|---|---|
| Worker registry | `_db_probe()` imports `worker.adapters.db_probe.adapter.DBProbeAdapter` behind `_try`; the import fails, the registry logs it at debug and moves on. `adapters registered:` never lists a `db_probe_*` key | `packages/worker/adapters/registry.py` |
| Worker package | `packages/worker/adapters/db_probe/` holds a `README.md` and an empty `__init__.py`. No `adapter.py`, no tests | `packages/worker/adapters/db_probe/` |
| README | Adapter table row: `db_probe` / "PostgreSQL / Oracle / MSSQL" / **"✅ Stable"** | `README.md` line 163 |
| Portal | 9 files: connection editor options, `DbConfig` model (`engine`, `dbname`, `user`, `password`), the constructor's `db` target family, groups typing, parameter types, playground family mapping, the adapter catalog page (which honestly says **"Planned — not yet implemented"**) | `packages/portal/src/app/**` |
| scenario-service | 5 non-test modules: the step catalog (`TargetSpec("db_probe_core")` with `query_transaction`, `query_balance`), the connection allowlist (`_ALLOWED_ADAPTERS`), the prompt-to-scenario assistant (`assist.py` adds a `settle` step on `db_probe_core` for every approved flow), the builtin certification pack `pack_auth_then_settled`, plus 6 test files | `packages/scenario-service/**` |
| Orchestrator | `_target_type_key` maps `db_probe_*` to itself (each probe is its own default-connection type) | `packages/orchestrator/api/main.py` |
| Examples | `examples/connections/bundled.json` ships `prod_db_probe` (a `db_probe_core` template); `examples/environments/production-template.json` budgets **10 000** connections for it; `examples/scenarios/code_step_surcharge.json` ends with a `settle_check` step on `db_probe_core` | `examples/**` |

Every test that touches the probe runs it through `MockAdapter`
(`{"mock": True}` in the engine tests, mock mode for packs and examples), so
the suite is green while a real run on any non-mock environment fails the
first `db_probe_core` step with `Unknown adapter`.

### The action vocabulary drifted four ways before the adapter existed

| Source | Actions it names |
|---|---|
| `adapters/db_probe/README.md` | `query_transaction`, `query_audit_log`, `assert_record_exists`, `query_raw` |
| `models/catalog.py` (the step catalog the constructor and assistant use) | `query_transaction`, `query_balance` |
| Portal adapter catalog page | `query` |
| `MockAdapter.DEFAULT_RESPONSES` | `query_transaction`, `query_account` |

Only `query_transaction` is common to all four. The mock answers
`query_balance` with the generic `{"status": "ok"}`, so the catalog's own
second action cannot be asserted on even in mock mode. This is the
per-surface-copy drift that invariant #3 was written against, on an adapter
that has no implementation to drift *from*.

### What "before and after" already means in PayProbe

The platform has three seams for data around an execution, and none of them
reach a database:

- **`init` nodes** (`_run_init_node` in `worker/engine/runner.py`) emit a fixed
  JSON object at the start of a scenario for downstream `${id.response.*}`
  references. Static only.
- **Test-data pools and tables** are resolved at run-build time by the
  orchestrator (`_attach_test_data`) and injected as `context["tables"]`.
  Static registries, edited by hand or by the assistant.
- **Graph edges mean "then"** (invariant #1). Inside one scenario, "before" is
  an upstream node and "after" is a downstream node. That is already
  sufficient for *read* probes once an adapter exists. What is missing is a
  way to say "and undo this when the scenario ends, whatever happened", and a
  way to run something once before or after the *whole run*.
- **Phase 1 health** (`WorkerEngine`, `health_check()` on every adapter) and
  `teardown()` (`disconnect_all`) are the only run-level lifecycle points.
  There is no run-level setup or cleanup.

Code steps cannot fill the gap: `run_code` executes user snippets in a network
namespace with no egress (`PAYPROBE_CODE_SANDBOX`, ATLAS §7), so a code node
can never reach a database by design. Database access must be an adapter.

### What we can reuse

- **asyncpg is already a hard worker dependency** (`packages/worker/pyproject.toml`
  `dependencies`), and the repo has two asyncpg pool patterns to copy
  (`scenario-service/api/pg_store.py`, `agent-hub/agent_hub/store.py`).
  `sqlite3` is stdlib. Neither engine adds an install step. Oracle, MSSQL and
  MySQL drivers are not installed anywhere today (`oracledb`, `aioodbc`,
  `aiomysql` all missing on 2026-09-24).
- **The connection model already carries the probe.** `DbConfig` in the
  portal, the `db_probe_*` allowlist entry, `extra="allow"` for engine-specific
  keys, the override matrix for per-environment values (invariant #7), and
  `SecretBox` encryption of `password` at rest (`password` is in
  `_SECRET_EXACT` in `payprobe_common/crypto.py`).
- **The variable resolver already digs into lists.** `${probe.response.rows[0].pan}`
  resolves today (`_dig` in `worker/engine/variables.py`), and `loop` nodes
  iterate a resolved list (`cfg["list"]` in `_loop_iterations`). Rows returned
  by a probe are consumable by every existing node kind with no resolver change.
- **The lazy registry and the mock contract.** `_try(_db_probe, ...)` is
  already in place; the mock adapter's canned-response map is the parity
  surface every example and pack runs against.
- **Blast-radius gating comes free.** A probe that fails phase 1 blocks every
  scenario that touches it (BLOCKED, not FAILED), so a dead database produces
  one clear signal instead of hundreds of misleading step failures.
- **The refusal precedent for load.** ADR-0009 refuses load runs against
  connections marked `external: true` with a 400 and a reason. The same shape
  fits a probe: a load run executes the *whole scenario* per transaction
  (`make_run_once` in `worker/load_worker.py`), so a probe step inside a load
  scenario would query the customer's database at the target TPS.
- **The diagnostics layer pattern.** `/diagnostics` already has a providers
  layer reporting credential-set / token-obtainable / reachable per provider
  connection; a databases layer is the same shape.
- **Secrets handling from ADR-0013 (landed 2026-09-25).** `is_secret_key` is
  parent-aware and knows the payment-crypto names; `mask_doc` / `merge_masked`
  give API-side masking and masked round-trip updates; the simulator store
  encrypts secret-named values at rest and exposes plaintext only through a
  runtime accessor; `${key.NAME}` resolves for simulator configs as well as
  scenarios. `dsn` joins `_SECRET_EXACT` beside those names, and a probe
  connection's `password` / `dsn` may be a `${key.NAME}` reference.

### Forces

- **Read-only must be a property of the connection, not a promise in a
  README.** The README says the adapter never writes. Invariant #6 says
  guardrails live in the tool layer, not the prompt; here the layer is the
  database session itself.
- **Schemas belong to the customer.** PayProbe cannot know the table that
  holds a transaction in a given core banking system. `query_transaction` must
  therefore be a *named query* whose SQL is connection data, the same way a
  provider pack's specifics are data (ADR-0009), and it must vary per
  environment through the override matrix (staging and production rarely
  share table names).
- **Writes need a reversibility story before they exist.** Invariant #2 makes
  every assistant write reversible from data. A seed row inserted into a
  customer database by a scenario has no journal today. Writes therefore ship
  only with a declared cleanup that the runner executes at scenario end
  regardless of outcome, and only on connections that opted in.
- **Rows are data the report will store.** Step responses land in `runs.db`,
  in WebSocket events and in reports. An unbounded `SELECT` would put a table
  into run history. Row caps, statement timeouts and column-name redaction
  are part of the adapter, not options.
- **A probe at load TPS is a denial of service.** Refuse by default, opt in
  explicitly, mirror the `external: true` rule.
- **Examples must stay executable without infrastructure.** `make test` runs
  every scenario under `examples/scenarios/` in mock mode. A real loopback
  proof needs a database that exists in CI with no service: SQLite in-memory,
  seeded by the connection itself.
- **Honesty about drivers.** The README names three engines; one is
  installable today. The ADR corrects the claim before it builds anything.

## Decision

Build the database probe as one worker adapter, `DBProbeAdapter`, registered
under the existing `db_probe_core` and `db_probe_switch` keys, with pluggable
engines behind one interface: `postgresql` (asyncpg, in-tree) and `sqlite`
(stdlib, run in a thread) ship in this ADR; `oracle`, `mssql` and `mysql`
become optional extras with a stated driver each and are **not** built here.

The adapter is **read-only by construction**: every statement runs inside a
read-only transaction (`SET TRANSACTION READ ONLY` on PostgreSQL, `PRAGMA
query_only` on SQLite), so a write is refused by the database, not by a regex.
A statement prefilter runs first only to give a readable error. A connection
opts into writes with `writes: true`, which is a per-environment value in the
override matrix, so a production probe stays read-only while its staging
override may seed data.

Actions are settled to a small vocabulary. `query` takes SQL and parameters
and returns rows. Any other action name is a **named query** looked up in the
connection's `queries` map, so `query_transaction` keeps working for the
catalog, the assistant, the pack and the mock, with the SQL supplied by the
connection. `execute` (writes only) takes SQL and parameters and an optional
`cleanup` statement that the runner queues and executes when the scenario
ends, in reverse order, whatever the outcome. The response flattens the first
row's columns to the top level, so `assert status eq APPROVED` on
`query_transaction` means what every existing pack and example already says,
and adds `rows`, `row_count`, `columns` and `truncated`.

Run-level fixtures come last and flag-gated: a run request may name `before`
and `after` scenarios. `before` fixtures run once after phase 1 and block the
run if they fail; `after` fixtures run once after phase 3, also on cancel or
failure, and can only annotate a verdict, never flip it. Their results appear
in the report as fixture entries.

When no probe is configured, nothing changes.

## Target shapes

### Connection (scenario-service record, worker-shaped config)

```jsonc
{
  "name": "core_db",
  "adapter": "db_probe_core",
  "engine": "postgresql",              // postgresql | sqlite (oracle, mssql, mysql: extras, later)
  "host": "db-core.internal", "port": 5432,
  "dbname": "corebank", "user": "payprobe_ro", "password": "…",   // password: SecretBox at rest, masked on read
  "writes": false,                     // default; true opts this connection into `execute`
  "pool_size": 2,                      // capped at 16 regardless of connection_budget
  "statement_timeout_ms": 5000,
  "max_rows": 100,
  "queries": {                         // named queries: schema knowledge is connection data
    "query_transaction": {
      "sql": "SELECT status, amount, rrn FROM txn WHERE rrn = $1",
      "params": ["rrn"]                // payload keys bound positionally
    },
    "query_balance": {
      "sql": "SELECT balance, status FROM account WHERE id = $1",
      "params": ["account_id"]
    }
  },
  "environment_overrides": {
    "staging": {"host": "db-staging", "writes": true,
                "queries": {"query_transaction": {"sql": "SELECT … FROM txn_stg WHERE rrn = $1", "params": ["rrn"]}}}
  }
}
```

SQLite for examples and tests: `{"engine": "sqlite", "dsn": ":memory:",
"init_sql": ["CREATE TABLE txn (...)", "INSERT INTO txn VALUES (...)"]}`.
`init_sql` runs once at `connect()` and only on SQLite; it is what makes an
example self-contained. `dsn` is added to `_SECRET_EXACT` because a DSN
commonly embeds a password.

### Step payloads and responses

```jsonc
// read, named query (works today in every pack and example)
{"target": "db_probe_core", "action": "query_transaction", "payload": {"rrn": "${auth.response.rrn}"},
 "assertions": [{"field": "status", "operator": "eq", "expected": "APPROVED"},
                {"field": "row_count", "operator": "eq", "expected": 1}]}

// read, ad hoc SQL (parameters only; no string formatting, ever)
{"action": "query", "payload": {"sql": "SELECT pan, expiry FROM test_cards WHERE active = $1 LIMIT 5", "params": [true]}}
// downstream: ${cards.response.rows[0].pan}, or a loop node over ${cards.response.rows}

// write with declared cleanup (connection must carry writes: true)
{"action": "execute", "payload": {
   "sql": "INSERT INTO account (id, balance) VALUES ($1, $2)", "params": ["${vars.acct}", 50000],
   "cleanup": {"sql": "DELETE FROM account WHERE id = $1", "params": ["${vars.acct}"]}}}
```

Response shape for reads:

```jsonc
{"status": "APPROVED", "amount": 10000, "rrn": "…",     // first row, flattened
 "rows": [{"status": "APPROVED", "amount": 10000, "rrn": "…"}],
 "row_count": 1, "columns": ["status", "amount", "rrn"], "truncated": false}
```

Reserved keys (`rows`, `row_count`, `columns`, `truncated`, `rows_affected`,
`duration_ms`) win over a same-named column. A column whose name is
secret-like (`is_secret_key`) is masked to `***` before the response leaves
the adapter. Writes return `{"rows_affected": n, "cleanup_registered": true|false}`.

### Cleanup steps (runner)

A step payload carrying `cleanup` registers `(target, "execute", cleanup)`
on the `ScenarioRunner`'s scenario-scoped cleanup list when the step succeeds.
After the graph ends (pass, fail, `stop_on_failure`, error, or cancel) the
runner executes the list in reverse order and emits each as a step outcome
with `action: "cleanup"`. A cleanup failure marks the scenario's result with
a `cleanup_failed` note and never changes the scenario verdict. This is
generic: an `http` create-then-delete pair may use it too.

### Run-level fixtures (flag-gated)

```jsonc
POST /runs  {"scenario_ids": [...], "fixtures": {"before": ["sc-seed-accounts"], "after": ["sc-verify-ledger", "sc-purge"]}}
```

Behind `PAYPROBE_RUN_FIXTURES=1` (default `0` until phase 4). Fixture
scenarios are ordinary scenarios (any node kinds, any targets). `before`
fixtures run sequentially after phase 1; a failure blocks the run with the
same BLOCKED semantics as a failed health check. `after` fixtures run
sequentially after phase 3 in a `finally`, including after cancel. The
report (`report_service`) lists them under "Fixtures" and the sign-off
provenance records their outcomes; they do not participate in gate
percentages.

### Load runs

A load request whose scenario touches a `db_probe_*` target is refused with
400 and a reason unless the probe connection carries `load_ok: true`.
Mirrors the `external: true` rule of ADR-0009 and lives in the same place in
`load_coordinator`.

### Diagnostics

`/diagnostics` gains a `databases` layer: per probe connection, `reachable`,
`read_only_enforced` (the session refused a probe write), `writes_enabled`,
`named_queries` (count and any that fail `EXPLAIN`).

## Options Considered

### Option A. One adapter, pluggable engines, named queries as data, DB-enforced read-only, cleanup steps, run fixtures (chosen)

**Pros:** builds what nine portal files, five service modules and the README
already promise, under the keys they already use; zero new dependencies for
the two engines that ship; read-only is enforced by the database session;
schema knowledge is connection data and varies per environment through the
existing override matrix; every existing pack, example, assistant output and
mock canned reply keeps its shape; cleanup and fixtures are generic runner and
orchestrator features that other adapters benefit from.
**Cons:** three engines stay unbuilt and the README claim must be corrected;
cleanup steps add a runner concept; run fixtures add an orchestrator request
field and a report section; SQLite's read-only mode is a pragma on the
connection, so the adapter holds separate read and write connections.

### Option B. Open the code sandbox to database drivers

**Pros:** arbitrary logic; no adapter to maintain.
**Cons:** breaks the code-node isolation boundary (ATLAS §7, `strict` mode
exists precisely so `auto` cannot degrade silently); no read-only guarantee,
no row caps, no secret handling; every scenario re-implements the connection.
Rejected.

### Option C. Point the generic `mcp` adapter at a database MCP server

**Pros:** works today for reads with zero PayProbe code.
**Cons:** no read-only enforcement PayProbe controls; per-call sessions; row
caps, redaction and timeouts depend on a third-party server; the four
drifted action names stay unresolved; the phantom stays in the README.
Kept as an escape hatch for engines this ADR does not ship (an operator can
front Oracle with an MCP server until the extra lands). Not the answer.

### Option D. Delete the phantom

**Pros:** honest in one afternoon.
**Cons:** removes `pack_auth_then_settled`, the assistant's `settle` step and
the one example that closes the loop from authorization to persisted state;
the README row goes and with it the platform's claim to prove state, which is
the point of a digital twin. The README correction happens anyway in phase 0.
Rejected as the end state.

### Sub-decision: how read-only is enforced

- **(i) The database session (chosen).** `SET TRANSACTION READ ONLY` /
  `PRAGMA query_only`. A write fails inside the database with a clear error
  and cannot be bypassed by a cleverly shaped statement.
- (ii) A statement regex. Kept only as the friendly pre-check; `WITH … AS
  (INSERT …)` and `SELECT … INTO` are counter-examples to any regex.
- (iii) A read-only database role. Recommended in the docs, not relied on:
  PayProbe cannot verify what a role can do.

### Sub-decision: where schema knowledge lives

- **(i) Named queries on the connection, overridable per environment
  (chosen).** Matches invariant #7 and the ADR-0009 data pattern; the
  assistant and the catalog keep emitting `query_transaction` without knowing
  a schema.
- (ii) Builtin SQL per engine. Rejected: there is no universal transaction
  table.
- (iii) SQL in every step. Allowed for `query`, but a named query is the
  reusable form and the only one the assistant may emit (see Open questions).

## Trade-off Analysis

The decisive factor is that the platform already made the promise. Nine
portal files, the catalog, the assistant, a certification pack, three
examples and the README all assume the probe exists, and each has quietly
grown its own idea of what it does. Continuing without the adapter means
every new surface adds a fifth vocabulary; removing the surfaces removes the
one claim that separates a digital twin from a message pump. Building it
under the existing keys is the cheapest path that ends the drift, and the
two engines that ship cost no dependency.

Enforcing read-only in the session rather than in code is the same reasoning
as invariant #6: the model proposes, the registry is the bouncer. Here the
bouncer is the database.

Cleanup steps and run fixtures are the two places this ADR spends new
engine surface. Both are deliberately small (one list on the runner, one
optional field on the run request) and both are generic rather than
probe-specific, which is the "no parallel stacks" convention.

## Consequences

**Easier**

- A scenario proves state, not just a response code: authorization, then the
  row in the core database, then the balance, in one graph, with the
  existing assertion operators.
- Data-driven scenarios read their inputs from a database before execution
  (`rows` into a `loop` node) instead of hand-maintained pools.
- Seed-then-verify-then-clean flows are safe by construction: cleanup runs
  whatever happened, and writes exist only where an environment opted in.
- A dead or misconfigured database is one BLOCKED signal per run, not a wall
  of FAILED steps, and `/diagnostics` says why.
- The README row, the catalog page, the mock and the adapter agree on one
  vocabulary, guarded by a parity test.

**Harder / risks**

- **Rows in run history.** `max_rows` (default 100) and `statement_timeout_ms`
  (default 5000) bound it; `truncated: true` is explicit; reports render
  `row_count` and the first row, never the full list, in Go/No-Go mode.
- **Writes against a customer database.** Off by default per connection,
  per environment; every `execute` needs a `cleanup` unless the payload says
  `cleanup: null` explicitly (an intentional, recorded permanent write); the
  agent-hub approval gate already covers a `full`-mode agent starting such a
  run (invariant #10). Nothing here creates a write path the journal cannot
  see, because the journal is not involved: the run result *is* the record.
- **Flattening collisions.** A column named `rows` is shadowed. Reserved
  keys are documented and the adapter logs a warning at `connect()` when a
  named query's `EXPLAIN` output includes one.
- **SQLite read-only is per connection.** The adapter keeps a read
  connection with `query_only` on and, when `writes: true`, a second one
  without it. Tests assert a write over the read connection fails.
- **Three engines stay claims.** The README row becomes "PostgreSQL, SQLite
  (Oracle / MSSQL / MySQL as extras, not yet built)" in phase 0, before any
  code. `payprobe-external-positioning` rules apply.
- **Portal changes need a host build.** The `DbConfig` editor (writes toggle,
  named-queries table, `max_rows`), the catalog flip from planned to builtin,
  and the load-refusal message all require `npm run build` on the host and a
  click-through; the ADR is not Accepted before that.

**Revisit**

- If a probe is needed at load TPS for a real certification, `load_ok` is
  the opt-in; if that becomes common, rate-limit the probe inside the driver
  rather than widening the default.
- If run fixtures need to pass data into the run's scenarios (a seeded
  account id), see Open question 1; this ADR ships fixtures without cross-
  scenario context on purpose.

## Rollout / sequencing

Each phase leaves `make test` green and ships independently. Baseline before
phase 1: the full-suite count, and the tests that reference `db_probe`
(6 scenario-service files, `worker/tests/test_engine.py`, the examples suite).

**Phase 0. Truth first, no behaviour change.**
README adapter row corrected to what exists. A new test,
`test_adapter_surfaces_agree`, loads `_ALLOWED_ADAPTERS`
(scenario-service), the catalog `TargetSpec` targets and the worker
`ADAPTER_MAP` and fails on any adapter the service side offers that the
worker cannot instantiate unless it is listed in one explicit
`PLANNED_ADAPTERS` set. Today that set contains `db_probe_core` and
`db_probe_switch`; phase 1 empties it. This is the guard against the next
phantom. Rollback: none needed.

**Phase 1. The adapter, reads only.**
`packages/worker/adapters/db_probe/adapter.py` (`DBProbeAdapter`),
`engines/postgresql.py` (asyncpg pool, `SET TRANSACTION READ ONLY`),
`engines/sqlite.py` (stdlib in `asyncio.to_thread`, `PRAGMA query_only`,
`init_sql`). `query` and named queries, positional parameter binding only,
flattening, `max_rows`, `statement_timeout_ms`, column-name redaction,
`health_check` as `SELECT 1` under the timeout. Mock canned responses
aligned to the settled vocabulary (`query_transaction`, `query_balance`
return the flattened shape); the README under `adapters/db_probe/` and
the catalog `ActionSpec`s rewritten to match; `dsn` added to
`_SECRET_EXACT`. Tests: SQLite in-memory for everything, one PostgreSQL test
module gated on a reachable database exactly like the agent-hub suite
(skips loudly, never silently green). Loopback proof: an example scenario
`examples/scenarios/db_probe_settlement.json` on a SQLite connection seeded
by `init_sql`, asserting `status`, `amount` and `row_count`, running for real
inside `make test` without mock. Rollback: remove `adapter.py`; the registry
returns to skipping the import.

**Phase 2. Writes and cleanup.**
`writes: true`, the `execute` action, the write session (PostgreSQL: a
normal transaction; SQLite: the second connection), `cleanup` registration
in `ScenarioRunner` and its `finally` execution with `action: "cleanup"`
outcomes, `cleanup_failed` note on the scenario result. Load-run refusal in
`load_coordinator` with `load_ok` opt-in. `/diagnostics` databases layer.
Tests: seed, verify, cleanup on pass; cleanup still runs on fail, on
`stop_on_failure` and on cancel; a write on a `writes: false` connection is
refused by the session; a load run with a probe step is 400 without
`load_ok`. Rollback: `writes` defaults false, so removing the feature is
removing the flag handling.

**Phase 3. Run fixtures, flag OFF.**
`fixtures.before` / `fixtures.after` on the run request behind
`PAYPROBE_RUN_FIXTURES`; engine hook points after phase 1 and after phase 3
(in `finally`); BLOCKED propagation from a failed `before`; report_service
"Fixtures" section and provenance entries; MCP `start_run` gains the field
(regenerate the portal catalog, `gen_catalog.py`). Tests: before-failure
blocks; after runs on cancel; fixtures absent from gate percentages.

**Phase 4. Portal, docs, flip.**
`DbConfig` editor (engine, writes toggle, named-queries table, `max_rows`,
`load_ok`), catalog page `availability: "builtin"`, run-start dialog fixture
pickers, load refusal message. Host `npm run build` and click-through.
`docs/adapters/db-probe.md` (how-to: read-only role, named queries, seed and
clean, fixtures), `docs/operations/configuration.md` rows for the two flags,
docs/README.md table link, `payprobe-config-and-flags` and
`payprobe-run-and-operate` skills, ATLAS §13. After one real-environment run
against a real PostgreSQL (the compose `postgres` counts), flip
`PAYPROBE_RUN_FIXTURES` default to `1`, keep the opt-out, and set this ADR to
Accepted.

**Deferred, recorded on purpose:** Oracle (`oracledb` thin async), MSSQL
(`aioodbc`, needs an ODBC driver in the image), MySQL (`aiomysql`) as
optional extras, each its own small PR against the engine interface;
DB-sourced test-data pools at run-build time (the orchestrator would then
hold database credentials, which changes the security model and needs its
own decision); table snapshot-and-diff before and after a run (a Chronoscope
style artifact; attractive, not designed here).

## Action Items

- [x] Record baselines: `make test` count; tests referencing `db_probe`.
- [x] Phase 0: README row; `test_adapter_surfaces_agree` with
      `PLANNED_ADAPTERS = {"db_probe_core", "db_probe_switch"}`.
- [x] Phase 1: `adapters/db_probe/{adapter,engines/postgresql,engines/sqlite}.py`;
      registry unchanged (import now succeeds); mock canned replies aligned;
      `catalog.py` `ActionSpec`s; `adapters/db_probe/README.md`; `dsn` in
      `_SECRET_EXACT`; `test_db_probe_sqlite.py`, `test_db_probe_postgres.py`
      (gated); `examples/scenarios/db_probe_settlement.json` +
      `examples/connections/bundled.json` SQLite example connection;
      `PLANNED_ADAPTERS` emptied.
- [ ] Phase 2: `writes`, `execute`, cleanup list in `ScenarioRunner`,
      `load_coordinator` refusal + `load_ok`, diagnostics databases layer,
      tests listed above.
- [ ] Phase 3: `fixtures` on the run request, engine hook points,
      report_service section, MCP registry field + `gen_catalog.py`, flag
      `PAYPROBE_RUN_FIXTURES` default `0`.
- [ ] Phase 4: portal editor and catalog flip (host build + click-through),
      `docs/adapters/db-probe.md`, configuration rows, skills, ATLAS §13,
      real-environment run, flag default `1`, status Accepted.

## Open questions

1. **Should `before` fixtures pass data to the run's scenarios?** A seeded
   account id is useless if no scenario can reference it. Proposal: the last
   step's response of each `before` fixture is published to every scenario
   as `${fixtures.<scenario_id>.response.*}`, read-only, resolved by the
   orchestrator into `variables` before the worker sees the scenarios. This
   ADR ships fixtures without it; decide before phase 3 or defer to a
   follow-up.
2. **Two probe types or one.** `db_probe_core` and `db_probe_switch` are
   separate default-connection types (`_target_type_key`), which lets an
   author bind "the core database" and "the switch database" without naming
   connections in steps. Proposal: keep both keys, one class; do not add a
   third until a pack needs it.
3. **May the assistant emit ad hoc `query` SQL?** Proposal: no. The assistant
   and agents emit named queries only; `query` with raw SQL is authored by a
   human in the constructor. Enforced in the tool layer (invariant #9), not
   the prompt.
4. **Permanent writes.** `cleanup: null` marks an intentional permanent
   write. Should it also require `writes: "permanent"` on the connection
   rather than plain `true`? Proposal: yes, a second opt-in, because
   "seed and clean" and "mutate for real" are different risk classes.
5. **Oracle driver.** `oracledb` thin mode needs no client libraries and
   supports asyncio since 2.0; confirm the licence posture and image size
   before adding the extra.

## Notes

**Out of scope, recorded on purpose:** databases *behind simulators* (the
PSP and ISO simulators keep their own in-memory state; a persistence layer
for simulators is a different decision); ORM or schema migrations of any
kind; DB-sourced pools and snapshot diffs (deferred above); agent-hub tools
that query customer databases directly (agents reach a probe only by starting
a run, under invariants #9 and #10).

**Depends on:** nothing unbuilt. Independent of ADR-0011; reuses ADR-0013's
secrets helpers. **Blocks:** any
scheme certification pack that must prove settlement state, and the
"digital twin proves state" claim in `payprobe-external-positioning`.
