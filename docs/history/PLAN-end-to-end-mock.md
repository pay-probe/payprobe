# PayProbe — Exact Plan: End-to-End on Mocks

**Goal of this push:** make a scenario actually *run* start-to-finish against mock
adapters and surface live results in the portal. Today you can author scenarios
but nothing executes them. This plan builds the missing spine.

**Decision:** prove the whole pipeline on mocks first, then swap mock → real
adapters (v0.2). Building real adapters before there's an engine to run them in is
wasted motion.

---

## Reality check (code vs ROADMAP.md)

`ROADMAP.md` marks v0.1 "done." The code disagrees. Actual state on `main`:

| Component | ROADMAP says | Actually |
|---|---|---|
| Scenario service + constructor (v0.3) | done | **Real** — 37 files, models, pg_store, projects/sets, import/export, tests |
| Portal (dashboard + constructor) | done | **Real** — Angular 20, on mock data |
| Worker base adapter + registry + mock adapter | done | **Real** — base contract, registry, MockAdapter |
| **Worker engine** (`engine/__init__.py`) | done | **EMPTY (0 lines)** — no executor, no phases, no semaphores |
| Three-phase pipeline | done | **Not built** |
| Orchestrator service | done | **README stub only** |
| Report service | done | **README stub only** |
| Auth service | done | **README stub only** |
| 4 helper probes | done | **README stubs only** |
| Real adapters (RestPay/HSM/ISO8583/CB/DBProbe) | v0.2 todo | not built (registry imports them lazily; absent = fine) |
| Infra (compose, postgres init.sql, nginx) | done | **Real** |

**Net:** the two ends exist (author scenarios, view UI). The middle — execute,
orchestrate, report — does not. That middle is this plan.

---

## The vertical slice we are building

```
Constructor ──POST /runs──▶ Orchestrator ──spawn──▶ Worker Engine
                                │                        │
                                │                   mock adapters
                                ▼                        │
                            Postgres  ◀──persist run_steps/phases
                                ▲                        │
                                └──── events ───────────┘
                                       │ (Redis pub/sub)
                                       ▼
                          Portal run-monitor (WS)  ──▶  Report service
```

Each numbered step below is independently testable. Steps 1–3 are the critical
path (a run executes and persists). Steps 4–6 make it visible and reportable.

---

## WHAT / WHEN / HOW

### Step 1 — Worker Engine  ⟵ *starting now*
**What:** the execution core in `packages/worker/engine/`.
**Why first:** everything downstream calls it; it has zero dependency on the
other unbuilt services and runs fully on mock adapters.
**How:**
- `events.py` — typed `RunEvent`s (`run.started`, `phase.update`, `step.result`,
  `scenario.result`, `run.completed`) + an `EventSink` protocol with an
  in-memory implementation (Redis sink slots in at Step 3).
- `variables.py` — resolve `${step_id.response.path}` references against prior
  step results (the spec's headline feature; validator already exists in
  scenario-service, this is the runtime half).
- `runner.py` — `ScenarioRunner`: run one scenario's steps sequentially, honor
  `stop_on_failure`, run assertions via `BaseAdapter.assert_response`, emit
  `step.result`.
- `engine.py` — `WorkerEngine`: `connection_semaphore` + `ops_limiter`
  (`asyncio.Semaphore`), `warmup_adapters()`, and three-phase execution:
  - Phase 1: `registry.health_check_all()`; failed targets recorded.
  - Phase 2: run `component`+`integration` scenarios; skip any whose targets hit
    a failed health check → `BLOCKED` (blast-radius gating, spec §6.3/§9.4).
  - Phase 3: run `e2e` scenarios; block those depending on a failed phase-2 target.
- Registry tweak: in mock mode route unknown targets to `MockAdapter` so the
  `mock.json` env (which names `http`, `hsm`, …) runs without real adapter deps.

**Done when:** `python -m payprobe.worker` (or a test harness) runs the bundled
example scenarios against mocks and prints a pass/blocked/fail summary.

### Step 2 — Test the engine
**What:** `packages/worker/tests/test_engine.py`.
**How:** cover (a) full mock run all-pass, (b) `force_unhealthy` adapter →
dependent scenarios BLOCKED, (c) `${...}` variable resolution between steps,
(d) failing assertion → scenario FAILED. `pytest` green.
**Done when:** tests pass in CI-equivalent local run.

### Step 3 — Run Orchestrator service
**What:** `packages/orchestrator/` FastAPI (spec §4.2).
**How:** `POST /runs` (env + scenario_ids) persists a `runs` row, launches the
engine as an asyncio task, wires a **Redis pub/sub event sink** on
`run:{id}:stream` and writes `run_phases`/`run_steps` to Postgres as events
arrive. `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, and the
`GET /runs/{id}/stream` WebSocket. Reuse the existing `asyncpg`/Redis infra from
`infra/docker`.
**Done when:** `curl POST /runs` against the mock env produces a persisted run
with steps, and the WS streams live events.

### Step 4 — Portal wiring (run path)
**What:** connect `run-monitor` + `dashboard` to live orchestrator APIs.
**How:** point the existing `WebSocketSubject` service at
`/runs/{id}/stream`; replace dashboard mock data with `GET /runs`. Constructor's
"run" action → `POST /runs`.
**Done when:** authoring a scenario and clicking run shows a live phase/step feed.

### Step 5 — Report service
**What:** `packages/report-service/` FastAPI (spec §4.4 / §10).
**How:** `GET /reports/{run_id}` assembles phases+steps; `POST .../baseline`
pins; `GET .../diff` does status/value/perf diffing; `export?format=html|junit|json`.
**Done when:** a completed run renders an HTML report and a JUnit file CI can read.

### Step 6 — Glue & verify end-to-end
**What:** docker-compose.mock brings up postgres + redis + orchestrator + worker
+ report + portal; one scripted run goes author → execute → report.
**How:** extend `infra/docker/docker-compose.mock.yml`; add a smoke script under
`scripts/`. Reconcile `ROADMAP.md` to reflect true state.
**Done when:** `docker compose -f docker-compose.mock.yml up` + smoke script is
green from a clean checkout.

---

## Deferred (explicitly *not* in this push)
Real adapters (v0.2), auth-service (mock run is single-user/local), helper
probes, observability (v0.4), CI/CD integration (v0.5). They sit on top of a
working spine and are cheap to add once Steps 1–6 land.

## Sequencing summary
1 → 2 (gate) → 3 → 4 ∥ 5 → 6. Steps 4 and 5 can proceed in parallel once 3 is up.
