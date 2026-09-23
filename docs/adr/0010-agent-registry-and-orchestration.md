# ADR-0010: Agent registry and orchestration (agent-hub)

**Status:** Proposed — phases 1 (registry) and 2 (heartbeat runner) implemented 2026-09-23
**Date:** 2026-09-22
**Deciders:** PayProbe maintainers (David + reviewers)

> **Implementation summary (2026-09-23).** Phase 1 landed: `packages/agent-hub`
> (registry of agent principals and workflow definitions on PostgreSQL, 49 tests
> green against Postgres 16 in the sandbox), compose/CI/Makefile/nginx wiring,
> an `agent-hub` entry in the orchestrator `/status` map, and the portal
> **Agents** page (Configure → Agents: list, version history, JSON draft editing,
> validate/publish/retire, global pause switch, PollHealth staleness, Settings →
> Endpoints entry). The portal was built in the sandbox with Node 22.22.3
> (`ng build --configuration production`, exit 0, font inlining disabled locally
> because Google Fonts is unreachable there; `angular.json` is unchanged). Still
> owed: host `make test` (the sandbox cannot import `iso8583`, so the
> orchestrator `test_observability` change is written to pattern, not run), a
> host build with fonts, a click-through of the Agents page, and the CI workflow
> hunk applied by hand.
>
> **Phase 2 (2026-09-23).** Heartbeat runner landed: `ToolScope` +
> `scoped_dispatch` in the one tool layer (allowlist, mode tiers, write scope,
> 64 KiB result cap, untrusted envelope); the REST backend and the provider
> caller moved into `payprobe_common` (the assistant now subclasses/imports
> them; its 63 tests unchanged); per-heartbeat on-behalf-of JWTs with an `act`
> claim; `agent_hub_heartbeats` (migration 2) with coalescing enforced by a
> partial unique index; `POST /agents/{name}/wake`, `/heartbeats`, cancel and
> revert; pause and daily-budget refusals recorded as heartbeats; `FakeLLMBackend`
> for CI. 69 agent-hub tests, 529 across four packages in one session. The
> portal heartbeat view followed the same day (Configure → Agents, under the
> selected agent: list, step waterfall, proposed plan, Wake / Cancel / Revert,
> PollHealth; host production build green, browser click-through owed), then
> the D11 alert webhook (`agent_hub/alerts.py`: signed, retried, fire-and-forget
> POST on `failed` / `budget_exceeded` / `timed_out` heartbeats and on advisor
> findings at `warn`+, `AGENT_HUB_ALERT_WEBHOOK_URL` + `_SECRET`, off by
> default) and the restart watchdog (`reconcile_running`: orphaned `running`
> rows become `failed` and alert). 81 agent-hub tests. Nothing from phase 2 is
> owed except a browser click-through and a real provider call.
>
> **Phase 3, engine core (2026-09-23).** `agent_hub/engine.py`: a persisted
> state machine over `agent_hub_runs` / `agent_hub_approvals` /
> `agent_hub_plans` (migration 3). Node executors for all six types;
> `agent_task` reuses the wake code path (`launch_heartbeat`, extracted from
> the route) under a narrowed mode; `condition` uses a whitelisted-AST
> evaluator (`exprs.py`, no `eval`); `approval` parks the run as `waiting`
> and alerts `approval.requested`; plan-mode proposals become plan artifacts.
> Routes: `POST /workflows/{name}/run`, `/runs`, cancel, `/approvals` inbox +
> decide, `/plans`. Seeds: `plan-executor` agent (full, approval-gated) and
> the reference workflows `observer` and `certification-plan`. Startup
> reconcile re-attaches running agent tasks and expires timed-out approvals.
> 117 agent-hub tests, including: human approval mid-flow, reviewer blocking a
> bad plan before anyone is asked, approved plan applied by the executor, a
> restart mid-run resumed from the row. Portal followed the same day: an
> approvals inbox above the Workflows tab (pending count on the tab, context,
> approve/reject with note, role-gated) and a runs view under each workflow
> (start with inputs, node-state table with heartbeat/plan/approval refs,
> cancel); host production build green, browser click-through owed.
>
> **D2, first half (2026-09-23).** The :8400 assistant's FastAPI app is
> mounted inside agent-hub under `/assistant`, unchanged (its own gate,
> session and chat stores, Redis when `REDIS_URL` is set); its lifespan runs
> from agent-hub's. All three nginx confs route `/api/assistant/` to
> `agent-hub/assistant/` and the orchestrator probes
> `agent-hub:8600/assistant/health` (public, answered by agent-hub). The
> `assistant` compose service stays this release as the deprecated alias and
> is removed in phase 5. Deploy order matters once: rebuild agent-hub before
> reloading the portal's nginx. Deliberately deferred: replacing the chat
> runtime with heartbeats of the `config` agent (each turn a recorded,
> revertable heartbeat); that changes the assistant UX and is a separate
> decision. Owed from phase 3: a real provider run of both reference
> workflows.
>
> **Phase 4, wake sources (2026-09-23).** `agent_hub/triggers.py`: `POST
> /events` (platform service token or admin) wakes every active agent whose
> spec declares a matching `event` trigger, one heartbeat each with the event
> as input under the principal `event:<name>` (no roles, so an event-woken
> agent can read but any write is refused downstream until a workflow with an
> approval carries a human's authority); a 30 s tick wakes `schedule`
> triggers (`interval_sec`, `daily_at` UTC) as `scheduler`, never while
> paused (`AGENT_HUB_SCHEDULER=0` disables). The orchestrator emits
> `run.completed` / `run.failed` when a functional or flow-debug run ends and
> `gate.failed` when a certification verdict is not GO, fire-and-forget,
> only when `AGENT_HUB_API_URL` is set. Seeds already declare these wakes:
> `observer` (15 min schedule + `run.failed` + `gate.failed`),
> `failure-triage` (`run.failed`), `certification-planner`
> (`run.completed`). 124 agent-hub tests, 386 orchestrator tests. Owed from
> phase 4: the webhook trigger, MCP catalog entries for wake/heartbeats/runs,
> insight-service as a first-class tool, and the unattended end-to-end proof
> against a real provider (a failing scheduled regression waking `observer`
> and a finding reaching a human through the alert webhook).
>
> **First real run (2026-09-23 13:23 UTC).** Thirty seconds after the phase-4
> deploy, the scheduler woke `observer` unattended against the configured
> provider (`claude-haiku-4-5`, key from Settings): 12 steps, 9 read-only tool
> calls, 27.5k tokens in / 2k out, 23 s, status `done`, and two correct
> critical findings (a scenario failing 25 consecutive runs on one assertion;
> a 93% next-failure prediction). It also answered as a markdown report with
> a fenced JSON block, not bare JSON, which the finding parser would have
> missed; `extract_json` now finds fenced or embedded JSON in both the alert
> path and the engine's `${node.json}` context, with that output as the test.

Companions: [`../agentic-engine-evaluation.md`](../agentic-engine-evaluation.md)
(Opus), [`../agentic-engine-evaluation-fable.md`](../agentic-engine-evaluation-fable.md)
(Fable), [`../agentic-os-paperclip-analysis.md`](../agentic-os-paperclip-analysis.md).
This ADR decides what those three left open and adopts everything they agreed on.

## Context

PayProbe already has the hard parts of an agent stack: one tool layer with
guardrails inside it (`payprobe_common/agent_toolkit.py`, invariants #3 and #6),
journalled reversible writes (#2), caller-JWT gating on every assistant, and
three separate AI containers (assistant :8400, MCP :8200, insight :8500). What
it lacks is a control plane: nowhere to define a second agent, scope it,
version it, wake it on an event, chain it with a reviewer, or answer "why did
the agent do X" from a run record.

The three memos settled the shape (they are treated as accepted here):

- **Advise-only survives autonomy.** The agent decides, proposes and explains;
  it never executes inside the evidence path and never decides whether the
  evidence is good (ADR-0003, ADR-0005, ADR-0009 precedent).
- **Plan-then-execute.** Agents author durable, reviewable plan artifacts; a
  deterministic pipeline executes them; the agent re-enters between runs.
- **Chunked autonomy.** An agent is autonomous only inside a short, bounded
  heartbeat. Everything across heartbeats lives in durable records the runtime
  owns. There is no hours-long model-driven loop anywhere.
- **Stage 0 is a principal, not a mode.** Identity, grants, budget and
  attribution on the auth-service JWT pattern, plus a provenance stamp on
  agent-authored artifacts and an outbound alert webhook.
- **Every agentic feature degrades to a deterministic floor** (the
  offline-deployment rule). Banks with no LLM egress must lose nothing.
- **Sequence:** observer first (advise-only), certification planner second,
  deterministic fuzzer in parallel, online LLM participants cut.

Constraints that shape the answer: the certified artifact must stay
reproducible; captured traffic (ISO 8583 fields, traces, simulator output) is
untrusted input and the obvious prompt-injection vector; portal changes need a
host build; every phase ends in a commit (uncommitted work has been lost three
times on this repo).

## Decision

Build a new container, **`agent-hub`** (:8600), holding the registry, the
heartbeat runner, the workflow engine and the approvals inbox. It imports the
toolkit and journal as a library and persists to the **platform PostgreSQL**.
All platform side effects from any agent pass through one scoped toolkit
instance built from the agent's registry entry.

| # | Decision |
|---|----------|
| D1 | **Workflows are JSON DAGs in v1.** No canvas authoring in this initiative; a fourth graph level is a later ADR. |
| D2 | **The :8400 assistant is folded into agent-hub in phase 3.** Phase 1 registers `config` as a builtin definition; phase 3 moves execution into agent-hub; :8400 becomes a compose alias for one release, then goes. |
| D3 | **Default posture: `plan` mode everywhere.** `full` mode on a non-mock environment requires an `approval` node; a workflow node may narrow an agent's registered mode, never escalate it. |
| D4 | **One LLM provider, configured once** (Settings → AI assistant; `ASSIST_LLM_*` env override wins). Per-agent `model` and `temperature` within that provider; no per-agent provider. |
| D5 | **Separate container.** The orchestrator stays LLM-free so that it *is* the deterministic floor; agent-hub is the only new LLM egress point and its own failure domain. |
| D6 | **PostgreSQL, not a file.** Registry, plans, runs, approvals and budgets are durable, cross-replica state in the platform database, with numbered idempotent migrations. No SQLite or JSON-file fallback: a missing DSN fails startup. |
| D7 | **Heartbeats over durable records.** An agent run is a sequence of bounded wakes (`timer`, `event`, `on_demand`, `mcp`), coalesced per agent; limits are per heartbeat; the budget is a schedulability budget (hard stop = unschedulable + incident). |
| D8 | **Execution policy as a state machine.** Planner → executor → reviewer → human approval → apply, persisted in agent-hub; executor and reviewer must differ; every heartbeat ends in a record the control plane can see. |
| D9 | **Tool access is enforced in the tool layer only.** Allowlist, mode, write scope and result caps live in `build_scoped_toolkit()`, never in prompts. |
| D10 | **Agents act under short-lived on-behalf-of JWTs** minted by auth-service (`act` = agent id, version, run id; `sub` = the invoking user), so RBAC and audit remain per human. |
| D11 | **Provenance stamp + alert webhook belong to Stage 0** (phases 1 and 2), per the Fable memo. |
| D12 | **Two new CLAUDE.md invariants** land with the runner (phase 2): agents get tools only through a scoped toolkit; no agent side effect without journal and gate. |

D1 to D4 were confirmed by David on 2026-09-22; D6 on 2026-09-23 (an
initial SQLite implementation was rejected as a mistake and replaced).

## Options considered

### A: separate container `agent-hub` on the platform Postgres (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium: one new service, its own tables in the shared database, toolkit and journal reused as a library |
| Failure isolation | High: a looping agent or provider outage restarts one container |
| Security boundary | High: single LLM egress point; outbound network can be restricted on that container |
| Evidence purity | High: the orchestrator executes only deterministic pipelines and stays the floor |
| Operability | Medium: one more image, one more `/status` entry; matches :8200 / :8400 / :8500 precedent |

### B: agent runs inside the orchestrator (Opus memo §4.1)

Reuses `TOPOLOGY_RUNS`, `schedule_store` and Redis run control. Rejected
because it puts LLM code in the service that must remain the deterministic
floor, and because a runaway agent would share a process with the execution
engine. The reusable part of the idea (durable, resumable, budgeted runs)
is taken as D7, in agent-hub.

### C: extend the :8400 assistant in place

Fewest moving parts on day one; rejected because the service is shaped for
one prompt and one session model, so registry, engine and approvals would be
bolted on and the fold-in migration would happen anyway, later and messier.

### D: SQLite on a volume (as `RUN_DB` and insight do)

Implemented first, rejected on review. The registry is cross-replica control
state (pause flag, approvals, budgets, plans a run record pins by hash); a
per-container file makes every one of those a single-replica assumption, and
the platform already runs Postgres for exactly this class of state. Kept here
so the question is not reopened.

## Trade-off analysis

Failure and egress isolation decide A over B and C: an agent that can start
runs, retune load and edit registries is the most privileged client on the
platform, and the cost of one extra container is small next to sharing a
process with the services it drives. Plan-then-execute (D8) trades mid-run
adaptation for evidence purity as a structural property; if mid-run reaction
ever matters it is the observer raising a finding, not the planner holding the
wheel. JSON DAGs (D1) trade authoring UX for testability: a workflow is a
fixture that runs under a scripted LLM in CI. One provider (D4) keeps key
management where it already lives and avoids a per-agent secrets surface.

## Design

### Registry (phase 1, implemented)

Two tables serve both kinds (`agent`, `workflow`): `agent_hub_definitions`
(identity, status, builtin) and `agent_hub_versions` (immutable once published,
`spec_sha256` provenance hash, one active version per definition enforced by a
partial unique index). `agent_hub_meta` holds the pause flag;
`agent_hub_migrations` records applied schema versions. Lifecycle:

    draft ──publish──▶ active ──(next publish)──▶ superseded
                         │
                       retire ──▶ retired

`name` resolves to the active version; `name@N` resolves while N is active or
superseded, so pinned workflows survive a newer publish. Builtins cannot be
retired.

**AgentSpec:** role, instructions (≤ 20k chars), model, temperature, examples,
tool allowlist, mode (`advisor` | `plan` | `full`, default `plan`),
write_scope {projects, environments}, limits per heartbeat {max_steps,
max_tokens, wall_clock_s}, budget {daily_tokens, hard_stop}, triggers
(`manual` | `schedule` | `event` | `mcp`), rbac {invoke, edit}.

**WorkflowSpec:** inputs, nodes (`agent_task` | `tool` | `condition` |
`approval` | `parallel` | `join`), edges with optional `when` guards, `end`
sentinel.

**Publish-time validation** (guardrails in machinery): every tool name must
exist in the toolkit registry; `advisor` may hold read tier only; `full` needs
a non-empty write scope; workflow must be acyclic and reference resolvable
agents; a node may not escalate an agent past its registered mode; a `full`
task outside `mock` needs an `approval` ancestor (D3); executor ≠ reviewer.

**Seeds (builtin, published on first start):** `config`, `scenario-author`,
`observer` (advise-only, timer + `run.failed` / `gate.failed` wakes),
`certification-planner` (plan-then-execute), `reviewer` (read-only),
`failure-triage` (advise-only root-cause triage of one failed run, woken by
`run.failed`; added 2026-09-23).

**API:** `/agents` and `/workflows` with versions, `/validate` dry run,
`/publish`, `/retire`, `/catalog`, `/pause`, `/health` (reports schema
version). Mutations need an `admin`-class role (`AGENT_HUB_ADMIN_ROLES`) or a
role in the definition's own `rbac.edit`; reads need any platform bearer.

### Scoped toolkit (phase 2)

```python
toolkit = build_scoped_toolkit(
    definition=agent_version,   # tool_allowlist, mode, write_scope
    principal=obo_token,        # act: agent_id, agent_version, run_id; sub: user
    journal=run.journal,        # every write recorded before it executes
    budget=heartbeat.budget,    # decremented per tool call
)
```

Enforcement order inside the toolkit: catalog filter, mode check, write-scope
check, material endpoint excluded unconditionally, result size cap (default
64 KiB), untrusted-data envelope on every result that originates from captures,
traces, simulator output or ISO messages. The runner never executes tool calls
that appear inside tool results.

### Heartbeats, plans, approvals (phases 2 and 3)

Tables `agent_hub_heartbeats` (wake source, principal, steps, tokens, cost,
journal ref, result, status), `agent_hub_plans` (durable plan artifacts with
provenance: `authored_by`, agent version hash, journal id), `agent_hub_runs`
(workflow state machine: node states, cursor), `agent_hub_approvals`,
`agent_hub_budgets`, `agent_hub_incidents`. Wakeups on an already-running agent
coalesce. A restart watchdog marks heartbeats still `running` past their wall
clock (plus a grace period) as `failed` with `orphaned by restart` and alerts
on them; it never re-queues (a wake belongs to its trigger, and a re-run would
be an unjournalled side effect), so an orphan can no longer block the agent's
next wake. Implemented 2026-09-23 as `RegistryStore.reconcile_running`, run
in the lifespan before the first request.

**Engine (phase 3, implemented 2026-09-23).** `agent_hub_runs` holds
`node_states` (per node: `pending | deferred | running | waiting | done |
skipped | failed | cancelled`, plus heartbeat/approval/plan ids, journal for
tool nodes) and `results` (per node, the `${node.field}` context). Every
engine step is written before the next starts, so a killed process resumes
from the row. A node is ready when all incoming edges are resolved and at
least one fired; unfired nodes are skipped. `condition` branches on
`when: true|false`, `approval` on `approved|rejected` (no `rejected` edge ⇒
the run ends `rejected`). `agent_task` narrows the agent's mode, never
escalates (validator and engine both enforce); a plan-mode task that proposed
writes leaves an `agent_hub_plans` row the approval carries as context. A
write- or execute-tier `tool` node outside `mock` needs an approval ancestor,
the same D3 rule as a `full` task. Concurrency is a per-run asyncio lock in
the single agent-hub process (D5: one container); a cross-replica lock is a
phase-5 item if replicas ever arrive, and it would be a Postgres advisory
lock, not Redis (D6: the platform Postgres is the only durable tier here).
Approval `timeout_s` expires on a 30 s tick (`AGENT_HUB_ENGINE_TICK_S`) and
fails the run.

Approval policy defaults: `plan` / `advisor` need nothing; `full` on `mock`
is journal only; `full` elsewhere, load above `AGENT_LOAD_APPROVAL_TPS`
(default 100) and any certify or sign-off always pass an `approval` node;
secrets material is never reachable. Global pause: `/pause`, checked before
every LLM call and tool call.

### Testing

`FakeLLMBackend` replays scripted tool calls so the runner, engine and
approvals run without a provider. A CI job `agent-golden` runs the reference
workflows (`observer`, `certification-plan`) against the mock environment under
FakeLLM. The phase-5 injection pack (poisoned DE fields, hostile simulator
responses) passes only if no tool call leaves the allowlist and no directive
found in evidence is followed.

### Deployment

Compose service `agent-hub` (image `payprobe-agent-hub`, port 8600),
`depends_on` postgres (healthy) and auth-service; `AGENT_HUB_DATABASE_URL`
defaults to the platform DSN. All knobs default in compose; no new empty lines
in `deploy/.env`. The dashboard "System health" panel and Settings → Endpoints
need the new service (portal, owed).

## Consequences

Easier: adding an agent is a registry entry; every agent action is
reconstructable from heartbeat records and the journal; the blast radius of an
agent failure is one container and one budget; the :8400 code becomes library
code with one owner.

Harder: two runners coexist between phases 1 and 3; autonomous `full` mode on
real environments needs a human in the loop by design; one more image and one
more Postgres tenant.

Revisit when: a workflow needs a different model class for cost reasons (D4);
operators author more than a handful of workflows by hand (D1); MCP exposure of
"run workflow" is decided under the pending ADR-0009 phase-5 exposure item.

## Rollout with gates

Each phase ends in a commit and a review gate.

- **Gate 0: this ADR accepted.** Passed 2026-09-22 (D1 to D4), D6 amended 2026-09-23.
- **Phase 1: Registry.** Done in code; exit criteria: create → version → publish over HTTP; invalid tool refused at publish; RBAC enforced; `/status` shows `agent-hub`; portal Agents page + host `npm run build`; host `make test`; committed.
- **Phase 2: Heartbeat runner.** Done: scoped toolkit, OBO tokens minted in agent-hub with the shared secret (no new auth-service endpoint needed), FakeLLM, provenance (`spec_sha256`, model, `act` claim) on every heartbeat, heartbeat records with cancel/revert, pause and budget stops. Portal heartbeat view, D11 alert webhook and restart watchdog all done 2026-09-23; phase 2 complete.
- **Phase 3: Workflow engine.** State machine, node types, approvals inbox, reference workflows `observer` and `certification-plan`; fold :8400 in (D2). Exit: both workflows complete with a human approval mid-flow; container kill mid-workflow resumes; reviewer blocks a deliberately bad plan. Status 2026-09-23: engine, routes, seeds and all three exit criteria covered by tests under FakeLLM (`test_hub_engine.py`); the approvals inbox and runs view are in the portal (host build green, click-through owed), the :8400 assistant is mounted inside agent-hub with nginx and the orchestrator probe repointed (the standalone container is the deprecated alias until phase 5), and the exit criteria still want one run against a real provider.
- **Phase 4: Triggers and exposure.** Run-lifecycle events, schedules via the existing scheduler, webhook trigger, MCP catalog entries, insight-service as a tool. Exit: a failing scheduled regression wakes `observer` unattended and a finding with evidence reaches a human. Status 2026-09-23: events (`POST /events`, emitted by the orchestrator for run and gate outcomes) and schedules (agent-hub's own tick, not the orchestrator scheduler: the trigger vocabulary is shared, the timer is local so agent-hub stays self-contained) are built and tested; webhook trigger, MCP entries, insight-service as a tool and the real-provider proof are owed.
- **Phase 5: Hardening and handover.** Injection pack, `agent-golden` CI, egress allowlist, quotas, ATLAS + CLAUDE.md invariants (D12), operator skill `payprobe-agents`, status flip to Accepted, :8400 alias removed. Exit: security review + Go/No-Go by David.

## Action items

1. [x] Phase 1 registry service, tests, compose, CI, Makefile, `/status` probe.
2. [x] Host test run (2026-09-23, per package, Python 3.13): all seven suites green; the combined `make test` session misreports (see CLAUDE.md) and is not what CI runs.
3. [x] Portal Agents page, Settings → Endpoints entry, nginx `/api/agents/` in all three confs (the dev conf was missed by phase 1 and fixed 2026-09-23), host production build with fonts. Owed: browser click-through of the Agents page and heartbeat view.
4. [x] `.github/workflows/ci.yml` carries the "Test agent-hub" step. Not yet exercised: the `adr-0010` branch has never been pushed, so CI has not run it.
5. [ ] ATLAS §11: add agent-hub with a pointer to this ADR.
6. [ ] Decide `AGENT_LOAD_APPROVAL_TPS` and the daily budget defaults for compose.
7. [ ] Push the branch and get one green CI run before phase 3 lands on top.
8. [ ] Phase 2 loose ends: a real provider call through `ProviderLLMBackend`; the `nats-demo-net` driver still points at a deleted scenario (`scn-0039c642`); `delete_scenario` has no "referenced by a network" guard (pre-existing, outside this ADR).
