# Scope: extend Connections to REST / payShield / DB adapters (Bucket B)

**Status:** Phase X1 BUILT + Phase X2 BUILT (2026-06-26, X2 compile-parity only).

> **Phase X2 done — connection editor now has per-adapter forms (compile-parity).**
> - `connection.models.ts`: `AdapterImpl` widened to include `http`/`payshield`/
>   `db_probe_core`/`db_probe_switch`; new `RestConfig`/`PayShieldConfig`/`DbConfig`
>   interfaces + `DEFAULT_*` + `blankConnection` init; `toAdapterConfig` emitters
>   (`restToAdapterConfig`/`payshieldToAdapterConfig`/`dbToAdapterConfig`) and matching
>   `fromAdapterConfig` branches.
> - `connections.component.html`: adapter `<select>` gained 4 options + 3 form blocks
>   (REST: base_url/auth/api_key/basic creds/timeout; payShield: host/port/header/timeout;
>   DB: engine/host/port/dbname/user/password).
> - `tsc -p tsconfig.app.json`: `connection.models.ts` has **zero** errors; the only
>   component error is the pre-existing rxjs `.subscribe` cascade (unchanged). The HTML
>   templates can't be AOT-verified in the sandbox (corrupt portal `node_modules` — Angular
>   packages are sourcemap-only), so **needs a real `npm ci` + `ng build` + click-test**.



> **Phase X1 done — REST/payShield/DB are now registerable connections.**
> - Widened `_ALLOWED_ADAPTERS` in `connection_store.py` to `{tcp, grpc, http,
>   payshield, hsm_client, db_probe_core, db_probe_switch}`; `protocol` is no longer
>   forced onto non-tcp adapters (model stays `extra="allow"`, so base_url / header /
>   engine etc. persist with no schema change). Secrets (api_key/password) still encrypt
>   at rest.
> - Made `POST /connections/test` generic: host/port guard for host/port adapters, then
>   build the adapter via `ADAPTER_MAP` and `connect()` + `health_check()` — works for
>   every registered adapter, not just TCP/gRPC.
> - Tests: `scenario-service/tests/test_connections.py` (REST/payShield/DB round-trip +
>   REST secret-at-rest) and `orchestrator/tests/test_connection_test_endpoint.py`
>   (unknown adapter, REST skips host/port guard, payShield still requires it). Suites:
>   scenario-service 249 pass, orchestrator connection suites 18 pass.
> - **Effect:** REST/payShield/DB targets can be created as connections via the API / MCP
>   `create_connection` today and flow through the whole migration (backfill → collisions →
>   flip → slim). Bucket B can collapse into Bucket A without waiting on the editor UI.

---

_Original scoping below._


**Why:** Today only `tcp` and `grpc` targets can be registered as Connections, so the
connection/env migration (`CONNECTION-ENV-MIGRATION-PLAN.md`) can't manage REST (`http`),
payShield (`payshield`), or DB-probe (`db_probe_*`) endpoints — they're stuck as inline
env adapters ("Bucket B"). This scopes what it takes to bring them in.

## Key finding: the runtime is already adapter-agnostic

Nothing in the *execution* path is tcp/grpc-specific:

- **Worker** `AdapterRegistry` resolves any `adapter` key against `ADAPTER_MAP`, which
  already registers `http`, `payshield` (+ `hsm_client`), `db_probe_core`,
  `db_probe_switch`, alongside `tcp`/`grpc` (`packages/worker/adapters/registry.py`).
- **Orchestrator** `_attach_connections` / `_attach_groups` only shallow-merge config
  dicts — no adapter assumptions. The Phase 4 flip, override matrix, endpoints[] and group
  selection are all adapter-neutral.

So a REST/payShield/DB target *already runs* fine as a named env adapter. The only thing
stopping it from being a **Connection** (and therefore getting the override matrix +
migration) is the connection registry boundary and the editor UI. Three spots:

## The three blockers

### 1. Validation allowlist — small (`connection_store.py`)

`ConnectionDraft` hard-codes `_ALLOWED_ADAPTERS = {"tcp", "grpc"}`, `_ALLOWED_PROTOCOLS`,
and defaults `protocol="iso8583"`, `host=""`, `port=0`. The model is already
`extra="allow"`, so REST fields (`base_url`, `api_key`, `timeout_ms`), DB fields
(`engine`, `dbname`, `user`, `password`), and payShield fields (`header`) persist with
**no schema change**.

Work: widen the allowlist to `{tcp, grpc, http, payshield, db_probe_core,
db_probe_switch}`; make `protocol`/`host`/`port` conditional so they aren't forced onto
non-tcp adapters (don't emit a bogus `protocol: iso8583` on a REST connection). Plus tests.
**~½ day, fully verifiable.**

### 2. Connection-test probe — small (`orchestrator/api/main.py:3243`)

`POST /connections/test` is hand-branched: `grpc`, else assume `TcpAdapter(host, port)`.
That's wrong for the new types — payShield is TCP but uses `HSMAdapter`, REST has
`base_url` not host/port, DB has `engine`/`host`.

Work: replace the bespoke branches with a generic probe that builds the adapter through
`AdapterRegistry` (or `ADAPTER_MAP[impl]`) and calls `connect()` + `health_check()` —
then it works for **every** registered adapter, including ones added later. Plus tests.
**~½–1 day, fully verifiable.**

### 3. Portal connection editor — the real cost (`portal/.../connections/`)

Today the editor has an adapter `<select>` of just `tcp`/`grpc` and two large form blocks
(`@if (d.adapter === "tcp")` ~230 lines, `@if (d.adapter === "grpc")`), with
`toAdapterConfig`/`fromAdapterConfig` handling only those two.

Work: add 3 options + 3 form blocks (REST: base_url / api_key / timeout / auth type;
payShield: host / port / header / timeout; DB: engine / host / dbname / user / password /
read-only query settings), matching model fields on `Connection`, new
`toAdapterConfig`/`fromAdapterConfig` cases, and `blankConnection` defaults. This is the
bulk of the effort and **cannot be build-verified in the cowork sandbox** (corrupt portal
`node_modules`). **~2–4 days.**

## Secrets — no new work

`api_key`, `password`, `pin` are already recognised by `SecretBox` / `is_secret_key` and
encrypted at rest. New connections just need to use those field names.

## Recommended phasing — decouple the cheap unblock from the UI

The migration value does **not** depend on the editor forms:

- **Phase X1 (backend, ~1 day, verifiable):** blockers #1 + #2. This alone lets
  REST/payShield/DB connections be **created via the API / MCP `create_connection`** and
  fully participate in backfill → collisions → flip → slim. Bucket B collapses into Bucket
  A immediately; users author these connections as JSON / via MCP until the UI catches up.
- **Phase X2 (portal forms, ~2–4 days, not verifiable here):** the friendly per-adapter
  editor UI. Pure polish on top of X1.

**Recommendation:** do X1 now if you want the migration to cover all real targets; defer
X2 until the portal can be built/click-tested on a real machine. X1 is small, safe, and
unblocks everything that matters for the connection/env model.

## Out of scope / watch-outs

- `terminal_sim` and other mocks stay standalone — they are not network endpoints and
  should never become connections.
- `http-live` already sources its values from `${vars.http_*}`; registering it as a
  connection is optional (Variables is already its single source).
- The `endpoints[]` / participant-group features are adapter-neutral, so REST/DB
  connections get HA/load-spread/failover for free once they're registerable.
