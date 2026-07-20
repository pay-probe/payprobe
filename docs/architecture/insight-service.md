# Insight service — design (companion to ADR-0005)

Status: **built 2026-07-12** (service core + wiring; portal surfacing and MCP
tools pending — see ADR-0005 "Implementation status"). Read
[ADR-0005](../adr/0005-ml-insight-service.md) first for the why and the
options that were rejected.

The one-line contract: **a read-only advisory service that turns run history
into categories, explanations, and probabilities — and is never allowed to act
on them.**

## 1. Placement

| | |
|---|---|
| Package | `packages/insight-service` |
| Port | 8500 |
| Role | Advisory reads over run/load history; model training + artifact store |
| Writes to registries/runs | **None.** Structurally read-only |
| Auth | Fail-closed JWT gate, same pattern as the other services (`PAYPROBE_ENV=dev` opens locally) |
| Docker context | `packages/` (ships `payprobe_common`, like scenario-service / payprobe-assistant) |
| Deps (service-local only) | scikit-learn, numpy — **must not** appear in orchestrator/worker requirements |

Data access: HTTP reads against the orchestrator (`RUN_API_URL` pattern, same
as the in-process assistant's runtime-read tools) — runs, run detail, load
runs, flakiness, trends. No direct SQLite access to the orchestrator's `RUN_DB`
(replicas + file locking say no).

## 2. The three capabilities

### 2.1 Failure categorization

**Baseline (floor, always available):** `report_service.diagnose.classify_error`
— the 10-category regex taxonomy. The service imports `report_service` (pure
library) and uses it verbatim; the taxonomy is shared vocabulary, not forked.

**Learned layer (Phase 1):** the failure corpus is every failed
`StepOutcome`'s `(error, action, target-adapter-family, assertion diffs)` plus
load-run `error_categories` raw messages.

- Features: TF-IDF over normalized error text (numbers, hex, ids, hosts
  masked out — `"connect to 10.1.2.3:8583 failed"` → `"connect to HOST:PORT
  failed"` — so clusters form on failure *shape*, not instance noise), PLUS
  **structured feature channels** (2026-07-12, see
  [the feature analysis](../history/insight-feature-analysis.md)): low-cardinality,
  digit-transliterated tokens (`tgt_hsm act_send_command rc_af env_staging
  dur_slow asrt_response_code`) derived from the step's target, action,
  response code, HTTP class, environment, duration bucket and failed
  assertion fields — stored in a `feats` column beside (never inside) the
  text, so signatures/dedup are untouched. Load runs feed the corpus as one
  weighted row per `error_categories` bucket (`src_load` channel); the
  predictor gained short-window fail rate + run-cadence features with a
  dimension guard on pickled artifacts.
- Model: incremental clustering (MiniBatchKMeans or HDBSCAN over the corpus)
  to *discover* categories; a lightweight classifier (linear SVM / logistic on
  the same features) to assign new failures to known categories fast.
- Every discovered cluster gets a **stable id** (`ins-cat-<hash>`), a
  human-editable name, and is *mapped onto* the nearest heuristic category
  where possible (`unreachable`, `timeout`, …) so existing trend surfaces keep
  working. Novel clusters carry `heuristic: "error"|"unknown"` — these are
  exactly the ones worth a human look, and the count of them is the metric
  that justifies (or kills) the learned layer.

**Output per failure:**

```json
{
  "category": "ins-cat-9f3a",
  "label": "payShield 'handler is closed' after chaos drop",
  "heuristic": "unreachable",
  "confidence": 0.91,
  "model_version": "cat-2026-07-12-1",
  "novel": false
}
```

`model_version` on every label is mandatory — relabeled history is
uninterpretable without it (ADR-0005 consequences).

### 2.2 Failure-reason explanation

Deterministic, template-based (Phase 1). For a failed step the service
assembles an **evidence pack** from history:

- the category + heuristic help text (from `_CATEGORY_HELP`);
- position in history: "first failure after N consecutive passes of this
  scenario in this environment" vs "fails every run since <date>";
- nearest-neighbor prior failures (same cluster, same scenario/adapter) and —
  when a later run of that scenario passed — what differed (environment,
  connection config hash, variable set), which is the closest honest proxy
  for "what fixed it";
- correlated platform signals in the failure window: active chaos storm,
  simulator restarts, load-run saturation (`stability`), diagnostics-doctor
  findings.

The explanation is the evidence pack rendered as text. **Phase 2 (BUILT
2026-07-12):** the standalone assistant (`:8400`, the LLM egress boundary)
exposes `POST /insights/explain/{run_id}` (`assistant_service/explain.py`):
it fetches the categorized failures over `rest.i()`, reduces each to an
explicit `evidence_view` — the *whole* LLM-visible surface, pinned by a test
so nothing outside the pack (raw errors, norms, signatures) can leak into the
prompt — and makes one grounded, tool-free completion ("use ONLY the
evidence; never invent; these are advisory"). insight-service itself makes
**no** LLM calls — one egress boundary stays one. No LLM configured ⇒ the
standard needs-config reply; the deterministic explanations always remain.
Portal: the "✨ plain words" button on the run report's insight panel renders
the prose above each deterministic explanation.

### 2.3 Run-outcome / flakiness prediction

**Baseline (Phase 0):** Bayesian-smoothed failure frequency per
(scenario, environment) over a sliding window, blended with the existing
`flakiness()` flip score. Honest and cheap: with 5 runs of history the answer
is mostly the prior, and the API says so (`basis: "frequency", n: 5`).

**Learned layer (BUILT 2026-07-12, `predictor_model.py`):** training examples
come from sliding over each scenario's outcome history (every outcome with
≥3 prior runs = one example; features: recency-weighted fail rate, flip rate,
last/last-2 outcomes, fail streak, pass-duration drift, log history size).
Logistic regression, evaluated on a **chronological holdout** (newest 20%)
against the frequency baseline's Brier on the *same* examples. The gate is
mechanical: the model activates (`basis: "model"`, factors from
coefficient×feature contributions) only when its holdout Brier is strictly
better; every retrain re-runs the gate, and a demoted model's artifact is
deleted so a restart can't resurrect it. Below 30 examples or with
single-class history it refuses to fit and says why. The baseline remains
the permanent floor and still answers `p_flaky` and thin-history cases.

**Output:**

```json
{
  "scenario_id": "sc-42",
  "environment": "staging",
  "p_fail_next": 0.34,
  "p_flaky": 0.61,
  "basis": "model",
  "model_version": "pred-2026-07-12-1",
  "n_history": 48,
  "top_factors": ["3 flips in last 10 runs", "duration +40% over 7d"]
}
```

`top_factors` is required — an unexplained probability gets ignored or, worse,
trusted blindly.

## 2.4 Custom training — datasets + Model Studio (added 2026-07-12)

Users can train the categorizer on **their own taxonomy** instead of waiting
for clusters to discover it. The flow: upload a labeled dataset (rows of
``{text, label}`` — raw failure/decline text and the user's label, as JSON or
CSV) → ``POST /models/train`` fits a supervised TF-IDF + logistic-regression
model with an honest 20% holdout accuracy → the model is **registered** (id,
datasets used, metrics) with its artifact **pickled to ``INSIGHT_DATA_DIR``**
→ activating it makes it the top of the categorize precedence:

    active custom model  →  auto-discovered clusters  →  heuristic taxonomy

The deterministic floor never goes away; a custom model changes labels and
explanations, never verdicts. Dataset text passes through the same normalizer
as the ingested corpus (digits/hosts/ids masked), so uploaded PANs never
persist. One model is active per kind; activation is exclusive; ``none``
deactivates. The portal's **Model Studio** page (/model-studio, Execution
group) drives all of it: upload (paste CSV or file picker), dataset list with
label chips, train-on-selected, registry with holdout accuracy, activate /
deactivate / delete.

Artifact persistence added here also covers the **auto-trained cluster
model**: it is pickled on every ``train()`` and reloaded at boot — before
this, a restart silently dropped back to heuristics until the next retrain.
An active model whose artifact is missing/corrupt at boot is deactivated
loudly rather than silently lied about.

**Corrections (human-in-the-loop).** ``POST /insights/corrections``
(run/scenario/step + the operator's label) does two things: appends the
failure's normalized text + label to the reserved **Operator corrections**
dataset (``ds-corrections`` — visible in Model Studio, retraining on real
ground truth is one checkbox away), and relabels the corpus row with
``model_version: human-correction``. Human labels are ground truth: the
failures endpoint serves them verbatim (no model re-categorization) and the
cluster retrain's relabel pass skips them. The run report's why-panel carries
the "wrong category? → teach the model" control.

## 2.5 The `predict` step (added 2026-07-12, owner request)

Scenarios can now ask for a model opinion **mid-flow** and branch on it. Not
a new node kind — a worker **adapter** (`worker/adapters/insight/`, target
`insight`, catalog "Model Insight"), so the palette, `${step.response.*}`
autocomplete and validators picked it up from the existing machinery:

    [auth step] → [insight.categorize {text: ${auth.response.error}}]
                → [if ${predict.response.novel}] → [alert / alternate path]
    [insight.predict_outcome {scenario_id: sc-42}]
                → [if ${predict.response.p_fail_next} > 0.7] → [re-run first]

Backed by ``POST /infer`` on the service (kinds ``categorize`` /
``explain`` / ``predict_outcome``; ``categorize``/``explain`` accept an
optional ``model_id`` to pin one registered model instead of the active
chain — loaded from the registry and cached, evicted on delete). The adapter
also exposes ``model_status`` (flattened /status for pre-flight assertions:
``custom_model_active`` etc.) and ``train`` (ingest + refit as a pipeline
step, e.g. after a data-load scenario). Inference is deliberately
**side-effect-free**: step-driven
inference is never logged into the predictions self-scoring loop, so scenario
control flow can't pollute the model's honesty metrics. Boundary unchanged:
the answer feeds the author's own if/switch logic; gates and sign-off never
see it. Connection config: ``base_url`` (default ``$INSIGHT_API_URL``),
optional ``token``, ``response_timeout_sec``.

## 3. API sketch

```
GET  /insights/failures/{run_id}          # categorized failures + evidence packs
GET  /insights/categories                 # discovered taxonomy, counts, trends
GET  /insights/predictions?label=&env=    # per-scenario outcome predictions
GET  /insights/predictions/{scenario_id}
POST /insights/categories/{id}/rename     # human names a cluster (the ONE write,
                                          # to the service's own store only)
GET  /status                              # health + loaded model versions
POST /train                               # manual retrain trigger (also scheduled)
```

Consumption:

- **Portal:** category chips + "why did this fail?" panel (with the
  teach-the-model correction control) on the run report; "Likely to fail
  next" card on Trends; Model Studio (`/model-studio`); Failure Taxonomy
  (`/insight-categories`) — the category table with inline rename plus the
  **novel-share gate meter** (the % of failures in `error`/`unknown`,
  visualized against the 15% phase gate, with the honest reading spelled out
  in both directions). All advise-only styling — informational, never a gate
  or a blocker.
- **MCP:** `get_insights`, `get_failure_insight`, `predict_scenario` proxy
  tools (registry.py → regenerate the portal catalog, or
  `test_catalog_generated` fails by design).
- **Assistants:** one read-only handler in `payprobe_common/agent_toolkit.py`
  (invariant #3), backed by REST in both backends. Read-only ⇒ no journal
  entry needed; if a write tool is ever added (e.g. rename-category via
  assistant), it gets journalling + `restore_one` like every other write.
- **Reports:** predictions may appear as *context* on Go/No-Go screens, never
  as an input to gate evaluation (ADR-0003's gates stay purely factual).

## 4. Persistence and training

- Service-local SQLite (`:memory:` in tests) beside file-backed stores — the
  house pattern. Tables: `failure_corpus` (normalized text + features + label
  + model_version), `categories` (stable id, name, heuristic mapping),
  `predictions_log` (every emitted prediction + eventual actual outcome — the
  self-scoring loop).
- Model artifacts: versioned joblib files beside the db
  (`models/cat-<ver>.joblib`), `INSIGHT_DATA_DIR` env. Loaded read-only at
  startup; `/status` reports versions.
- Training: incremental corpus ingestion on a schedule (poll orchestrator for
  new completed runs); full retrain manual or nightly. Retraining writes a
  *new* artifact version; old labels keep their `model_version` — append-only,
  never destructive.
- **Self-scoring:** because `predictions_log` records predicted vs actual, the
  service reports its own Brier score / calibration on `/status`. A model that
  scores worse than the frequency baseline is automatically demoted to
  baseline. This is the honesty mechanism that keeps Phase 1 from becoming
  decorative.

## 5. What the model never sees

Decrypted SecretBox content, key material, unmasked PANs, request/response
payload bodies (only assertion *diffs* and error text). The capture buffer's
PAN masking and the vault's read-mask happen upstream of everything this
service ingests. Error text is additionally run through the same normalizer
that masks numbers/ids before storage — a PAN that leaks into an error message
does not survive into the corpus.

## 6. Phase gates (the anti-over-engineering clause)

| Phase | Ships | Gate to proceed |
|---|---|---|
| 0 | API contract on orchestrator: heuristic categories + frequency predictions | — (do now, cheap) |
| 1 | `insight-service`: clustering, evidence packs, calibrated prediction | ≥500 categorized failures AND ≥20 runs for ≥10 scenarios in a live install; `error`/`unknown` bucket ≥15% of failures (else the regex ladder is sufficient and Phase 1 is not worth a service) |
| 2 | LLM-rendered explanations via standalone assistant | Phase 1 evidence packs exist and users actually open the "why" panel (portal telemetry / anecdote — don't build prose nobody reads) |

Each learned model replaces its baseline only after beating it on held-out
data; the baseline remains the permanent fallback (cold start, tiny installs,
model-load failure).

## 7. Deployment checklist (when Phase 1 starts)

- compose service + nginx route (`/api/insights`), `packages/` build context.
- Fail-closed JWT gate; service JWT for orchestrator reads.
- **Both** dashboard health panels (Endpoints probe + `/status` services map)
  + Settings→Endpoints entry — the documented two-panel gotcha.
- MCP tools + `gen_catalog.py` regeneration.
- `make test-insight-service`; suite runs per-package like the others.
- ATLAS §11 roadmap entry + this doc's status flipped when accepted/built.
