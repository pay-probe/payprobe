# Agentic OS — what Paperclip teaches, and what transfers to PayProbe

**Status:** Analysis. Third document in the agentic-engine thread, after
[`agentic-engine-evaluation.md`](agentic-engine-evaluation.md) (Opus) and
[`agentic-engine-evaluation-fable.md`](agentic-engine-evaluation-fable.md).
**Date:** 2026-08-21
**Subject:** [github.com/paperclipai/paperclip](https://github.com/paperclipai/paperclip)
(MIT, Node/TS + React), read at HEAD of master.

---

## 1. What Paperclip is

An open-source **control plane for teams of AI agents** — "if OpenClaw is an
employee, Paperclip is the company." A Node server + React UI that hires
arbitrary agent runtimes (Claude Code, Codex, Cursor, OpenClaw, any
process/HTTP endpoint), arranges them in an org chart with goals, and governs
their work through tasks, budgets, approvals, and audit. Its own framing of the
"Agentic OS" pillar: *cross-provider runtime, sandboxing, governed tool access,
RBAC, cost controls, trace collection.*

The org-chart/company metaphor is the marketing skin. Underneath it is a set of
**control-plane primitives** that are the actual answer to "what does an OS for
agents consist of" — and those primitives are what this analysis extracts.
Notably, Paperclip's server has ~80 service modules for governance alone; the
lesson to take is the primitives, emphatically not the surface area.

---

## 2. The primitives (with where they live in the code)

### 2.1 Heartbeats — agents do not run continuously

The single most transferable idea (`docs/agents-runtime.md`,
`services/heartbeat*.ts`). An agent is not a long-lived loop; it is a dormant
identity that wakes in **short, bounded execution windows**:

- Four wake sources: `timer`, `assignment` (work checked out to it),
  `on_demand`, `automation`.
- Wakeups on an already-running agent **coalesce** instead of stacking runs.
- Each heartbeat: start adapter → hand it current context → run until
  exit/timeout/cancel → persist status, token usage, logs → sleep.
- **Session resume**: the adapter session id is stored, so consecutive
  heartbeats continue one conversation; an operator can reset it when the
  context goes stale.

The consequence is the real architecture: **durable state lives in the task
records, not in the model's context window.** The agent re-derives "what am I
doing" from the DB on every wake. Crash-tolerance, cost-bounding, and
auditability all fall out of that one inversion.

### 2.2 Bring-your-own-agent via a mutable adapter registry

`packages/adapters/*`, `server/src/adapters/registry.ts`. Ten-plus adapters
(claude/codex/cursor/opencode local CLIs, gateways, generic `process`, generic
`http`) behind one interface; external adapters register into a mutable
registry, and the server — not a shared enum — is the source of truth for "is
this adapter real." The agent *identity* (role, budget, permissions, secrets)
is decoupled from the *runtime* that executes its heartbeats.

### 2.3 Budgets as enforced infrastructure, not etiquette

`services/budgets.ts`. Per-agent and per-company monthly budgets with soft
thresholds (→ approval request) and `hard_stop` (→ agent or whole company
**paused**, a budget *incident* opened, `budget.hard_threshold_crossed`
audit event emitted, new work refused with a reason). Enforcement sits in the
service layer where spend is recorded — the agent is never asked to respect its
budget; it simply stops being schedulable.

### 2.4 Execution policy — review/approval as a runtime state machine

`docs/guides/execution-policy.md`. Any task can carry an `executionPolicy`:
an always-on invariant (every run must post a comment back), then ordered
`review` / `approval` stages with agent-or-human participants. The runtime
routes the work — the original executor is excluded from reviewing itself, a
`changes_requested` bounces it back with the return-assignee recorded. Key
sentence from their doc: *"Instead of relying on agents to remember to hand off
work for review, the runtime enforces review and approval stages
automatically."* Guardrails in the machinery, not the prompt — independently
identical to PayProbe invariant #6.

### 2.5 Watchdogs + self-healing recovery — liveness, never correctness

`services/task-watchdogs.ts`, `services/recovery/*`. Watchdogs detect *stopped
subtrees* — tasks not terminal, but with no live run, no queued wakeup, nothing
that will ever move them — carefully, with a grace window so the evaluation
can't race a freshly-enqueued first run. Recovery has classified failure
origins, capped retries on an explicit backoff schedule (`0/60/120/240/480 s`,
max 5), "stranded notices" when it gives up, and repair of inconsistent
dispositions. What recovery **never** does: change what the task was, edit its
acceptance, or fake an outcome. It restores *liveness* and otherwise escalates
to a human. This is precisely the availability/evidence line both PayProbe
memos drew — arrived at independently, in production, by a system with much
more autonomy at stake.

### 2.6 The attention system — "does it need me" as a first-class query

`services/attention.ts`, `routes/decision-queues.ts`, `inbox-dismissals`.
Everything that might need a human — approvals, blockers, stalled work, budget
incidents, join requests — resolves into one prioritized inbox, with
**dismissals persisted**. That last detail matters: it makes alert fatigue
*measurable* (what fraction of surfaced items do humans dismiss unactioned?),
which is exactly the falsifier gate the PayProbe observer memo wanted and left
unmeasured.

### 2.7 Decision training — human judgments become a dataset

`routes/decision-training.ts`. Any human decision (an approval, an
interaction, an execution decision) can be captured as a training example with
notes — and the write path **rejects agent actors**: only humans may author
training data. An active-learning loop where the labels are the operator's real
decisions, with provenance, gathered as a by-product of governing.

### 2.8 Attribution — every action has an actor chain

Actor model threaded through every route: `actorType` (user/agent/system),
`agentId`, `runId`, `agentApiKeyId`, and `onBehalfOfUserId` → a
`responsibleUserId` even for agent actions. Plus scoped per-agent secret
bindings and a governed MCP tool gateway rather than raw tool access. An agent
is a *principal* — identity, grants, budget, audit trail — not a mode of some
other service. This is PayProbe's proposed Stage 0, shipped.

### 2.9 Enforced outcomes

Their roadmap names it directly: watchdogs, recovery actions, and review gates
exist so work terminates in *"merged code, published artifacts, shipped docs,
or explicit decisions — instead of vague status updates."* Every agent
engagement must end in an artifact the control plane can see.

---

## 3. What transfers to PayProbe — mapped onto the staged plan

| Paperclip primitive | PayProbe landing spot | Stage |
|---|---|---|
| Actor/principal model + per-agent secret bindings | The **agent principal** (Fable memo §5) — Paperclip confirms the shape: identity + grants + budget + attribution, on the existing auth-service JWT/`svc` pattern | 0 |
| Budget hard-stop as *unschedulability* + budget incident | Agent budgets enforced where spend is recorded; breach = principal stops being schedulable + an incident artifact — richer than the "cap in `dispatch`" both memos proposed | 0 |
| Heartbeat wake sources + coalescing | The **observer**: `schedule_store` is already the `timer` source; add `automation` (run-completed, gate-failed → wake) and coalesce — don't queue five evaluations of the same run | 1 |
| Attention inbox + persisted dismissals | The observer's delivery surface *and* its falsifier: dismissal tracking makes the ">50% dismissed = noise" gate measurable from day one | 1 |
| Watchdog "stopped subtree" detection | Directly reusable concept for run/network babysitting: a network run with no live traffic, no queued work, and no terminal status is *stranded* — today nothing notices | 1 |
| Recovery = liveness only, capped backoff, stranded notice | Validates and sharpens the "solve = availability repair only" column; adopt the explicit retry schedule + give-up notice rather than open-ended retry | 1 |
| Execution-policy state machine (runtime-routed review, executor ≠ reviewer) | The answer to Opus open question #5: agent-authored scenarios/plans get an execution policy — review stage before anything enters a certified run, enforced by the registry, not by prompt | 2 |
| Session-resume across heartbeats, state in the DB | The **certification planner**: plan + run reports are the durable state; each planner wake re-reads them. Supports plan-then-execute — Paperclip agents "hold the wheel" only *within* a bounded heartbeat, never across the whole job | 2 |
| Decision training (human-only writes) | The insight-service feedback loop ADR-0005 never specced: operator approve/reject/dismiss decisions become labeled examples for categorizer and observer alike | 3 |
| External task protocol (external tool = task source; host = governance) | Later, and the analogy is exact: Jira/CI as *certification-request* source, PayProbe as execution + governance control plane — same normalized-at-the-edge, idempotent-sync design | later |

## 4. What does not transfer

- **The org chart, goals, hires, multi-company.** PayProbe's agents are few and
  functional (observer, planner, triage) — role metaphors and reporting lines
  add narrative, not control. Skip the entire layer.
- **The BYO-agent adapter zoo.** Paperclip must run *any* agent runtime; the
  PayProbe assistant already has the provider-neutral caller seam, which is
  enough. One lesson does carry: keep principal (identity/grants) decoupled
  from runtime (which LLM executes), which the split between agent config and
  `ASSIST_LLM_*` already gives.
- **The scale.** Paperclip's control plane is ~80 server services *because
  governance is its entire product*. For PayProbe, governance is scaffolding
  around a testing product. Budget the primitives (§3) at the smallest version
  that holds; anything growing toward an inbox-with-triage product is scope
  creep.
- **Process-only verification.** Paperclip's deepest limitation: it governs
  *process* (was it reviewed, was it approved, did it stop) because it cannot
  check *correctness* — its work products are arbitrary. Its answer is human
  review stages everywhere. PayProbe has oracles (`certify`, gates,
  `iso_validate`, `score_resilience`) and can automate the verification step
  Paperclip must staff with humans. Copy their process spine; keep our
  verdicts. This asymmetry is PayProbe's structural advantage and the reason
  its agentic engine can be *more* autonomous per unit of risk.

## 5. Bearing on the Opus ↔ Fable disagreement

The one genuine split between the two memos was: may the agent hold the wheel
during a run (Opus, budgeted) or must it hand a plan to a deterministic
executor (Fable)? Paperclip's production answer is a third shape that mostly
sides with Fable while naming what Opus was reaching for:

> **Chunked autonomy.** The agent is autonomous only *inside* a bounded,
> budgeted heartbeat. Everything across heartbeats — what the job is, where it
> stands, what was decided — lives in durable records the runtime owns. There
> is no hours-long model-driven loop anywhere in the system; there are many
> short ones over shared durable state.

Applied to the certification planner: plan-then-execute stands, and the
"planner re-enters between runs" step from the Fable memo *is* a heartbeat —
wake on `automation` (run completed), read the report, propose an amendment,
exit. Opus's real concern — mid-run adaptation — becomes a watchdog/observer
wake during the run, not a planner holding the wheel. The heartbeat vocabulary
(wake sources, coalescing, session resume, state-in-records) is the missing
implementation language for both memos' Stage 1 and Stage 2, and PayProbe's
`schedule_store` is already its `timer` half.

## 6. Bottom line

Paperclip independently converged on the same governance instincts PayProbe's
ADRs encode — enforcement in the runtime not the prompt, liveness-repair not
outcome-repair, every action attributed — which is decent evidence those
instincts are load-bearing for anyone operating autonomous agents. What it
adds that the two memos lacked is an **operating model**: heartbeats over
durable state, budgets as schedulability, execution policies as state
machines, attention with persisted dismissals. Steal those four. Skip the
company. And keep the one thing it doesn't have: oracles.
