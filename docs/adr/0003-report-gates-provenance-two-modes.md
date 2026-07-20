# ADR-0003: Report gates, provenance, and two-mode rendering

**Status:** Accepted — implemented (see Implementation status)
**Date:** 2026-06-28
**Deciders:** PayProbe maintainers (David + reviewers)

## Context

A PayProbe report is consumed for two unrelated jobs, and today it serves
neither cleanly:

- **Improvement** — an engineer mid-loop asking "what broke, why, did my change
  help?" High detail, run many times a day, throwaway.
- **Go/No-Go** — an approver at a gate asking "is this safe to ship, and can I
  defend that decision later?" A verdict, run rarely, and the artifact is
  *the fact that gives the green light to run on production*.

The current report (`packages/report_service/generators.py` +
`packages/portal/src/app/run-monitor/run-report.component.ts`) is built for the
first job: a dark, expand-everything view with a phase summary, per-step
assertions/payloads, a trace waterfall, and a "vs previous run" panel. Used as a
sign-off it is unsafe:

- The headline is a **pass-rate you must interpret**, not a verdict. There is no
  explicit go/no-go rule.
- `not_run` / `blocked` steps render as quiet grey rows — a run that **silently
  skipped** cases can look green. A sign-off must treat absence of evidence as
  failure, not success.
- "vs previous run" (`generators.index_steps` → status diff) can compare two
  *failing* runs. A sign-off needs "vs the last **approved** run."
- Nothing records **what was tested against what**: no system-under-test build,
  no resolved endpoint targets, no who/when, no content hash. The report is not
  auditable and not tamper-evident.
- Reports are **live views** that change as data changes. A sign-off must be an
  immutable snapshot of the decision.

### What we can reuse

- `generators.certify(run_detail, pack)` already scores a run against a pack and
  emits `compliance_pct` + `certified = (passed == total)`. This is the seed of
  a gate, but it is a single all-or-nothing rule with no tiers and no
  no-silent-skips check.
- `generators.html_report` / `junit_xml` / `certification_html` are pure
  functions over a `detail` dict — the right seam to add a second rendering.
- `diagnose.diagnose(detail)` already produces ranked findings + a headline; it
  becomes the supporting evidence under a verdict, not the verdict itself.
- Run detail already carries `id`, `status`, `environment`, `started_at`,
  `completed_at`, and `summary.{phases,scenarios[].steps[]}`.

### Forces

- One run, two renderings — do **not** fork the engine or re-run tests.
- Gate logic and provenance must be **pure and testable** (the `report_service`
  pattern), not buried in the orchestrator or the portal.
- A sign-off artifact must be reproducible and immutable once issued.
- Don't regress the improvement loop; it stays the fast, detailed default.

## Decision

Introduce three pure additions to `report_service`, plus a `mode` on the report
surface, plus a deliberate **certify (promote) action** that freezes an
artifact.

### 1. A gate model (`report_service/gates.py`)

A `GatePolicy` evaluates a `run_detail` (+ optional pack + baseline) into a
verdict. Rules are explicit and configurable; the evaluation is the headline.

```jsonc
// GatePolicy — config, per-project with global defaults
{
  "tiers": { "P0": 100, "P1": 95, "P2": 0 },   // min pass-% required per priority
  "require_coverage": true,        // every case in the pack must have run
  "forbid_not_run": true,          // not_run / blocked / pending => NO-GO
  "max_regressions": 0,            // vs the baseline (last GO run)
  "allowed_environments": ["staging", "prod-like"]  // sim-only => NO-GO
}
```

```jsonc
// GateResult — the verdict
{
  "verdict": "GO",                 // GO | NO_GO
  "gates": [
    {"id": "P0", "ok": true,  "detail": "12/12 P0 passed (100%)"},
    {"id": "coverage", "ok": true, "detail": "all 18 pack cases ran"},
    {"id": "no_silent_skips", "ok": true, "detail": "0 not_run / blocked"},
    {"id": "regressions", "ok": true, "detail": "0 vs last GO run 7af3…"},
    {"id": "environment", "ok": true, "detail": "ran against staging"}
  ],
  "blocking": []                   // ids of failed gates; non-empty => NO_GO
}
```

`certify()` is refactored to delegate scoring; gates layer tiers +
no-silent-skips + coverage + regression + environment on top.

### 2. Provenance (`report_service/provenance.py`)

A `provenance(detail, ctx)` stamp embedded in every sign-off artifact, so the
report proves what was tested against what:

```jsonc
{
  "run_id": "…",
  "scenario_versions": { "auth-approve": "v4", … },
  "pack": { "id": "visa-base1", "version": "2.1" },
  "environment": "staging",
  "endpoints": [ {"target": "issuer", "host": "10.0.1.50", "port": 7000} ],
  "system_under_test": { "build": "abcd123", "source": "git" },
  "triggered_by": "david@…",
  "started_at": "…", "completed_at": "…",
  "baseline_run_id": "7af3…",          // last GO run compared against
  "content_hash": "sha256:…"           // hash of the frozen result + policy
}
```

Endpoints are the **runtime-resolved** host/port (not config names), reusing the
same resolution the orchestrator already performs at run start.

### 3. Two renderings over one run

A `mode` selects how the same `run_detail` is rendered:

- `improvement` (default) — today's live, detailed view. Unchanged.
- `signoff` — verdict-first: GO/NO-GO banner + `GateResult`, provenance block,
  `diagnose` findings and per-step detail collapsed below as evidence,
  exportable to PDF.

In the portal this is a tab/route switch over the existing component data; in
`generators.py` it is a second template (`signoff_html`) + a new `pdf` export.

### 4. Certify = a deliberate, freezing action

Sign-off reports are **not** live views. Certifying a run is an explicit action
(`POST /runs/{id}/certify` with a `GatePolicy`) that:

1. Evaluates gates, builds provenance, computes `content_hash`.
2. Writes an **immutable** `SignoffReport` snapshot (file-backed, `SecretBox` at
   rest like other secret-bearing stores; endpoints/PANs masked).
3. Records an **approval trail** entry (who certified, when, optional note).

A green improvement run is *promoted* into a sign-off artifact — the bridge
between the two goals. Re-certifying creates a new snapshot; existing ones are
never mutated.

## Options Considered

### Option A — Gates + provenance in `report_service`, two templates, freeze on certify (chosen)

| Dimension | Assessment |
|---|---|
| Reuse | High — extends `certify`/`html_report`/`diagnose`; same pure-function pattern. |
| Testability | High — gate + provenance are pure dict→dict, unit-testable. |
| Auditability | Strong — immutable snapshot + content hash + approval trail. |
| Blast radius | Low — improvement path untouched; new files + one endpoint + one template. |

### Option B — Add a "pass threshold" flag to the existing report, no freeze

Smallest change: parameterise the pass-rate and recolour the badge. Rejected —
still a live view (not auditable), no tiers, no no-silent-skips, no provenance.
It dresses up a pass-rate as a verdict without making it defensible.

### Option C — A separate certification service / package

Cleanest isolation but forks scoring, run access, and store plumbing for a
capability that is ~90% reuse of `report_service`. Rejected for v1; revisit only
if sign-off needs its own deployment/retention lifecycle.

## Consequences

**Positive**

- The verdict is explicit, tiered, and defensible; a sign-off can't be faked by
  a silently-skipped suite.
- Sign-off artifacts are immutable, hashed, and carry an approval trail — they
  survive an audit.
- Regressions are measured against the **last GO run**, the only baseline that
  matters for shipping.
- The improvement loop is unchanged and stays the fast default.

**Negative / risks**

- Gate policy is now a config surface to get right; bad thresholds give false
  confidence. Ship sane defaults (P0 = 100%, no silent skips, 0 regressions) and
  make overrides explicit and reviewed.
- Provenance exposes endpoints/PANs — masking + `SecretBox` at rest are
  mandatory, mirroring the Secrets Vault posture.
- "Last GO run" needs a durable pointer per (project, pack, environment);
  defining its scope wrong makes the baseline meaningless.

## Rollout / sequencing

1. `gates.py` (GatePolicy/GateResult) + refactor `certify` to delegate; unit
   tests incl. the no-silent-skips and coverage cases.
2. `provenance.py` + content hash; wire runtime endpoint resolution.
3. `signoff_html` template + verdict-first portal view (reuse run-report data).
4. PDF export of the sign-off artifact.
5. `POST /runs/{id}/certify` → immutable `SignoffReport` store + approval trail.
6. "Last GO run" baseline pointer + regression gate against it.

## Implementation status

Built (with tests):

- `report_service/gates.py` — `GatePolicy` + `evaluate_gates` (tiers, coverage,
  no-silent-skips, regression, environment); `certify` refactored to share
  `score_cases`.
- `report_service/provenance.py` — `provenance` stamp + canonical, tamper-evident
  `content_hash`.
- `report_service/generators.py` — `signoff_html`, a printable light-theme
  verdict-first document (browser / headless-Chrome Print → PDF).
- `orchestrator/api/signoff_store.py` — immutable snapshot store + approval trail
  + baseline pointer per (project, pack, environment); `SecretBox` encryption at
  rest (shared `payprobe_common.crypto`).
- `payprobe_common` — new shared package holding `SecretBox`, consumed by the
  orchestrator (no cross-service dependency edge). scenario-service keeps its own
  identical, interoperable copy for now (separate build context — see Remaining).
- Endpoints in provenance are runtime-resolved (`_resolved_endpoints` stamps the
  run summary at completion).
- Orchestrator endpoints: `POST /runs/{id}/certify`, `GET /signoffs`,
  `GET /signoffs/{id}`, `GET /signoffs/{id}/html`, `POST /signoffs/{id}/approve`.
- Portal: `reports/:id/signoff` view (verdict banner, gates, provenance,
  approval trail, certify form, PDF/print) + link from the run report.
- MCP: `certify_run`, `list_signoffs`, `get_signoff`, `approve_signoff`
  (registry + regenerated catalog).

## Remaining work (deployment-level, not code gaps)

- **Runtime endpoint resolution.** DONE — `_resolved_endpoints(env)` stamps the
  run's resolved adapter `{target, host, port}` onto the summary at completion,
  and `_signoff_endpoints()` reads it (falling back to run-level for older runs),
  so provenance carries the real targets.
- **Server-side PDF bytes.** Today PDF = Print on the self-contained HTML. If a
  downloadable `.pdf` from the API is wanted, add a renderer (WeasyPrint or
  headless Chrome) to the orchestrator image — an infra/deps decision, kept out
  of the dependency-free `report_service`.
- **Encryption-at-rest wiring.** DONE — new shared `payprobe_common.crypto`
  package holds `SecretBox`; the orchestrator consumes it via COPY + `PYTHONPATH`
  (like worker/report_service) and constructs `SignoffStore(box=SecretBox())`,
  with `cryptography` added to the image. With `PAYPROBE_SECRET_KEY` set,
  secret-named fields in snapshots are ciphertext at rest and plaintext on read;
  no key = passthrough. No orchestrator→scenario-service dependency edge.
  Follow-up: scenario-service still keeps its own identical `crypto.py` (same
  `enc:v1:` format, interoperable) because its container builds from its own
  directory context and can't vendor a sibling package — converge it onto
  `payprobe_common` once that build moves to a repo-root context.
- **MCP SDK in CI.** `test_registry` / `test_catalog_generated` skip without the
  `mcp` package; ensure CI installs it so registry/catalog drift is caught.

## Open questions

- **Baseline scope:** is "last GO run" keyed per project, per pack, per
  environment, or all three? (Leaning all three — a GO is only valid for the
  env + pack it ran.)
- **Policy ownership:** who can edit a `GatePolicy`, and is a policy change
  itself audit-logged? (Leaning yes — the gate rules are part of the evidence.)
- **Approval model:** is certify single-actor, or does it need a separate
  reviewer/approver (four-eyes) before an artifact counts as GO?
- **Retention:** how long are sign-off snapshots kept, and where — alongside run
  history or a dedicated immutable store?
