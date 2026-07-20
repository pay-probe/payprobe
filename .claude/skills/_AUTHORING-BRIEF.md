# PayProbe Skill Library — Shared Authoring Brief

Author/maintainer-facing provenance document, NOT a skill — consumers should start at `README.md` in this directory. Binding rules for anyone editing or adding skills. Date of authorship: 2026-07-03.

## Mission
Build `.claude/skills/` so junior/mid-level engineers AND Sonnet-class AI sessions can debug, extend, validate, and advance PayProbe at distinguished-fellow standard without the original author.

## Audience
- Junior/mid human engineers AND Sonnet-class AI sessions equally. Optimize for BOTH: trigger-rich descriptions, copy-pasteable commands, explicit gates (for AI) plus narrative rationale (for humans).
- Assume zero PayProbe context. Payments jargon must be defined at first use.

## The retiring fellow's answers (fold into everything)
- **Hardest live problem:** implementing ADR-0001 — distributed topology on the worker fleet (participant flows currently run orchestrator-local; the accepted design moves them to Redis-coordinated external workers, reusing LoadBus + heartbeat machinery).
- **Unwritten non-negotiables (confirmed):**
  1. *Measure, never assume* — no fix ships without a measured root cause / reproducing test (see docs/history/PROGRESS.md "measured, not assumed").
  2. *Reversibility mandatory* — migrations/features land flag-gated with a rollback path before defaulting ON (see docs/history/DEFAULT-CONNECTION-MODEL-SPEC.md phases, docs/history/CONNECTION-ENV-MIGRATION-PLAN.md).
  3. *Suite green before anything* — `make test` must pass before and after every change; known failures may not accumulate.
  - (*No parallel stacks* — reuse existing seams (adapters, LoadBus, event transport) — is a strong observed convention (ADR-0001/0002 both reuse machinery) but was NOT confirmed as a hard rule; present it as a design convention, not a non-negotiable.)
- **Costliest past failures (emphasize):** (a) connection/env model churn — endpoints[]+selection built then removed, misaligned connection×environment matrix attempt, migration that ended a no-op; (b) silent degradation — undeclared pycryptodome silently erroring crypto nodes, edge-less path KeyError, historical broad exception swallowing; (c) portal/canvas churn — ngx-graph adopted then replaced by custom SVG canvas, Angular 17→22 upgrade chain, 2500-line constructor component.
- **"Beyond state of the art" means all four:** full-fidelity scheme simulation (spec-exact VISA/payShield/CyberSource + binary ISO 8583); time-travel observability (Chronoscope replay + network/execution trace as a debugging standard); AI-operated test platform (assistant + MCP + agent-tools operated safely by AI end-to-end); chaos-certified payment resilience (resilience certification + chaos dial as a rigorous, publishable methodology).

## Repo ground truth (verified 2026-07-03)
- Repo root: the payprobe repository checkout. NOTE: inside skills, always write commands relative to repo root (e.g. `cd <repo-root> && make test`) — never embed session/user-specific absolute paths in skill content.
- Layout: `packages/{worker,orchestrator,scenario-service,auth-service,mcp-server,payprobe-assistant,report_service,payprobe_common,portal,helpers}`, `docs/` (Diátaxis + `docs/adr/000{1,2,3}`), `examples/`, `infra/`, root specs (`*-SPEC.md`, `docs/history/MIGRATION-DEPLOY-RUNBOOK.md`), `docs/history/PROGRESS.md`, `ROADMAP.md`, `docs/history/project-review.md`, `docs/standards-gap-analysis.md`.
- Test entry: `make test` = `cd packages && python -m pytest worker/tests orchestrator/tests scenario-service/tests mcp-server/tests payprobe-assistant/tests -q`. CI: `.github/workflows/ci.yml` (worker, services, portal build, portal-e2e, mock-integration) + `security-scan.yml` (Syft SBOM, Trivy).
- License: PolyForm Noncommercial 1.0.0 — source-available, NOT open source. README carries an employer-independence disclaimer; never position as employer-affiliated or as OSS.

## Authoring rules (binding)
1. **Format:** `.claude/skills/<name>/SKILL.md` with YAML frontmatter: `name:` and a trigger-rich `description:` stating exactly WHEN a model should load it (symptoms, tasks, keywords). Optional `scripts/` dir for executable helpers.
2. **Voice:** imperative runbook voice. Copy-pasteable commands (repo-root-relative). Tables and checklists over prose walls. Define every jargon term once.
3. **Scope discipline:** each skill states when NOT to use it and which sibling skill to use instead (taxonomy below).
4. **GROUND TRUTH ONLY:** verify every command, flag, path, symbol, and claim against the repo before stating it. Run read-only checks (ls, grep, git log, pytest --collect-only). A wrong runbook is worse than none. If you cannot verify, label the claim `UNVERIFIED:` or omit it.
5. **No private paths as load-bearing sources:** embed the knowledge itself; do not cite user-home paths, Cowork session paths, or personal memory files.
6. **Date-stamp volatile facts** and end every skill with a **"Provenance and maintenance"** section: one-line re-verification commands for anything that may drift.
7. **No oversell:** unproven/unbuilt items stay labeled open/candidate/planned (ADR-0001 and 0002 are Proposed, not built; standards gaps are gaps). Nothing may contradict README/LICENSE/CONTRIBUTING or route around change control.
8. **Write ONLY inside `.claude/skills/<your-skill-dir>/`.** The rest of the repo is read-only. No mutating git commands.

## Taxonomy (16 skills — cross-reference by these names)
| Skill | One-line scope |
|---|---|
| payprobe-change-control | how changes are classified/gated/reviewed; the non-negotiables with rationale + historical incident behind each |
| payprobe-debugging-playbook | symptom→triage table; time-costing traps with their stories; discriminating experiments |
| payprobe-failure-archaeology | chronicle of investigations, dead ends, rejected fixes, removals: symptom→root cause→evidence→status |
| payprobe-architecture-contract | load-bearing design decisions + WHY; invariants that must hold; known-weak points |
| payments-domain-reference | ISO 8583/EMV/PIN/DUKPT/HSM/scheme theory as it applies HERE |
| payprobe-config-and-flags | every configuration axis: env vars, files, flags, defaults, prod-vs-experimental, add-a-flag checklist |
| payprobe-build-and-env | recreate the dev environment from scratch; known traps |
| payprobe-run-and-operate | running/deploying: compose anatomy, artifact conventions, what lands where |
| payprobe-diagnostics-and-tooling | measure-don't-eyeball: diagnostic endpoints/tools with interpretation guides + scripts/ |
| payprobe-validation-and-qa | what counts as evidence; acceptance thresholds; golden inventory; adding tests |
| payprobe-docs-and-writing | docs of record, Diátaxis, ADR/spec/PROGRESS templates, house style |
| payprobe-external-positioning | license/positioning/claims discipline; what must be proven before claiming |
| payprobe-distributed-topology-campaign | executable decision-gated campaign for ADR-0001 (hardest live problem) |
| payprobe-proof-and-analysis-toolkit | first-principles analysis recipes with worked examples from repo history |
| payprobe-research-frontier | the four SOTA ambitions: why SOTA fails, PayProbe's asset, first three steps, falsifiable milestones |
| payprobe-research-methodology | evidence bar, hypothesis-predicts-numbers, idea lifecycle flag→adopt/retire, adversarial refutation |
