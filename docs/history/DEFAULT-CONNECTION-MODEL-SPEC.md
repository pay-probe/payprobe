# Default-connection model — spec

**Status: COMPLETE — Phases A–F all BUILT (2026-06-26/27).** Default-connection
resolution is on by default (opt-out `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION=0`); the
env-adapter fallback is kept as graceful degradation. Drafted 2026-06-26.
**Builds on:** `CONNECTION-ENV-MIGRATION-PLAN.md` (connection-wins is now the default).
**Decision (David):** a connection *is* the adapter instance. A step always resolves to a
connection; the environment only overrides a connection's parameters and never defines
adapters of its own. An unbound step falls back to a **default connection** for its
adapter type. Adapter type alone is enough to pick the right connection.

## Principle

```
Step (action + payload + optional connection)
   → a Connection  (= the adapter instance: adapter, protocol, host/port, framing, …)
       ↑ Environment supplies per-env parameter overrides (the matrix). No adapters block.
   ↳ if no connection chosen → the DEFAULT connection for the step's adapter type
Catalog = action vocabulary per adapter type (stays; not routable)
```

Two things go away: the environment's `adapters` block (env becomes a pure override/label),
and the idea that a bare catalog target resolves to an env-defined adapter. What replaces
the latter is a default *connection* per type — still an adapter instance, just the
designated one.

## Current state (grounded in code)

- A step carries `target` (catalog type, e.g. `tcp_iso8583`) and optional
  `config.connection`. The worker resolves the adapter by `registry.get(step.target)`
  (`worker/engine/engine.py`), looking `target` up in `env['adapters']`.
- `_attach_connections` (`orchestrator/api/main.py`) injects only the connections a step
  *references* via `config.connection`, and repoints that step's `target` to the
  connection name. A **bare-target step (no connection) resolves to the environment's
  inline adapter** of that name — this is the path being removed.
- Most existing scenarios are bare-target: `rest_pay`/`http`, `terminal_sim`, `hsm`,
  `db_probe_core` steps run with `config: {}` and rely on the env's default adapter.
- Builtin catalog targets needing a default connection: `tcp_iso8583` (tcp/iso8583),
  `http` (REST), `hsm` (tcp/header_echo or payshield), `db_probe_core` (db), plus
  `terminal_sim` and `grpc`. `ConnectionDraft` already holds full adapter config +
  `environment_overrides`.
- `mock` environments set `mode: mock`, which routes *every* target to `MockAdapter` in
  the registry **before** adapter resolution — so mock runs never need real connections.

## Target model

- **Connection** = adapter instance: `{adapter, protocol, host, port, framing, …}` +
  `environment_overrides` matrix + new `default: bool`.
- **Default connection** = the one connection flagged `default` for a given adapter type.
  Exactly one per type. Used whenever a step of that type names no connection.
- **Step** = unchanged shape. `config.connection` empty ⇒ resolve to the type's default
  connection (was: the env's inline adapter).
- **Environment** = name + (optionally) override values; **no `adapters` block**. Its
  per-env values live on connections' override matrices (already the single source).
- **Catalog** = unchanged: action/payload vocabulary per adapter type; drives the editor's
  action menu and the connection-list filter (`connectionsFor` already filters by family).

## Phases — each backward-compatible; the fallback is removed last

### Phase A — `default` flag on connections (additive, no behavior change) — **BUILT**

- Added `default: bool = False` to `ConnectionDraft` (`connection_store.py`).
- `type_key(adapter, protocol)` groups defaults: `tcp/iso8583` and `tcp/header_echo` are
  distinct slots, every other adapter is its own. **At most one default per type**, enforced
  by `_demote_other_defaults` in `upsert` — setting a new default *moves* it (last-write-wins)
  rather than rejecting, and an edit that omits `default` preserves the stored flag (sending
  `default=False` clears it). New reads: `get_default(adapter, protocol)` and `defaults()`.
- `default` is metadata, stripped from worker config everywhere connections are inlined
  (`_NON_ADAPTER_KEYS`/`_SKIP_CONN_KEYS` in orchestrator; `NON_ADAPTER_KEYS` in
  `env_migration.py`).
- Tests in `tests/test_connections.py`: round-trip + query, second-default demotes first,
  distinct types each keep a default, edit preserves/clears. Suites green (scenario-svc
  connections + env_migration 33; orchestrator connection/group 13).

### Phase B — seed default connections from env default-adapters (additive) — **BUILT**

- `plan_seed_defaults(environments, connections, catalog_targets)` /
  `apply_seed_defaults` in `env_migration.py`: for each **catalog-target-named** env
  adapter (`tcp_iso8583`, `http`, `hsm`, `db_probe_core`, …) creates one default
  connection per type — base from the first (sorted) env that defines it, other envs'
  differences folded into the override matrix (reuses `override_diff`). Instance-named
  adapters (`switch_visa`) are ignored. A type that already has a default is skipped
  (`skipped_existing`). Adapters that can't be a connection (mock-only `terminal_sim`)
  are reported in `failed`, never raised.
- Endpoint `POST /admin/migrate/seed-default-connections?apply=false` (dry-run default),
  catalog targets read from `catalog.merged()`. Idempotent — a second run skips the
  now-existing defaults. Environments untouched; both sources still exist.
- Tests in `tests/test_env_migration.py` (one-per-type + matrix, ignores instances, skips
  existing default, apply creates + idempotent); scenario-svc env_migration + connections
  suites green (37). **Live dry-run:** seeds exactly one default — `tcp_iso8583` from
  `local-env`, no overrides.

### Phase C — resolution fallback to the default connection (flag-gated) — **BUILT**

- `_attach_connections` (`orchestrator/api/main.py`) now, when
  `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION` is on, resolves an **unbound action step**
  (no `config.connection`) to the default connection for its adapter type: indexes
  defaults by `_conn_type_key(adapter, protocol)`, maps the step's catalog target to a
  type via `_target_type_key` (hsm→tcp/header_echo, grpc, http/rest_pay→http, db_probe_*,
  payshield, else tcp/iso8583), injects the default's `base ⊕ override[env]`
  (`_connection_effective`) and repoints the step. By TYPE, not name — a default named
  `primary_switch` serves a bare `tcp_iso8583` step.
- Flag default **off** (opt-in like the Phase-4 flip). No default for a type ⇒ step left
  as-is (env-adapter fallback). The early-return was relaxed so unbound-only runs still
  resolve. `_NON_ADAPTER_KEYS`/effective-config logic factored into module helpers.
- `mock` untouched (short-circuits before resolution). Chosen connections still win.
- Tests in `test_connections_wiring.py`: unbound→type default (by type), env override
  applied, no-default→fallback, flag-off→unchanged, bound step keeps its connection.
  Orchestrator suite 193 pass (5 unrelated pre-existing fails: jwt module, payShield crypto).

### Phase D — editor surfaces the default (portal) — **BUILT + AOT-verified**

- Step editor empty connection option now reads **"Default {type} connection"** (was
  "…adapter"); helper text updated to "the one marked default for this adapter type"
  (`constructor.component.html`).
- Connection editor: a **"Default for this adapter type"** checkbox (`d.default`) next to
  Direction; `Connection.default` added to the model + `blankConnection` +
  `fromAdapterConfig` (all branches) + `connections.service.save` body (kept out of
  `toAdapterConfig`, like `mode`/`disabled`). The one-per-type move is enforced
  server-side (Phase A), so the UI just sets the flag.
- **Verified with a real `ng build`** (clean copy in `/tmp/pbuild`, `npm ci` + AOT):
  EXIT=0, full bundle, **zero errors** — only pre-existing NG8113 unused-import warnings
  in unrelated components.

### Phase E — empty the environments' adapters block (registry-level) — **BUILT**

- `plan_slim` (`env_migration.py`) gained an optional `catalog_targets` arg. Beyond the
  existing name-matched case, it now also removes a **standalone catalog-target** env
  adapter (e.g. `tcp_iso8583`) when its adapter type has a **default connection** and the
  env adapter is collision-clean vs what that default resolves to under the env — covering
  the case where the default is named differently from the target (`primary_switch`).
  Differing entries are `blocked` (with `owned_by_default`); no `catalog_targets` ⇒ the
  case is skipped (existing behaviour preserved). `apply_slim` unchanged.
- Endpoint `POST /admin/migrate/slim-environments` now passes `catalog.merged()` targets.
- Tests: removable via differently-named default, blocked when default differs, kept when
  no type default. scenario-svc env_migration + connections suites green (40).
- Note: a no-op until defaults are seeded + resolution enabled (correct ordering —
  seed → resolve → slim).

### Phase F — flip default on; keep the fallback as graceful degradation — **BUILT**

- `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION` now defaults to **on** (opt-out:
  `=0`/`false`/`no`). The default-connection model is the standing behaviour.
- **Deviation from the original plan, on purpose:** the env-adapter fallback is *kept*, not
  hard-removed. A bare-target step whose type has no default still degrades to the env
  adapter rather than failing — so flipping the default on is safe even where defaults
  aren't seeded yet (verified no-op on the live registry, which has no defaults). Once
  every type has a default and Phase E has slimmed the env adapters, the fallback is inert
  dead code. This is strictly safer than "remove fallback + add a fail-fast validation,"
  and needs no validation gate.
- Tests updated: default-on resolves without a flag; opt-out (`=0`) leaves unbound steps;
  no-type-default falls back to the env adapter. Orchestrator suite 193 pass (5 unrelated
  pre-existing fails).

## Backward compatibility

At every phase before F, old scenarios run unchanged: a bare-target step either hits the
env adapter (flag off / no default) or the default connection (flag on). Nothing about
saved scenarios or step shape changes. The two irreversible-feeling steps (E emptying env
files, F removing the fallback) come last and only after C is proven in a real environment.

## Test matrix

- Phase A: one-default-per-type accepted; second rejected/moved; `default` round-trips.
- Phase B: seeding is idempotent; per-env diffs land in the default connection's matrix;
  secrets encrypt at rest; dry-run writes nothing.
- Phase C (the important one): bare-target step → default connection under the active env
  (flag on); → env adapter when no default or flag off; chosen connection still wins;
  `mock` still short-circuits.
- Phase E: slim removes default-connection-owned env adapters; leaves budget; blocks if a
  type still differs.
- Phase F: run fails clearly when a used type has no default; opt-out flag restores legacy.
- Regression: existing connection/env/group/step-env suites stay green.

## Risks / rollback

- Order is load-bearing: **flag + seed defaults → resolve → empty envs → remove fallback.**
  Resolving before defaults exist, or removing the fallback before envs are emptied, breaks
  bare-target runs.
- Phases A–C are additive/flag-gated and revert with a one-line flag flip. E (env edits)
  and F (fallback removal) are the committing steps.
- The mandatory invariant for F: a default connection exists for every adapter type any
  scenario targets. The Phase-A validation plus Phase-B seeding guarantee it; F adds a
  startup/`validate_scenario` check that surfaces a missing default before a run.

## Examples modernized (2026-06-27)

The bundled example **environments** demonstrated the old pattern (named adapter
instances defined inline). They've been converted to the connection model:

- New `ConnectionStore.seed_from_dir` (mirrors the env seed) + `CONN_SEED_DIR`, wired in
  the lifespan; reads `{"connections": {...}}` or flat per-file docs. Compose now mounts
  `examples/environments → /environments` and `examples/connections → /connections` into
  scenario-service (the env mount was previously missing — bundled envs only surfaced via
  the orchestrator's file merge, not the registry seed).
- `examples/connections/bundled.json` — 12 connections converted from the example envs:
  `switch_visa`/`switch_mc`/`hsm_primary`/`hsm_backup` (multi-instance, with
  `adapter_defaults` inlined + `extends` preserved), the three gRPC instances, `http_live`,
  `payshield_hsm`, and `prod_http`/`prod_switch`/`prod_db_probe`. None is flagged `default`
  (per-deployment choice).
- `examples/environments/*.json` slimmed to selectors (empty `adapters`), keeping
  `connection_budget` + descriptions. `production-template` retains only its non-connection
  PKCS#11 `hsm` inline; `mock` unchanged.
- Tests: `seed_from_dir` (map + flat forms, idempotent); the real `bundled.json` loads and
  every connection validates. scenario-svc 262 / orchestrator 193 green (pre-existing
  unrelated fails aside).

## Why this is worth it

It collapses three routing paths (env default adapter, named connection, raw target) into
one: a step resolves to a connection, always. The environment stops being a place that
defines adapters and becomes purely the per-env value selector — the exact end-state the
connection/env migration was moving toward, now with no awkward bare-target special case.
