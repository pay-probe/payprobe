# Feasibility brief — an ML service that learns from PayProbe data

*Short brief, not an ADR. **Superseded 2026-07-12** by
[ADR-0005](../adr/0005-ml-insight-service.md) +
[the design doc](../architecture/insight-service.md); the service was built as
`packages/insight-service` covering categorize / explain / predict. The
`predict` **step** sketched below WAS subsequently adopted (2026-07-12), but
as a worker **adapter** (`insight`, catalog target "Model Insight") rather
than a new NodeKind — a plain `call` node covers it. Its output feeds the
scenario author's own if/switch control flow; the advise-only boundary holds
because report gates and sign-off never see it.*

## The idea in one line

Add a new package, `packages/ml-service` (port 8500), that trains models on the
data PayProbe already produces — runs, step outcomes, wire-level traces, load
runs, resilience certificates — and exposes them through **one new scenario
step** (`predict` node) plus read-only advisory endpoints. The model can
**categorize** (cluster/label failures), **suggest** (next steps, likely
faults), **decide** (a learned pass/anomaly verdict feeding sign-off), and
**predict** (response code / latency band / failure likelihood before a
transaction runs).

## Is it a good idea?

Yes, with one guardrail up front. It fits the platform thesis — *"does this make
the simulated network more faithful, or the verdict more trustworthy?"* Learned
categorization and anomaly detection make the verdict richer; prediction lets a
scenario branch on a *likely* outcome before paying for the round trip. All four
uses are natural extensions of data we already persist, which is exactly the
project's recurring lesson: **the system already knows most of what you'd ask
for — derive it before adding a knob.**

The guardrail: **the model advises; the registry and the gates still decide.**
This mirrors the assistant's design (§6 of ATLAS) — the model proposes, a
deterministic layer is the bouncer. A learned verdict must never *silently*
override a Go/No-Go gate; it feeds evidence into the gate, and a human or an
explicit rule signs off. That keeps sign-off auditable and tamper-evident
(ADR-0003), which is the whole point of the report machinery.

## Is it possible?

Yes — the seams already exist, so this is additive, not surgery.

**The data is there and already structured.** Every run persists `StepOutcome`s
with response codes, timings, assertions, and wire-level `raw_log` + engine
logs; load runs persist `error_categories`, `tps_series`, and stability; the
trace system stitches transactions across hops by correlation id;
resilience runs produce scored certificates. That is a labelled dataset almost
for free — pass/fail *is* the label for classification, response code and
latency *are* the regression targets, and `error_categories` is a ready-made
taxonomy to bootstrap clustering.

**The step catalog is data-driven.** New node types are declared as `ActionSpec`
entries with a `behavior` kind (today `adapter` / `http` / `code`). A `predict`
step slots in the same way — the portal palette, the `${step.response.*}`
autocomplete, and the variable-reference validator all pick it up from
`/catalog` with no editor rewrite.

**There's a clean place for the model call.** A `predict` node behaves like the
existing `http` node: at run time the worker POSTs the current context
(transaction fields, prior step outputs) to the ML service and exposes the
result as `${step_xxx.response.label}` / `.score` / `.suggestion`, so downstream
`if`/`switch` nodes can branch on it. The worker stays "dumb about config"
(§3) — the orchestrator resolves the ML endpoint like any other.

**Nothing forces heavy ML infra.** Phase 1 can be scikit-learn (gradient-boosted
trees / logistic regression / k-means) over tabular features — small, offline,
CPU-only, and testable with the repo's fake-first culture. Deep models are a
later option, not a prerequisite.

### What the four capabilities map to

| Capability | Model / method | Trained on | Surfaced as |
|---|---|---|---|
| **Categorize** | clustering + classifier over failure features | step outcomes + `error_categories` + trace hops | failure tag on the run report; auto-grouped "flaky/timeout/decline" buckets |
| **Suggest** | ranking over historical (context → next-step) pairs | scenario graphs + run histories | "likely next step" / "probable fault" hints in the editor's ✨ Ask AI and diagnostics |
| **Decide** | anomaly detection / learned classifier | resilience + load-run series | an advisory `anomaly_score` shown *beside* the deterministic gate — never replacing it |
| **Predict** | classification (resp. code) + regression (latency band) | per-transaction features | `predict` node output for pre-flight branching |

## What to build (design sketch)

1. **`packages/ml-service` (port 8500)** — FastAPI, same fail-closed JWT gate as
   every other service (§7). Endpoints: `POST /train` (build a model from a data
   pull), `GET /models`, `POST /predict`, `POST /categorize`, `POST /suggest`.
   Models are versioned artifacts persisted beside the other file-backed
   registries; a model card records what it was trained on (provenance, mirroring
   ADR-0003's "what ran against what").
2. **A feature/label extractor** that reads the orchestrator's run/load/trace
   history (read-only, via the existing service JWT bridge) and turns it into a
   training table. This is the real work — the model is the easy part.
3. **A `predict` node kind** — add `"predict"` to `NodeKind`, to `CONTROL_KINDS`,
   its partition to the validators, plus one `ActionSpec` with `behavior:
   {"kind": "predict", ...}`. Add MCP read tools (`ml_predict`, `ml_categorize`)
   for parity with the rest of the tool layer.
4. **Advisory surfacing** — a category/anomaly badge on the run report and a
   "model suggests" line in diagnostics. Read-only, clearly labelled as a model
   opinion, with the training-set version shown so a stale model is obvious.

## Trade-offs and risks (be honest)

- **Cold start.** A fresh install has little history; the model is weak until
  enough runs accumulate. Mitigation: ship with a bootstrapped model trained on
  the showcase/pack data, and label the model card with sample size.
- **Trust / silent authority.** The danger is a learned verdict quietly gaming a
  Go/No-Go. Non-negotiable: **advisory only, gates stay deterministic**, model
  output is separately labelled and versioned in the provenance record.
- **New external dependency + service.** Keep Phase 1 to scikit-learn / numpy so
  the offline, no-network test culture (§9) survives; no GPU, no model registry
  SaaS. Reversibility still holds — a model is a data artifact you can delete.
- **Reproducibility.** A prediction that can't be explained undermines auditable
  sign-off. Favour interpretable models (tree feature importances, cluster
  centroids) and store the feature vector with each prediction.
- **Scope creep into a second assistant.** The ✨ Ask AI and general assistant
  already exist; "suggest" must plug into them, not spawn a third brain. One
  toolkit, one place (§6).

## Phased plan

- **Phase 0 — spike (read-only, no new node).** Extract a training table from
  existing run/load history; train a failure classifier offline; report accuracy
  in a notebook/script. Decides whether the signal is real before we build UI.
- **Phase 1 — the service + `predict` node.** `ml-service` with `/train` +
  `/predict`, the `predict` node kind end-to-end, MCP read tools, tests with
  fake data. Categorize and Predict shipped.
- **Phase 2 — advisory surfacing.** Category/anomaly badges on reports and
  diagnostics; "model suggests" wired into the existing assistants. Suggest +
  Decide (advisory) shipped.
- **Phase 3 — hardening.** Model cards + provenance, scheduled retraining
  (the `schedule` machinery already exists), drift/staleness indicators
  (reuse the poll-health staleness pattern from the maps).

## The one thing to get right

Keep the boundary the platform already keeps everywhere: **the model configures
opinions and predictions; the deterministic registry and the gates remain the
authority.** Build it advisory-first, provenance-tracked, and reversible, and it
strengthens the verdict instead of muddying it.
