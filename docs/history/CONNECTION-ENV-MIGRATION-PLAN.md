# Connection / Environment migration plan

**Status: COMPLETE (2026-06-26).** All phases built; connection-wins is now the
default precedence (escape hatch `PAYPROBE_CONNECTION_OVERRIDE_WINS=0` restores
legacy). Live registry was verified a no-op (`safe_to_flip`), so finalizing the
default carried no data risk. Drafted 2026-06-26.
**Decision being executed:** the agreed connection/env model — separate a connection's
*shape* from its *values*; an Environment is a lean selector, not an adapter owner.

## The one invariant

A flow names a **Connection**. The **Environment** only chooses *which column of values*
that connection resolves to. Network host/port/creds belong on the connection's
`environment_overrides` matrix, never re-declared per environment.

```
effective = connection.shape  ⊕  connection.environment_overrides[env]
```

The goal of this migration is to make that statement true everywhere, with no run
breaking on the way.

---

## Current state (grounded in code)

- **Connections** already carry the matrix: `ConnectionDraft.environment_overrides:
  dict[str, dict]` — `packages/scenario-service/api/connection_store.py:79`. Single
  source of truth, editable from both the Connection and Environment editors.
- **Environments still carry host/port directly** in their `adapters` block, e.g.
  `examples/environments/multi-instance.json` (`switch_visa.host/port`, `hsm_primary`)
  and `examples/environments/production-template.json` (`switch.host/port`). This is the
  duplication we are removing.
- **Precedence is currently backwards.** `_attach_connections`
  (`packages/orchestrator/api/main.py:446`) does
  `adapters.setdefault(name, base)` — so when an environment *and* a connection both
  define the same adapter key, **the environment wins and the connection's overrides are
  silently ignored**. `_NON_ADAPTER_KEYS = {"name","environments","environment_overrides"}`
  (line 480) already keeps the matrix out of the worker config; the per-env override is
  merged at lines 488–490.
- Resolution order in `_resolve_run` (line 820): `_attach_connections` → `_attach_groups`
  → `_attach_step_environments`. Only adapters a step actually references are pulled in.
- **Not every env adapter is a connection.** `terminal_sim`, `hsm` (crypto/PKCS11), and
  `db_probe_*` are non-connection adapters. These legitimately stay in
  `environments[].adapters`. The migration touches *only* network-connection entries.

---

## End state

- Scenario editor exposes only **Environment** (top) + **Connection** (per step).
  Adapter type and host/port disappear from the authoring surface.
- `environments[].adapters` holds **only non-connection adapters** (mock terminal sim,
  crypto/code tools, db probes).
- For any connection-backed target, the connection override matrix is the single source
  of per-env values, and the connection override **wins** over a stray env adapter of the
  same name.

---

## Status of this plan

- **Phases 0, 1, and 3 are BUILT** (`packages/scenario-service/api/env_migration.py`):
  - Phase 0/1: `classify_adapters`, `plan_backfill`/`apply_backfill`; endpoint
    `POST /admin/migrate/connection-overrides` (dry-run default).
  - Phase 3: `find_collisions` + `effective_under_env`; endpoint
    `GET /admin/migrate/collisions` returning `safe_to_flip`.
  - Tests `tests/test_env_migration.py` — 16 green; scenario-service suite 243 pass
    (3 pre-existing `No module named worker` cross-package failures, unrelated).
- **Phase 4 is BUILT** (flag-gated, default OFF): `_attach_connections`
  (`packages/orchestrator/api/main.py`) now flips to "connection wins over inline env
  adapter" when `PAYPROBE_CONNECTION_OVERRIDE_WINS` is set; default keeps legacy behaviour
  so landing it changes nothing until you opt in. Standalone (non-connection) env adapters
  are never touched. Tests `orchestrator/tests/test_connections_wiring.py` cover both flag
  states + standalone-untouched + full-matrix resolution (27 pass across the env suites).
- **Phase 5 is BUILT** (registry-level, not example-file editing): `plan_slim`/`apply_slim`
  in `env_migration.py` + endpoint `POST /admin/migrate/slim-environments` (dry-run
  default). Removes a connection-backed adapter entry from an environment only when it is
  collision-clean under that env; standalone adapters are kept; unsafe ones are reported
  under `blocked`. End-to-end test proves backfill → clean collisions → slim leaves the env
  with only standalone adapters and the matrix as the single source. (Slimming the bundled
  `examples/environments/*.json` is intentionally NOT done — they have no backing
  connections, so they stay self-contained; real slimming runs against the live registry.)
- **Phase 2 is BUILT** (portal, compile-parity only): the connection editor's per-env
  override block now shows a read-only **base → effective** preview
  (`effectiveRows(env)` in `connections.component.ts`, `.eff` table in the template),
  computed as `toAdapterConfig(draft) ⊕ override` — the same merge the runtime does, so you
  can eyeball each env before flipping. TS adds zero new errors beyond the known repo-wide
  `@angular/common`/`rxjs` type cascade; needs a real `ng build` + click-test to confirm
  the template renders. The Environment editor's mirror view is a future nicety.

- **Cutover sequence:** run Phase 1 backfill (`apply=true`) → confirm
  `GET /admin/migrate/collisions` is `safe_to_flip` → set
  `PAYPROBE_CONNECTION_OVERRIDE_WINS=1` → run Phase 5 slim (`apply=true`).
- The **Environment editor** now carries the same read-only base → effective preview per
  connection (`effectiveRowsForConn` in `environments.component.ts`), so the inheritance
  view is present on both surfaces.
- **DONE — connection-wins is now the default.** `_CONNECTION_OVERRIDE_WINS` reads
  `PAYPROBE_CONNECTION_OVERRIDE_WINS` defaulting to ON; set it to `0`/`false`/`no` to opt
  back to legacy env-wins. Tests updated:
  `test_connection_wins_over_inline_env_adapter_by_default` (no flag) +
  `test_legacy_env_adapter_wins_when_opted_out` (escape hatch). Orchestrator
  connection/env/group/step-env suites green (34 pass).
- **All phases implemented and finalized.** Remaining is operational only: a real
  `npm ci` + `ng build` + click-test of the portal UIs (blocked solely by this sandbox's
  corrupt `node_modules`; TS adds zero new errors), and deploying the built services.

## Phases

Each phase ships independently and leaves all suites green. The risky precedence flip is
**last**, after the data has actually moved.

### Phase 0 — Classify (no behavior change)

Add one read-only helper that, given a run's env + referenced connections, partitions
`env['adapters']` keys into:

- **connection-backed** — key matches a registered connection name, and
- **standalone** — everything else (terminal_sim, hsm, db_probe_*).

This is the lens every later phase uses ("is this entry a connection?"). Expose it as a
pure function in the orchestrator and reuse it in the backfill and the collision check.
No endpoint or file changes. Unit-test the partition.

### Phase 1 — Backfill (additive, non-destructive)

Move per-env values **into** the matrix without deleting anything from the env files yet.

- A one-shot migration routine (`scripts/migrate_env_to_overrides.py` or a guarded
  `POST /admin/migrate/connection-overrides` on scenario-service) that, for each bundled
  + registry environment E and each connection-backed adapter key K in `E.adapters`:
  writes `connection[K].environment_overrides[E] = { differing keys vs connection base }`.
  Only keys that **differ** from the connection's base are stored, so the matrix stays
  diff-shaped, not a full copy.
- If connection K does **not** exist yet, it is **reported, not auto-created**. Deciding
  which env is the "base" for a brand-new connection is ambiguous and not safely
  reversible, so the plan flags any unregistered, network-shaped env adapter (has
  `host`/`port`/`base_url`) under `needs_connection` and leaves it untouched. Registering
  it is a deliberate human step; re-running the backfill then picks it up.
- **Idempotent**: re-running produces no further change. Dry-run mode prints the diff it
  would write. Secrets ride the existing SecretBox path (`encrypt_doc`), never logged.
- Leaves `environments[].adapters` untouched. After this phase, both sources exist; env
  still wins at runtime (precedence not yet flipped), so behavior is unchanged.

### Phase 2 — Show inheritance (read-only safety net)

Before changing precedence, make the truth visible so you can eyeball the backfill:

- Both editors render **base → +env override → effective** for each connection (the
  Connection editor already has the override rows; add the computed "effective under env
  X" preview; mirror it in the Environment editor's per-connection section).
- A `GET /connections/{name}/effective?env=E` (or client-side compose) returns the merged
  result `_attach_connections` would produce — the same `{**base, **overrides}` math
  (main.py:490), so the UI and runtime agree.

### Phase 3 — Collision validation

Add a validator (surfaced in `validate_scenario` / a startup lint) that flags any
environment whose `adapters` block still defines a **connection-backed** key with
host/port — i.e. a value that *will* change meaning when precedence flips. Output: the
env, the key, and base-vs-env diff. Green = safe to flip.

### Phase 4 — Flip precedence (the actual switch)

Only after Phases 1–3 are clean for every env:

- In `_attach_connections` change `adapters.setdefault(name, base)` →
  assign so the **connection (base ⊕ override) wins** over a pre-existing env adapter of
  the same name. Keep `setdefault` semantics only for **standalone** adapters (Phase 0
  partition) so non-connection env adapters are still respected.
- Concretely: if `name` is connection-backed, `adapters[name] = base` (override the env);
  else leave the env's entry. Update the docstring (lines 449–460) which currently
  promises the opposite ("an adapter the environment already defines explicitly wins").
- This is the single line of real behavior change. Gate it behind the green collision
  check so it can't land while duplicated values still disagree.

### Phase 5 — Cleanup

Now that the matrix wins and is authoritative:

- Strip migrated host/port (and other connection params) out of
  `examples/environments/*.json` `adapters` blocks, leaving only non-connection adapters
  and `connection_budget`. `production-template.json`'s `switch`/`http`/`core_banking`
  network entries move to connection overrides; `terminal_sim`/`hsm`/`db_probe_core` stay.
- Re-seed: bundled env seeding (`ENV_SEED_DIR`) now imports the slimmed files.
- Remove the per-step `environment_override` authoring path if still desired (already
  removed from UI per the step-env-override work; field kept for round-trip).

---

## Test matrix

- **Phase 0:** unit — partition correctly splits a mixed env (switch=connection,
  terminal_sim=standalone).
- **Phase 1:** migration is idempotent; only-diff keys stored; secrets encrypted at rest;
  dry-run = no writes; creating a missing connection from an env block.
- **Phase 4 (the important one):** extend `orchestrator/tests/test_connections_wiring.py`
  — same connection defined in BOTH env and matrix: assert the **connection override now
  wins** (was: env wins); a **standalone** env adapter (terminal_sim) is still respected;
  a connection with no override falls back to base; unknown env → base + metadata stripped.
- **Regression:** `test_step_environments_wiring.py`, `test_groups.py`, and the worker
  engine env-override test stay green. Run suites per-package (known sys.path collision
  if worker + scenario-service share a process).
- **End-to-end:** run `scn-47cd54b1` (RestPay) under `mock` and a second env; assert it
  hits the right host purely from the matrix, with the env `adapters` block slimmed.

---

## Risk / rollback

- The only irreversible-feeling step is Phase 5 (deleting values from env files). Don't do
  it until Phase 4 has run green in a real environment. Phases 1–3 are purely additive and
  reversible.
- Rollback for Phase 4 is a one-line revert (`setdefault` restored); because Phase 1 left
  env `adapters` intact, the old precedence still has its data to fall back on until
  Phase 5.
- Order is load-bearing: **backfill before flip, flip before cleanup.** Flipping before
  backfill would make empty/stale connection bases win and break runs.
