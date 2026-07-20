# PayProbe Skill Library — Index

Sixteen skills that let a zero-context engineer or AI session debug, extend, validate,
and advance PayProbe. Authored 2026-07-03, reviewed and fixed 2026-07-06. Authoring
rules and provenance: `_AUTHORING-BRIEF.md` (maintainers only). Every skill ends with a
"Provenance and maintenance" section — run its re-verification commands before trusting
volatile facts (counts, flags, line numbers drift).

## The 16 skills

| Skill | One-line scope |
|---|---|
| payprobe-change-control | How changes are classified/gated/reviewed; the three non-negotiables with rationale and the incident behind each |
| payprobe-debugging-playbook | Symptom→cause→experiment triage for PayProbe failure modes; the traps that cost real time |
| payprobe-failure-archaeology | Settled battles: dead ends, removals, reverted designs — so nobody re-fights them |
| payprobe-architecture-contract | Load-bearing design decisions + WHY; invariants; known-weak points stated plainly |
| payments-domain-reference | ISO 8583 / EMV / PIN / DUKPT / HSM / scheme theory as implemented HERE, with extension-point maps |
| payprobe-config-and-flags | Every env var, file store, flag: defaults, prod-vs-experimental, add-a-flag checklist |
| payprobe-build-and-env | Recreate the dev environment from scratch; dependency traps |
| payprobe-run-and-operate | Compose bring-up, run/load-run/simulator/topology lifecycle, artifact locations, deploy/migrate |
| payprobe-diagnostics-and-tooling | Measure, don't eyeball: every diagnostic endpoint with interpretation guides + scripts/ |
| payprobe-validation-and-qa | Tests and thresholds that gate merges; evidence standards; golden inventory; adding tests |
| payprobe-docs-and-writing | Docs of record, Diátaxis map, ADR/spec/PROGRESS templates, house style |
| payprobe-external-positioning | License/claims discipline: source-available (NOT open source), what must be proven before claiming |
| payprobe-distributed-topology-campaign | Executable, decision-gated campaign for ADR-0001 (the hardest live problem) |
| payprobe-proof-and-analysis-toolkit | HOW to measure/prove: first-principles recipes with worked examples from repo history |
| payprobe-research-frontier | Four SOTA ambitions: why SOTA fails, PayProbe's asset, first three steps, falsifiable milestones |
| payprobe-research-methodology | WHEN a claim counts as proven: evidence bar, hypothesis-predicts-numbers, idea lifecycle |

## Routing by question (resolves the known ties)

| You are asking… | Load |
|---|---|
| "Something is broken / load run stuck in running — WHY?" | payprobe-debugging-playbook |
| "Fix/reconcile stranded runs, operate the platform" (the action) | payprobe-run-and-operate |
| "Crypto test fails locally": install/fix a missing dep | payprobe-build-and-env |
| — triage an unknown failure | payprobe-debugging-playbook |
| — regression vs environment call | payprobe-validation-and-qa |
| "Prove my fix is right": how to measure | payprobe-proof-and-analysis-toolkit |
| — when does it count as proven | payprobe-research-methodology |
| — which tests/thresholds gate the merge | payprobe-validation-and-qa |
| "Chaos storm": run it | payprobe-run-and-operate |
| — interpret/score it | payprobe-diagnostics-and-tooling |
| — design the experiment | payprobe-proof-and-analysis-toolkit |
| "Add/extend a dialect, field table, simulator rule, HSM command" | payments-domain-reference |
| "Has X been tried before?" / found deleted code in git | payprobe-failure-archaeology |
| "Can I claim/publish/release this?" | payprobe-external-positioning |
| "Add an env var / what does this flag do?" | payprobe-config-and-flags |
| "Work on ADR-0001 / distributed topology" | payprobe-distributed-topology-campaign |
| "Write an ADR/spec/doc" | payprobe-docs-and-writing |
| "What gates this change? Do I need a test/ADR/flag?" | payprobe-change-control |

## Non-negotiables (full rationale in payprobe-change-control)

1. **Measure, never assume** — no fix without a measured root cause / reproducing test.
2. **Reversibility mandatory** — behavior changes land flag-gated, default OFF, with a rollback path, before defaulting ON.
3. **Suite green before anything** — `make test` passes before and after every change.
