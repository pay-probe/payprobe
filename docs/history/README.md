# docs/history — the build record

Finished working documents, kept as the honest record of how each subsystem
landed. Nothing in here is maintained as current documentation — for that, see
[docs/](../README.md), [ATLAS.md](../ATLAS.md) and [the ADRs](../adr/). Each
spec carries its own status stamp from the time it was executed.

| Document | What it was |
|---|---|
| `PROGRESS.md` | Iteration log of the autonomous test-infra hardening campaign (2026-06) |
| `project-review.md` | Periodic whole-codebase honest assessments (reviews 1–4) |
| `PLAN-end-to-end-mock.md` | Plan that took the mock stack end-to-end |
| `TEST-CONSOLE-BUILD-SPEC.md` | Load/test console build spec (P0 generators, DUKPT/PVV) |
| `TEST-DATA-MANAGER-SPEC.md` | Test Data Manager phases 1–3 build spec |
| `SECRETS-VAULT-SPEC.md` | Secrets vault build spec (SecretBox at rest, masked inventory) |
| `DEFAULT-CONNECTION-MODEL-SPEC.md` | The connection model rework (single `port`, override matrix) |
| `CONNECTION-ENV-MIGRATION-PLAN.md` | Migration plan for the connection×environment model |
| `CONNECTION-ADAPTER-EXTENSION-SCOPE.md` | Adapter extension scoping for connections |
| `MIGRATION-DEPLOY-RUNBOOK.md` | Deploy/migration runbook (dry-run-first pattern) |
| `PARTICIPANT-FLOW-BUILD-SPEC.md` | Participant flows build spec (long-lived listeners) |
| `PROXY-TAP-BUILD-SPEC.md` | TCP proxy/tap build spec (ADR-0002 stage 1) |
| `GENERAL-ASSISTANT-BUILD-SPEC.md` | The general config assistant build spec |
| `standards-gap-analysis.md` | Gap analysis vs. payment-industry standards |
| `reports-renovation.md` | Reports renovation proposal (led to ADR-0003) |
| `insight-feature-analysis.md` · `ml-service-feasibility.md` | Analyses behind ADR-0005 (insight service) |
| `website-*-prompt.md` | Working prompts used to build payprobe.io pages |
| `sample-run-report.html` · `nats-demo-issuer-flow.json` | Sample artifacts from development |

These files were moved here from the repo root (and loose `docs/`) on
2026-07-18. New build specs should be created here directly — the convention
is documented in `.claude/skills/payprobe-docs-and-writing`.
