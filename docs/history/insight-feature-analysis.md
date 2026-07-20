# Insight feature-richness analysis (2026-07-12)

A deep pass over every data source the platform produces, asking one
question: **what signal exists that the insight models never see?** Each
finding says what the data actually contains (verified in code, not assumed),
what it's worth, and whether it was implemented now, deferred, or rejected.

## 1. Where the models are blind today

The categorizers (heuristic / clusters / custom) see exactly one thing: the
**normalized error string**. The predictor sees the per-scenario outcome
sequence. Everything else in a run is discarded at ingest.

### 1.1 Step responses — the biggest untapped channel  → **IMPLEMENTED**

`StepOutcome.response` is persisted verbatim in every run summary and carries
protocol-level facts the error string often lacks:

- ISO 8583 adapters shape `response_code` (DE39) into the payload
  (`worker/adapters/tcp/adapter.py` even logs `DE39=…`); the mock and HTTP
  paths return `response_code` / `status_code` / `auth_code`.
- `target` + `action` are stored **on the failure row itself** and never used
  as features — yet "timeout on the HSM" and "timeout on a REST gateway" are
  different diseases with identical error text.
- `duration_ms` separates instant refusals from slow deaths.
- failed assertions name the exact field that mismatched.

**Implementation: structured feature channels.** A `featurize` module derives
low-cardinality tokens — `tgt_hsm act_send_message rc_05 http_5xx env_staging
dur_slow asrt_response_code` — stored in a new `feats` column beside (never
inside) the normalized text. Training text = `feats + " " + norm` for both
the cluster and custom models; TF-IDF treats each channel token as vocabulary.
Deliberate invariants: the **signature stays a hash of the pure error text**
(dedup/bulk-correction semantics unchanged), and unknown tokens at inference
are simply absent (a plain-text `/infer` call still works). The predict
step's `categorize`/`explain` actions accept optional context
(`target`, `action`, `response_code`, `environment`, `duration_ms`) so
scenarios can supply the same channels.

### 1.2 Load runs — volume, but pre-bucketed  → **IMPLEMENTED (scoped)**

Verified shape (`worker/engine/load/driver.py`): load workers do NOT keep raw
error text. They keep `error_categories` = `{type-bucket: count}` (e.g.
`connect/TimeoutError: 12093`), deliberately bounded to 50 buckets, plus one
`last_error` sample. So load runs **cannot** enrich the text corpus the way I
originally hoped — there is no text. What they can do:

- put their failure **types** into the taxonomy/label-queue with honest
  counts (a soak that produced 40K `connect/TimeoutError` should dominate the
  impact ranking);
- carry `src_load` + target tokens so load-shaped failures cluster apart
  from scenario-shaped ones.

**Implementation:** sync now also walks new completed load runs and ingests
one corpus row per error category (norm = the category text + `last_error`
sample when it matches, feats = `src_load` + context), with the count carried
as an impact weight on the row. Scenario-outcome prediction is untouched —
load runs are not scenario outcomes.

### 1.3 Predictor features  → **IMPLEMENTED**

Two blind spots in the outcome features: (a) no *short-window* signal — a
scenario failing 3 of its last 5 after a stable year looks mild through the
long recency decay; (b) no *cadence* signal — a scenario that hasn't run in
two weeks carries more uncertainty than one run hourly. Added
`recent_fail_rate_5` and `log_gap_hours` (from `created_at` deltas).
Artifact compatibility: pickled models now carry a feature-dimension guard —
an artifact whose coefficients don't match the current feature vector is
discarded at load (retrained next cycle) instead of predicting garbage.

## 2. Deferred — and why (honesty section)

- **Chaos/storm correlation as features.** Verified: `GET
  /simulators/{sid}/chaos` returns *current* state only; storm windows are
  not persisted historically. Correlating a failure with "storm was active
  at the time" is only possible for runs ingested while the storm is still
  live — a best-effort signal too unreliable to train on. The right fix is
  upstream: persist storm windows in the orchestrator (small change, do it
  when chaos-aware features are actually wanted), then join on time ranges.
- **Network-trace hops** (which *downstream* participant actually failed).
  Rich but expensive: traces are capture-gated (off by default) and keyed by
  correlation id, so coverage would be sparse and biased toward runs where
  someone enabled capture. Revisit if trace capture becomes always-on.
- **Embedding-based text features** (sentence-transformers). Real gains on
  paraphrased errors, but a heavyweight model dependency against the repo's
  offline-test culture and the [ml]-extra quarantine. TF-IDF + channels
  first; embeddings only if the label queue shows TF-IDF confusing shapes a
  human finds obviously distinct.
- **Request payloads as features.** Rejected outright: requests carry PANs,
  amounts, keys. The masking normalizer would neuter most of the signal
  anyway, and the privacy budget isn't worth the residue.

## 3. Expected effect

Channels multiply the effective vocabulary exactly where failures diverge:
same text, different `tgt_*`/`rc_*` → separable clusters and custom-model
classes. Load-run ingestion grows the corpus past the 50-failure gate in one
soak test. The two predictor features attack the two most common
mis-predictions (sudden regression after stability; stale scenarios). All of
it remains behind the existing honesty gates — the cluster model still needs
`MIN_CORPUS`, the predictor still has to beat the frequency baseline on
holdout, and drift watch will say so if the richer features rot.
