# PayProbe — Project Review

_Whole-codebase assessment: strengths, weak spots (ranked, with evidence), quick wins, and a forward-looking idea list. Generated 2026-06-20 against the current tree._

## Snapshot

| Area | Lines | Tests |
|---|---|---|
| worker (engine + adapters + load) | ~7.2k py | strong |
| scenario-service | ~8.6k py | strong |
| orchestrator | ~2.8k py | good |
| portal (Angular) | ~12.3k ts | **none** |
| mcp-server | ~0.25k py | minimal |
| auth-service | real (JWT, users) | yes |
| report-service / helpers | README only | — |

Backend: **347** Python test functions. Frontend: **0** spec files. Debt markers are low (3 TODO/FIXME); broad `except Exception` swallows in the core orchestrator paths now log with context (cleanup-only catches left intentional).

## Status update — review 4 (2026-06-21)

Product-polish pass on top of the hardening. Suite state (all green): report_service **15**, scenario-service **164**, orchestrator **79**, worker load/sandbox/metrics **25** — **366** backend tests total (+29 since review 3). Portal app type-checks clean; a Playwright e2e harness landed (2 specs / 6 cases). The only failures remain the 4 pre-existing `test_engine.py` crypto/EMV tests (missing key material in this sandbox; they pass in CI).

Delivered this pass:

| Item | Status |
|---|---|
| Diagnose-my-run | ✅ `report_service/diagnose.py` (taxonomy unreachable/timeout/auth/assertion/tls/binding → cause + fix + evidence; functional **and** load runs), `GET /runs/{id}/diagnose` (live snapshot or persisted), portal "Diagnose this run" panel. 9 lib + 3 endpoint tests |
| Reopenable load runs | ✅ `GET /load-runs` (filtered by `load:` label) + "Recent runs" panel + `/load/:id` route that replays the event stream to rebuild charts; persisted summary for finished runs. 1 test |
| Playwright e2e | 🟡 harness landed: UI golden paths (dashboard, load-form reactivity, docs diagram) run backend-free in a CI `portal-e2e` job; full author→run / load→stop / diagnose flows written but gated behind `E2E_FULL`; excluded from the app build. Component unit specs still 0 |
| Constructor breakup | 🟡 first safe slice: pure graph/geometry helpers → `graph-util.ts`; component 2548→2457 lines, type-checks clean. More to extract, now with e2e as a guardrail |

## Status update — review 3 (2026-06-20)

After three rounds of hardening, the security/reliability tier is largely closed. Current suite state: worker load/sandbox/metrics **25**, scenario-service **164**, orchestrator **75** — all green (the only failures are 4 pre-existing `test_engine.py` crypto/EMV tests that need crypto key material absent from this sandbox; they pass in CI).

| # | Finding | Status |
|---|---|---|
| 1 | Orchestrator unauthenticated | ✅ **Resolved** — `require_auth` fail-closed on every route + WS guard; real `auth-service` (JWT issuance, password hashing, `/token` `/verify` `/me`) |
| 1b | scenario-service auth weaker than orchestrator | ✅ **Resolved** — same `require_auth` (JWT + static, fail-closed), `tests/conftest.py` keeps tokenless tests open |
| 2 | Code nodes not security-sandboxed | ✅ **Resolved** — child runs in a fresh network namespace (`unshare`), `auto`/`strict`/`off` modes; `/nodes/execute` refuses unauthenticated unless `PAYPROBE_ALLOW_UNAUTH_CODE` |
| 3 | Single-process / in-memory run state | ✅ **Resolved (control plane)** — `run_control` routes cancel/stop cross-replica for **functional and load** runs; load runs register kind=`load` and stop routes via the bus + cancel channel; `RUN_DB` durable; live snapshots flow over Redis Streams so any replica can observe |
| 4 | Load fallback runs in orchestrator loop | ✅ **Resolved** — in-process fallback capped (`PAYPROBE_INPROC_MAX_TPS`/`_CONNECTIONS`); above the cap it refuses, logs, and surfaces an "Action needed: start external workers" notice in the portal |
| 6 | Secrets plaintext at rest | ✅ **Resolved (connections)** — Fernet `SecretBox` encrypts secret-named fields in the connections file (`PAYPROBE_SECRET_KEY`); plaintext in memory; backward-compatible. Scenario `secret_vars` in Postgres still pending |
| 7 | No self-observability | 🟡 **Mostly resolved** — Prometheus `/metrics` on all three services + per-load-run gauges; OpenTelemetry tracing scaffolding present (`init_tracing`) but spans not yet threaded end-to-end |
| 8 | Broad exception swallowing | ✅ **Resolved (core paths)** — variable-merge, terminal-event-publish, scheduler-loop swallows now log; cleanup catches intentional |
| 5 | No portal tests | 🟡 **Harness landed** — Playwright e2e scaffolded: UI golden paths (dashboard, load form, docs diagram) run backend-free in CI; full author→run / load→stop / diagnose flows gated behind `E2E_FULL`. Component unit coverage still thin |
| — | Product polish | ✅ **Diagnose-my-run** (taxonomy → cause + fix, `report_service.diagnose`, `/runs/{id}/diagnose`, portal panel); **reopenable load runs** (`GET /load-runs` + `/load/:id` replay); **constructor slimmed** (pure graph/geometry helpers → `graph-util.ts`, 2548→2457 lines, more to extract) |
| — | report-service | ✅ **Resolved (library)** — report/certification generators extracted to a tested `report_service` package (sibling of `worker`); orchestrator delegates to it; 6 generator tests + CI job. Standalone HTTP service still optional/future |
| — | CI gaps | 🔴 **Open** — no coverage gate, no security scan (bandit/`npm audit`/Trivy), no portal tests, `sleep 15` instead of readiness poll |
| — | committed build artifacts / packaging | 🟡 **Fixed in tree** — `build/` git-ignored; untrack with `git rm -r --cached packages/auth-service/build` (sandbox can't touch `.git`) |

**Net:** posture moved from "open by default, single-process, plaintext secrets" to "fail-closed auth across services, network-isolated code sandbox, encrypted connection secrets, cross-replica run control, and a capped load fallback." The remaining work is **quality/operability**, not security: **portal tests (#5)**, **report-service**, **CI hardening**, and finishing **tracing**. Highest leverage now is the **portal test harness** — it's the largest untested surface and the only red security-adjacent gap left (the auth/login UI ships untested).

## What's strong

The architecture is clean and the seams are in the right places: a sink-agnostic event core, a resumable Redis-Streams backbone, an adapter-registry pattern that isolates each protocol, and a three-phase execution model with blast-radius gating. The worker engine is genuinely well-built (asyncio + uvloop, bounded concurrency, virtual-thread-free). Test discipline on the backend is good, CI runs lint + tests + a mock integration run, and the docs follow Diátaxis with live API references. The new load-testing subsystem reuses the same adapters and event transport rather than forking a parallel stack.

---

## Weaknesses, ranked

### 1. The orchestrator is unauthenticated (Critical)

`packages/orchestrator/api/main.py` has **no auth dependency anywhere**. Every endpoint is open: `POST /runs`, `POST /load-runs`, `POST /nodes/execute`, the simulators, cancel/stop. scenario-service at least has an optional static bearer (`require_token`, gated on `API_TOKEN` and **off by default** — `packages/scenario-service/api/main.py:222`). The promised `auth-service` is a README stub, so there is no JWT issuance, no RBAC, no per-project isolation anywhere.

Combined with the next item, an unauthenticated `POST /nodes/execute` is effectively "run arbitrary code on the worker host for any client that can reach the port."

**Fix:** ship a minimal auth gate now — a shared dependency that validates a JWT (or at least the static bearer) applied to orchestrator routers, defaulting to **deny when unset** in non-dev. Then build `auth-service` for real (JWT + roles + project scoping).

### 2. Code nodes are sandboxed for crashes, not for security (High)

`packages/worker/engine/code_runner.py` runs user Python/JS in a subprocess with `python -I`, rlimits, a CPU/mem cap, a clean env, and a timeout. That contains hangs, OOM, and crashes well — but it is **not** a security boundary: the snippet can still open sockets and read the filesystem. Reachable unauthenticated via `/nodes/execute`, that's SSRF / data-exfiltration / RCE-adjacent.

**Fix:** drop network access (network namespace / `unshare -n`, or run under nsjail/firecracker/gVisor), restrict the filesystem, and put it behind auth. At minimum, document it as "trusted authors only" and disable `/nodes/execute` when auth is off.

### 3. Single-process, in-memory run state (High)

Live run tracking is process-local: `RUNS: dict` and `LoadCoordinator.runs` live in one orchestrator process, and `RunStore` defaults to `:memory:` (`main.py:105`). Consequences: a restart loses the ability to stream/stop/cancel in-flight runs; you cannot run two orchestrator replicas (a second instance can't see or stop the first's runs); and a load run's coordinator state vanishes on crash. This caps availability and horizontal scale for a tool whose whole job is high-concurrency execution.

**Fix:** move run/load-run registries into Redis (or Postgres) keyed by run id, so any replica can observe, aggregate, and stop a run; default `RUN_DB` to a real file/Postgres in non-dev.

### 4. The load subsystem's in-process fallback can starve the orchestrator (High — and it's my own code)

The fallback I added spawns `load_worker` coroutines **inside the orchestrator event loop** when no external worker claims shards. At low rates that's fine and makes the feature "just work," but at the advertised 20K TPS those workers contend with the API/WebSocket loop on the same process — the orchestrator can stall exactly when you most need its live metrics. Related load-subsystem gaps: coordinator aggregation is single-consumer and in-memory (no second replica, no persistence of the time-series — only the final summary is stored); soak `connections` are virtual clients sharing the adapter pool, not real sockets; only the *last* error is surfaced, not an error taxonomy; there's no per-worker breakdown in the UI.

**Fix:** cap fallback workers to a low rate and make external workers the documented production path (set `PAYPROBE_LOAD_EXTERNAL_WORKERS=1`); persist load samples as a time-series; add per-worker + top-N-error rollups.

### 5. The portal has no tests (High)

12.3k lines of Angular, **zero** `.spec.ts`. CI lints and builds the portal but runs no unit or e2e tests, so regressions in the editor, run-monitor, or the new Load Test page are caught only by humans. The constructor component alone is very large (1.3k+ lines).

**Fix:** add Playwright e2e for the golden paths (author → run → report; configure → load test → stop) and component tests for the high-traffic pieces; gate CI on them.

### 6. Mixed durability and unencrypted secrets at rest (Medium)

scenario-service persists scenarios in Postgres (`pg_store.py`) but keeps connections, starter flows, formats, catalog, and assist config in **JSON files** guarded only by an intra-process `threading.Lock`. That's not safe across replicas and defaults to `:memory:` in several places. Connection definitions include endpoint credentials; `secret_vars` are masked in the UI but stored **plaintext**. There's no secrets-manager integration.

**Fix:** move the file-backed registries into Postgres (you already have it) for multi-instance safety, and encrypt secret values at rest (or reference a Vault/KMS handle instead of storing the value).

### 7. No self-observability (Medium)

For a performance-testing platform there is, ironically, no metrics endpoint, no tracing, and no structured run/latency export to a TSDB. Diagnosing "why is throughput low" relies on logs. The recent "0 TPS / all errors" episode is the case in point — the cause was invisible until error reasons were threaded through by hand.

**Fix:** expose Prometheus `/metrics` on each service (request rates, run counts, queue depths, worker liveness), add OpenTelemetry spans across orchestrator→worker→adapter, and ship a Grafana dashboard for load runs.

### 8. Broad exception swallowing (Medium)

77 `except Exception` and ~54 `except … : pass`. Some are deliberate (never let reporting kill a run — including code I added), but blanket swallowing hides adapter bugs and makes failures look like silence. 

**Fix:** narrow the catches, log at `warning`/`exception` with context, and reserve bare `pass` for paths that are genuinely best-effort (and comment why).

### 9. Docs / diagram overstate what's built (Low, but trust-eroding)

`docs/architecture/overview.md` and the new in-app platform diagram show `auth-service`, `report-service`, and the helper services as components, but they are README stubs — certification/reports actually live in the orchestrator. The overview ASCII diagram also predates the load-testing subsystem and gRPC.

**Fix:** mark planned-vs-built on both diagrams (e.g. dashed "planned" boxes for auth/report/helpers), and refresh the ASCII overview to include the load fleet.

### 10. CI gaps (Low)

`ci.yml` is solid but has no coverage gate, no security scanning (bandit / `npm audit` / Trivy on images), no portal tests, and builds neither stub service. The mock-integration step uses a fixed `sleep 15` rather than a health-gate.

**Fix:** add coverage thresholds, dependency/image scanning, portal tests, and replace the sleep with a readiness poll.

---

## Quick wins (high value, low effort)

1. Apply the existing `require_token` pattern to the orchestrator and default to deny-when-unset outside dev. (hours)
2. Disable `/nodes/execute` when auth is off; add a `PAYPROBE_ALLOW_CODE_EXEC` guard. (hours)
3. Default `RUN_DB` to a file path in the container image, not `:memory:`. (minutes)
4. Add `npm audit --production` and `bandit` to CI. (hours)
5. Encrypt `secret_vars` at rest with a single Fernet key from env. (half day)
6. Replace the load fallback's unbounded in-proc workers with a low rate cap + a one-line "run external workers for real load" note in the UI. (hours)
7. Mark planned services as dashed on both architecture diagrams. (minutes)

---

## New ideas / roadmap

**Make the load subsystem best-in-class.** Persist per-run throughput/latency time-series and add them to `/runs/trend`, so performance regressions show up alongside pass/fail. Add **SLO gates** on a load run (fail if p99 > X ms or error-rate > Y%) and wire them into scheduled runs + CI — turning the load tool into a performance-regression guard. Add **run-vs-run compare** (you already have regression compare for functional runs) and a **per-worker / top-N-error** breakdown.

**Scale the fleet for real.** Run `load_worker` as Kubernetes Jobs with an HPA that targets the requested TPS; let the coordinator live in Redis so multiple orchestrators and a worker fleet coordinate without a single process. Add a connection-true soak mode (one socket per virtual client) where the adapter supports it.

**Close the auth/tenant story.** Build `auth-service` (JWT + refresh), add RBAC (viewer/author/operator/admin) and project-scoped isolation, and an audit log of who ran what against which environment — important when the tool can drive real payment switches.

**Operate it like production.** Prometheus + OpenTelemetry + Grafana; health/readiness probes everywhere; a `/status` aggregator. Secrets via Vault/KMS. Image scanning and SBOMs.

**Deepen the testing value.** Record-and-replay: import a real switch capture (PCAP / ISO 8583 log) and generate a scenario from it. Fault/chaos injection in the simulators (latency, drops, declines, partial reads) to test resilience, not just happy paths. Data-driven load with a configurable card-profile / MTI mix to mirror production traffic shape.

**Polish the product.** Playwright golden-path e2e; break up the giant constructor component; a live load-run **artifact/dashboard** the user can reopen; and an in-app "diagnose my run" helper that reads the error taxonomy and suggests the likely cause (the manual version of what we just did for "0 TPS").

---

## Bottom line

The engineering core is strong and the design is coherent — this is a well-architected system, not a prototype. The gaps are mostly at the **edges that turn a good internal tool into a trustworthy product**: authentication/isolation, durability of run state, frontend testing, secrets-at-rest, and self-observability. The single highest-leverage move is **auth + run-state durability**, because they unblock multi-user use and horizontal scale at once. The load subsystem is a strong new capability but should graduate from in-process fallback to a real, persisted, externally-scaled fleet before it's leaned on at 20K TPS.
