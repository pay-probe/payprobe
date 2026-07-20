# Reports Renovation — Design Notes

Status: proposal / brainstorm
Date: 2026-06-28

## Core premise

A PayProbe report serves **two distinct goals**, with different audiences,
cadences, and definitions of "good". The current report tries to be one thing
and ends up muddled. The renovation separates the two cleanly while keeping a
single run-data source.

| | **Improvement mode** | **Go/No-Go mode** |
|---|---|---|
| Audience | Engineer mid-loop | Approver at a decision point |
| Question | "What broke, why, did my change help?" | "Is this safe to ship — yes/no, and can I defend it?" |
| Cadence | Many times a day | Rarely, at a gate |
| Output | Live, high-detail, throwaway | Verdict, auditable, frozen artifact |
| Wins on | Detail + speed | Trust + auditability |
| 70% pass rate means | Useful progress | Hard NO-GO |

**Design principle: same run data, two renderings.** The engine produces one
result set per run. The report layer presents it either as a debugging view or
as a sign-off document. No engine fork.

- A `mode` selector (`improvement | signoff`) or two templates over the same
  `run_detail` dict in `report_service/generators.py`.
- A run done in improvement mode can be **promoted** to a sign-off report.
  That promotion is the bridge between the two goals.

---

## Goal 1 — Improvement mode (the debugging loop)

This is roughly what `run-report.component.ts` already is. Lean into it.

1. **Trend + flakiness inline.** We already compute `run_trend`, `flakiness`,
   and load-run comparison server-side but on separate pages. Surface a
   sparkline of the scenario's last N runs and a "flaky" flag directly on each
   test case in the report.
2. **Assertion-level diffing.** Today's compare is status-level
   (`index_steps` → passed/failed). Show *which* assertion changed
   (expected vs actual drift) and field-level value changes between runs.
3. **AI narrative summary.** Use `diagnose.py` + the LLM gateway to add a
   one-paragraph "what happened in this run" at the top — what failed, likely
   cause, what changed vs last time. Optional / cached.
4. **Coverage report.** Across a project: which adapters, message formats,
   MTIs, and fields were actually exercised vs defined in the catalog. A
   "what haven't we tested yet" view. Nothing like it exists today.
5. Keep the existing strengths: trace waterfall, wire dumps, per-step
   payloads, assertion tables.

Detail and speed win here; nothing is permanent.

---

## Goal 2 — Go/No-Go mode (production sign-off)

The report is the fact that gives the green light to run on production. It must
be trustworthy, complete, and auditable.

1. **Verdict first, unambiguous.** Top of the report = a single **GO / NO-GO**
   banner plus the criteria that produced it. Not a pass-rate you have to
   interpret. Example: "GO: 100% of P0 cases passed, scheme compliance 100%,
   0 regressions vs last green run." `certification.certified` is the seed;
   promote it to the headline.
2. **Explicit, configurable gates.** Make the rules that turn a run into
   GO/NO-GO explicit and tunable (e.g. P0 = 100%, P1 ≥ 95%, 0 regressions,
   coverage complete). NO-GO names exactly which gate failed.
3. **No silent gaps.** A green report that quietly skipped tests is worse than
   a red one. Surface `not_run` / `blocked` / `error` as loudly as failures.
   A run can't be GO if cases didn't actually execute. Add a coverage
   assertion: "all cases in pack X were run" — fail the gate on mismatch.
4. **Regression vs last GO run, not just previous run.** "vs previous" can
   compare two failing runs. Pin the baseline to the last *certified* run so
   the gate means "zero regressions from what actually shipped."
5. **Provenance / tamper-evidence.** Stamp each report with: scenario + pack
   versions, environment + resolved endpoint targets (host/port at runtime,
   not config names), build/commit of the system under test, who triggered it,
   timestamp, and a content hash.
6. **Immutability.** Once a run is certified, freeze it as an immutable
   snapshot. Improvement reports are live views; sign-off reports are frozen
   artifacts created on promotion.
7. **Approval trail on the artifact.** Who reviewed, who signed off, when,
   optional note. The report becomes the record of the decision, not just the
   test result.
8. **Environment honesty.** Make it visually obvious whether the run hit
   staging, a simulator, or prod-like infra. A GO is only valid for the
   environment tested — a green run against simulators must not be mistaken
   for a prod-readiness signal.

Trust and auditability win here; raw detail is supporting evidence, collapsed
by default.

---

## Shared / cross-cutting

1. **Reports hub (`/reports`).** Today a "report" = a single run. There's no
   landing page aggregating across runs/projects/schemes. Add a filterable
   index (project, scheme, env, date, pass-rate), saved views, and
   "compare any two runs" — not only "vs previous." Unifies the currently
   scattered flakiness / trends / certification / load-compare pages.
2. **PDF export.** We emit HTML and JUnit but no PDF. Sign-off summaries and
   certification badges are exactly what gets attached to change tickets and
   audits. Add a `pdf` generator alongside the HTML one.
3. **Exec / shareable summary.** A one-page, light-theme, branded variant:
   pass-rate donut, phase breakdown, top failures, scheme compliance %,
   duration vs last run. Generalize the `certification_html` badge instinct.
4. **Scheduled / emailed digests.** Tie into scheduled-tasks: a daily/weekly
   bundle of trend + flakiness + last cert status, delivered. Turns reports
   from pull to push.

---

## Open decisions

- **Sign-off: view or action?** Should sign-off mode be a *view* over any run,
  or should certifying be a deliberate **action** that freezes an artifact?
  Lean: action + immutable snapshot, for auditability.
- **Where do gates live?** Gate rules + provenance fields in `report_service`
  (pure functions, testable) vs orchestrator. Lean: `report_service`.
- **Gate config scope:** per-project, per-pack, or global defaults with
  per-project overrides.

---

## Rough phasing

1. **Foundations:** gate model (run → GO/NO-GO) + provenance fields in
   `report_service`; no-silent-skips. Everything else builds on this.
2. **Sign-off rendering:** verdict-first template, baseline-vs-last-green,
   PDF export, immutable snapshot + promotion action.
3. **Improvement polish:** inline trend/flakiness, assertion-level diff,
   AI narrative.
4. **Hub + reach:** `/reports` index, exec summary, scheduled digests,
   coverage report.
