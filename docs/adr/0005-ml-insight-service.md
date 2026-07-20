# ADR-0005: ML insight service — failure categorization, explanation, and outcome prediction

**Status:** Accepted — service core implemented (see Implementation status)
**Date:** 2026-07-12
**Deciders:** PayProbe maintainers (David + reviewers)

## Context

PayProbe accumulates rich outcome data that today is only consumed by
rule-based and statistical code:

- **Per-step failures** — `StepOutcome` carries `error`, `assertions`,
  `raw_log`, structured `trace` lines, `duration_ms`, `started_at_ms`
  (`packages/worker/engine/runner.py`).
- **Run history** — durable `RUN_DB` with per-scenario pass/fail sequences;
  `RunStore.flakiness()` already computes a flip-based flakiness score and
  `run_trend` a daily pass-rate series.
- **Load runs** — persisted `error_categories`, `tps_series`, `stability`,
  latency histograms; `/load-runs/{id}/compare` does threshold regression
  detection.
- **A working failure taxonomy** — `report_service/diagnose.py::classify_error`
  maps raw error text to 10 categories (unreachable / timeout / auth /
  assertion / tls / binding / reference / url / error / unknown), each with a
  human explanation + fix hint (`_CATEGORY_HELP`), and `diagnose_run` ranks
  findings under a headline.

Three jobs are wanted that this machinery does not do, or does only crudely:

1. **Failure categorization that learns.** `classify_error` is a regex ladder.
   Anything it can't match lands in `error`/`unknown` — and in practice that
   bucket grows with every new adapter (payShield response codes, VISA action
   codes, CyberSource decision reasons don't match generic regexes). We want
   clustering/classification over the failure corpus so recurring failure
   *shapes* get named even when no rule anticipated them.
2. **Failure-reason explanation.** When a step fails, tell the operator *why*
   in terms of what the data shows: which category, what changed vs the last
   passing run of the same scenario, which prior runs failed the same way and
   what fixed them. Today `_CATEGORY_HELP` gives a static per-category hint;
   there is no instance-specific explanation.
3. **Run-outcome / flakiness prediction.** `flakiness()` is descriptive
   (what *was* flaky). We want predictive: given a scenario's history,
   environment, and recent platform signals, estimate the probability the next
   run fails or flakes — feeding scheduling ("re-run flaky ones first"),
   Go/No-Go context, and triage ordering.

### Forces

- **Advise-only is non-negotiable.** Invariant #6: the model proposes, the
  registry/orchestrator is the bouncer. A prediction must never gate a run,
  auto-modify a scenario, or alter a Go/No-Go verdict — sign-off provenance
  (ADR-0003) stays purely factual.
- **Cold start is real.** A fresh install has zero runs; a small team may have
  dozens, not thousands. Anything that *requires* big data before being useful
  is dead on arrival. The heuristic taxonomy must remain the floor, with
  learned output layered on top only when data volume justifies it.
- **The assistant tool layer lives once** (invariant #3). If insights are
  exposed to the assistants, that is one handler in
  `payprobe_common/agent_toolkit.py` + one primitive per backend — not a new
  per-service tool copy.
- **`report_service` is a pure library** on purpose (ADR-0003): functions over
  dicts, no I/O, no state. Model training (artifacts, retraining schedules,
  heavy deps) does not fit there.
- Heavy ML dependencies (scikit-learn, numpy/scipy) must not leak into the
  orchestrator or worker images — the worker fleet is sized for load
  generation, not model training.
- The monorepo has an established service shape: FastAPI package, file/SQLite
  persistence beside the db, fail-closed JWT gate, compose entry, two portal
  health panels, MCP proxy tool.

## Decision

Add **`packages/insight-service`** (port **8500**), a read-only advisory
service, built in three phases with the explicit rule *statistics first,
models when the data justifies them*:

- **Phase 0 (no new service):** expose the existing heuristics behind the
  *final API shape* — categorization = `classify_error` + `_CATEGORY_HELP`,
  prediction = a Bayesian-smoothed frequency estimate over
  `RunStore.flakiness()`/history. This ships value immediately and freezes the
  contract that later phases upgrade behind.
- **Phase 1:** stand up `insight-service` with classical ML — TF-IDF +
  clustering over the failure corpus for *discovered* categories (falling back
  to the heuristic taxonomy per corpus size), template-based instance
  explanations ("first failure after 12 passes; last similar failure resolved
  by X"), and a calibrated classifier for outcome prediction with honest
  abstention below minimum history.
- **Phase 2 (optional):** LLM-written explanations via the standalone
  assistant (the existing LLM egress boundary), grounded on Phase-1 features —
  never a new LLM egress path from insight-service itself.

Full component design, data contracts, feature sets and phase gates are in
[`docs/architecture/insight-service.md`](../architecture/insight-service.md).

## Options considered

### Option A — Extend `report_service` + orchestrator (no new service)

Grow `diagnose.py` with statistical models; orchestrator hosts the endpoints.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost | Lowest — no image, no port, no health wiring |
| Scalability | Poor — training in the orchestrator process; heavy deps leak into the runtime image |
| Fit | Violates `report_service`'s pure-library contract as soon as artifacts/state appear |

**Pros:** cheapest path; zero deployment surface; heuristics already live there.
**Cons:** `report_service` stops being pure the moment it persists model
artifacts; sklearn/numpy land in orchestrator (and, via imports, worker)
images; retraining competes with run orchestration for the same process. This
is exactly the "just put it where it's convenient" drift the repo has paid to
undo twice (crypto.py, assistant tool layers).

### Option B — Dedicated `insight-service` package (chosen)

New FastAPI service, own image, own deps, file-backed model artifacts,
read-only against orchestrator data.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — one more service, but on a well-worn template |
| Cost | One container + the known checklist (health panels, endpoints tab, MCP tool, compose) |
| Scalability | Good — training isolated; can be given CPU without touching the runtime path |
| Fit | Matches the monorepo shape; keeps invariant boundaries clean |

**Pros:** heavy deps quarantined; retraining can't hurt the runtime; advise-only
is structurally enforced (the service has no write access to registries or
runs); the API shape can be stood up in Phase 0 *before* the service exists
and moved later without breaking consumers.
**Cons:** real operational surface (deploy, monitor, version artifacts);
the two-health-panels + endpoints + MCP checklist must actually be done;
risk of over-engineering if data volume never materializes — mitigated by
phase gates below.

### Option C — Delegate everything to the LLM assistant

No trained models; the standalone assistant (`:8400`) reads run/trace data via
its existing tools and writes explanations/predictions on demand.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (code) / High (behavior) |
| Cost | Per-call LLM cost; latency in seconds |
| Scalability | Poor for bulk jobs (categorize 10K load-run errors) |
| Fit | Good for narrative explanation; wrong for classification and probability estimates |

**Pros:** zero training pipeline; instantly handles novel failure text; the
egress boundary and caller-JWT gate already exist.
**Cons:** non-deterministic categories (same error, different label on
different days) poison trend analysis; "probability of failure" from an LLM is
a vibe, not a calibrated number; cost scales with transaction count. Rejected
as *the* mechanism; kept as Phase 2 garnish on top of deterministic features.

## Trade-off analysis

The real decision is not "which option" but **where determinism is required**.
Categories and probabilities feed trends, comparisons, and (as context, never
verdict) sign-off screens — they must be reproducible, so they belong to
deterministic code (heuristics → classical ML). Narrative explanation is
consumed by a human once — it may be an LLM. Option B is the only shape that
holds that line while keeping heavy deps out of the runtime images.

The main risk of B is building a service for data that never comes. The phase
gates absorb this: Phase 0 costs almost nothing and is useful forever; Phase 1
starts only when a real corpus exists (gate: ≥500 categorized failures and
≥20 runs for ≥10 scenarios in a live install); each model must beat the
heuristic/statistical baseline on held-out data before it replaces it.

## Consequences

**Easier:**

- Triage: failures arrive pre-categorized with instance-specific context, not
  just a static category hint.
- Trend/regression analysis over *learned* categories catches adapter-specific
  failure families the regex ladder can't.
- Flaky-first re-run ordering and "this scenario is likely to fail tonight"
  warnings become possible.
- Both assistants gain read-only insight tools through one `agent_toolkit`
  handler.

**Harder:**

- One more service to deploy, secure (fail-closed JWT), monitor (both health
  panels + Settings→Endpoints), and document.
- Model artifacts need versioning and a "which model produced this label"
  provenance field, or categorized history becomes uninterpretable after
  retraining.
- Category churn between model versions must be managed (stable category ids;
  re-labeling is append-only, never destructive).

**Explicitly out of scope / never:**

- Auto-acting on predictions (blocking runs, editing scenarios, changing
  verdicts). Any future write path would require journalling + `restore_one`
  per invariant #2 — by staying read-only we avoid that entirely.
- Training on secret material. Features are drawn from error text, statuses,
  timings, categories — never decrypted SecretBox content, key material, or
  unmasked PANs (the capture buffer's redaction happens *before* anything the
  service reads).

## Implementation status (2026-07-12)

Phases 0 and 1 were built together as `packages/insight-service` — the API
contract and the service landed at once (the frequency/heuristic baselines ARE
the Phase-0 behavior; the learned layer activates only when scikit-learn is
installed and the corpus clears `MIN_CORPUS`). Built:

- `insight_service/` — `normalize` (masking + signatures), `store` (SQLite,
  `INSIGHT_DB`, `:memory:` in tests), `categorize` (shared taxonomy floor +
  optional TF-IDF/KMeans clusters with stable `ins-cat-*` ids), `explain`
  (evidence packs + prose), `predict` (recency-weighted Beta frequency,
  flip-based flakiness, honest `top_factors`), `ingest` (incremental sync +
  self-scoring join), `rest` (GET-only orchestrator reader), `auth`
  (fail-closed gate), `main` (FastAPI, port 8500).
- Wiring: compose service + volume, nginx `/api/insights/` (prod + dev),
  orchestrator `/status` `INSIGHT_API_URL` probe, `make test-insight`
  (24 tests; learned-layer tests skip without sklearn), CLAUDE.md map row.
- **Custom training extension (same day):** user-uploaded labeled datasets
  (`/datasets`, JSON or CSV, normalized before storage), supervised
  categorizer training with holdout metrics (`/models/train`), a model
  registry with exclusive activation, and pickled artifacts in
  `INSIGHT_DATA_DIR` reloaded at boot (covers the auto-trained cluster model
  too — closing the in-memory-only gap). Precedence: custom → clusters →
  heuristics; still advisory everywhere. Portal **Model Studio** page
  (`/model-studio`) drives upload/train/activate. Design doc §2.4.
- **Corrections loop (same day):** `POST /insights/corrections` — an operator
  relabels a failure; the label becomes ground truth (immune to model
  relabeling) and feeds the reserved "Operator corrections" dataset for
  retraining. Run-report why-panel gained the "teach the model" control.
- **`predict` step (same day, owner request):** mid-flow inference as a worker
  adapter (`insight`; catalog target "Model Insight") over a new
  side-effect-free `POST /infer` — scenarios branch on
  `${step.response.category}` / `.p_fail_next` etc. Five actions:
  `categorize` and `explain` (both accept optional `model_id` to pin one
  registered model over the active chain; pinned models are registry-loaded
  and cached, evicted on delete), `predict_outcome` (incl. `top_factors`),
  `model_status` (flattened pre-flight booleans for assertions), and `train`
  (pipeline retrain). The feasibility brief's node-kind idea, landed the
  cheap way (a `call` node + adapter needs no NodeKind/validator changes).
  Step inference is excluded from the self-scoring loop; gates still never
  see model output.
- **ML-quality round (same day):** (a) predictor **calibration** — isotonic
  over a calibration slice carved from the training data (never the
  evaluation holdout), so the gate judges the calibrated probabilities that
  actually ship; (b) **self-scoring join fixed** — predictions now score
  against the *first subsequent* run (chronological, per-run scoping), not
  whatever run was ingested last; (c) **active-learning label queue** —
  `GET /insights/label-queue` ranks distinct failure shapes by
  novelty × uncertainty × impact, corrections accept `apply_to_signature`
  for bulk relabeling, Model Studio grew a "Label these next" card;
  (d) **drift watch** — health snapshots (novel share + mean confidence) on
  every sync, an at-fit baseline on every train, `stale` verdict on
  `/status.categorizer.drift` and a warning banner on the taxonomy page.
- **Phase 2 (same day):** LLM-written explanations via the standalone
  assistant — `POST /insights/explain/{run_id}` on :8400
  (`assistant_service/explain.py`), grounded on an explicit `evidence_view`
  subset of the packs (test-pinned so nothing else reaches the prompt);
  portal "✨ plain words" button on the insight panel. Built ahead of the
  usage gate at the owner's request; the deterministic explanations remain
  the always-available floor.
- **Learned predictor (same day):** `predictor_model.py` — logistic regression
  over outcome-history features, gated on beating the frequency baseline's
  Brier on a chronological holdout; auto-fitted on every `/train`, demoted
  (artifact deleted) the moment it stops winning; `basis: "model"` in
  prediction responses only while it holds the crown. `/status.predictor.gate`
  shows the verdict either way.

## Action items (remaining)

1. [x] Service + API contract + baselines + learned layer (see above).
2. [~] Portal: **code written 2026-07-12, NOT build-verified** (AI sandboxes
   can't run `npm run build` — needs a real build + click-through on the
   host). What landed: `shared/insight-api.service.ts` (client, degrade-on-
   error contract), `insight` endpoint in RuntimeConfig + both environment
   files (dev :8500 / prod `/api/insights`) — this feeds the dashboard
   "Endpoints" panel and Settings→Endpoints generically; "System health"
   picks up `insight-service` from the orchestrator `/status` probe. Run
   report: advisory "model insight" section (category chips, novel flag,
   expandable "why did this fail?" evidence text, same-signature run links),
   fetched only for failed runs, hidden entirely on any error. Trends:
   "Likely to fail next" card (P(fail), P(flaky), history, top factor),
   advisory dashed styling, hidden when the service isn't deployed.
3. [x] MCP tools DONE 2026-07-12: `get_run_insights` / `list_insight_predictions`
   / `train_insights` ("Model insights (advisory)" group), catalog regenerated,
   89 mcp-server tests green. `agent_toolkit` DONE the same day: two read-only
   tools (`get_run_insights`, `list_insight_predictions`) + primitives in both
   backends (StoresBackend `_insight_api_get`, RestBackend via `rest.i()`),
   `INSIGHT_API_URL` env in both; unreachable ⇒ a clean "optional deployment"
   ToolError. Both agent suites green (330) + 3 new bridge tests.
4. [~] Gate check in the wild: the **Failure Taxonomy page**
   (`/insight-categories`) now visualizes the `error`/`unknown` share against
   the 15% phase gate (meter + honest verdict text both ways) and lists every
   category with inline rename. What remains is simply accumulating real run
   data and reading the meter.
5. [x] Nightly training DONE 2026-07-12 — as a **self-training loop** in the
   service itself (`INSIGHT_TRAIN_INTERVAL_SEC`, compose default 86400; 0 =
   off), not via the run-schedule machinery (that schedules *runs*, and the
   service owning its own retraining keeps the boundary clean). Errors are
   logged and swallowed so a down orchestrator can't kill the loop.

Note (2026-07-12): insight-service's tests deliberately have **no
`tests/__init__.py`** and keep helpers in `insight_fixtures.py` — with an
`__init__.py` the suite collides with scenario-service's `tests` package in
cross-package collection (both hyphenated parents ⇒ bare `tests` module name).
The scenario-before-mcp-server combined-collection failure is pre-existing and
unrelated (CLAUDE.md: per-package runs are the known-safe mode).
