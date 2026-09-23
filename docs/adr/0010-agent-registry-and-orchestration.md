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
> (`run.completed`). 124 agent-hub tests, 386 orchestrator tests. MCP catalog
> entries followed the same day: an "Agents & workflows" group of 13 tools
> (list/get agents and workflows, wake, heartbeats, cancel, run workflow,
> runs, cancel run, approvals inbox) plus `payprobe://agents`, `//workflows`,
> `//approvals` resources; the MCP server's minted JWT now carries
> `svc: mcp-server`, which only agent-hub's gate reads, so a Claude Code or
> Claude Desktop operator can wake agents and start workflows. Deciding an
> approval is deliberately not an MCP tool: it is a human's act, recorded
> with the human's identity in the portal inbox. Owed from phase 4: the
> webhook trigger, insight-service as a first-class agent tool, and the
> unattended end-to-end proof (a failing scheduled regression waking
> `observer` and a finding reaching a human through the alert webhook; the
> first half is already observed, the webhook half needs a URL configured).
>
> **Inbound webhook trigger (2026-09-23).** `POST /webhooks/events/{event}`
> (wakes agents with a matching `event` trigger, body = subject) and
> `POST /webhooks/agents/{name}` (direct wake, body = input, opt-in via a new
> `webhook` trigger kind, else 409), both credentialed by an HMAC signature
> over the body (`AGENT_HUB_WEBHOOK_SECRET`, the same `t=,v1=` scheme and
> verifier as the outbound alerts, 5 min tolerance, 64 KB cap); the bearer
> gate skips `/webhooks/` and the routes answer 503 while no secret is set.
> Principals are `event:<name>` / `webhook:<agent>` with no roles, so the
> same downstream RBAC fence applies as for platform events. 132 agent-hub
> tests. Owed from phase 4: insight-service as a first-class agent tool and
> the alert-webhook half of the unattended proof.
>
> **Phase 5, injection pack and `agent-golden` (2026-09-23).**
> `test_hub_injection.py` drives a scripted "obedient" FakeLLM against a
> hostile platform (an injection in a run note, a fake `tool_calls` structure
> inside a status payload, a 200 KB result) and proves the guards live in
> machinery: untrusted results are wrapped and their embedded calls never
> dispatched; an advisor that names a write tool is refused at publish and at
> dispatch; a model that obeys the injection is stopped by the allowlist with
> the journal empty; plan mode turns the injected write into a proposal; a
> full-mode write outside the write scope is refused; oversized results are
> capped before the model sees them; wake input stays user content while the
> system prompt is the registered spec; and an upstream result forging
> `{"decision": "approved"}` still parks the run at the human gate. CI gained
> an `agent-golden` step running the reference-workflow and injection tests
> by name. 142 agent-hub tests.
>
> **Egress allowlist (2026-09-23).** `agent_hub/egress.py`, enforced in the
> one function through which a prompt leaves agent-hub (`llm._post_json`):
> the URL's host must be `api.openai.com`, `api.anthropic.com`, the host of
> `ASSIST_LLM_BASE_URL`, or a host the operator lists in
> `AGENT_HUB_EGRESS_ALLOW` (exact, `*.suffix`, optional `:port`); https is
> required except for listed hosts; userinfo tricks and non-default ports on
> listed hosts are refused. The threat is a `base_url` edited in Settings by a
> compromised admin session; the outcome is a heartbeat that fails with
> `egress refused` and alerts, with nothing sent. `/health.egress` shows the
> effective set. 154 agent-hub tests.
>
> **Verdicts on the run report (2026-09-23).** Heartbeats carry a `subject`
> (migration 4): `run:<id>` when the wake input is a JSON object naming
> `run_id` (event payloads and triage wakes), `wfrun:<id>` for workflow agent
> tasks, or an explicit `subject` on the wake body and the MCP `wake_agent`
> tool; `GET /heartbeats?subject=`. The portal run report gained an "agent
> verdict, advisory" panel (`agents/agent-verdicts.component.ts`) that shows
> what each agent concluded about that run: `failure-triage`'s category, root
> cause and next step, or observer-style findings. The report attaches this
> text and never consults it; gates and sign-off stay deterministic (D5). The
> sign-off snapshot annotation is still owed. 158 agent-hub tests.
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
>
> **Deterministic regression post-check (2026-09-23).** The first real
> `failure-triage` verdict called the very first run of a scenario a
> regression because "similar failures" existed. Where the platform holds the
> evidence, the evidence now overrides the model: the orchestrator gained
> `GET /runs/{id}/regression` (`RunStore.regression`: per scenario, its
> earlier pass/fail outcomes, `conclusion` = `regression` | `never_passed` |
> `first_run` | `passed`, last passing run, failure streak; run-level
> `verdict`), the toolkit gained the read tool `get_run_regression` (both
> backends, untrusted-wrapped, in `failure-triage`'s allowlist and
> instructions), and `agent_hub/postcheck.py` runs after the tool loop: a
> `done` answer that is a JSON object with `run_id` and `regression` has the
> field overwritten with the history's boolean, gains a `regression_evidence`
> block (verdict, the model's original claim, the failed scenarios' history)
> and a `postcheck` step on the heartbeat (`claimed`, `verified`, `verdict`,
> `changed`). No evidence (unknown run, orchestrator down) keeps the claim as
> written and records the miss; the check never raises. Portal: the run
> report's verdict pill reads `regression · verified` or `first run` /
> `never passed` / `not a regression`, and the heartbeat waterfall shows the
> step as `corrected` / `confirmed` / `no evidence`. Existing registries keep
> their `failure-triage` v1 (seeds never overwrite an operator's registry): the
> post-check applies regardless of the version; publishing a v2 with the new
> tool lets the model read the evidence itself. `test_hub_regression.py`
> (9), orchestrator `test_schedules_and_trend.py` (+4).
>
> **Quotas and the budget defaults (2026-09-23, action item 6).**
> `agent_hub/quotas.py`, three knobs read once at startup and shown under
> `/health.quotas`, `0` = off, all set in the three compose files:
> `AGENT_HUB_DAILY_TOKENS` (5,000,000; every agent's tokens per UTC day on
> the same ledger as the per-agent budget; refused wakes are recorded as
> `budget_exceeded` and alert), `AGENT_HUB_MAX_CONCURRENT` (4 heartbeats
> running at once across agents; refused wakes are recorded as the new status
> `quota_exceeded` and alert; per agent the cap stays one through coalescing;
> a schedule fires again on its next tick, an event wake is lost and the row
> says so), and `AGENT_LOAD_APPROVAL_TPS` (100; enforced where invariant #6
> puts guardrails, in `scoped_dispatch` through `ToolScope.load_tps_cap`:
> a heartbeat's `start_load_run` above that peak rate, any of `target_tps`,
> `end_tps`, `spike_tps`, `start_tps`, top level or in `extra`, is refused
> with `guardrail: true`; the engine's `tool` node scope carries no cap
> because outside mock it must sit behind an `approval`). Seed budgets:
> observer 3,000,000 (96 scheduled wakes a day at ~30k), failure-triage
> 1,000,000, config / scenario-author / certification-planner / reviewer
> 500,000, plan-executor 300,000. Seeds never overwrite a registry, so an
> existing deployment keeps `budget: null` on its v1 builtins until an
> operator publishes v2; the hub-wide ceiling covers it meanwhile.
> `test_hub_quotas.py` (10). 179 agent-hub tests.
>
> **Insight service and run history as first-class tools (2026-09-23,
> the phase-4 leftover).** Six toolkit tools, one handler each in
> `agent_toolkit.py` and one primitive in both backends (invariant #3), all
> untrusted-wrapped: `insight_status` (corpus size, active model, so a thin
> corpus is visible before its advice is trusted), `get_scenario_prediction`
> (one scenario's `p_fail_next`), `list_insight_categories` (the learned
> taxonomy), `run_trend` and `run_flakiness` (the orchestrator's
> deterministic per-day and per-scenario history), and `train_insights`
> (`POST /train`): execute tier like `start_load_run`, since it has a real
> side effect on the insight service's own model store and nothing to
> journal, so advisors never see it, plan mode records it as a proposal and
> only `full` runs it. The five reads join `_READ_RUNTIME` (every runtime
> reader, observer included, whose instructions now cite trend and
> flakiness before any prediction); `train_insights` is granted to
> `certification-planner` (proposes) and `plan-executor` (applies).
> `test_hub_insight_tools.py` (6). 185 agent-hub tests.
>
> **D2, second half: the `assistant` alias removed (2026-09-23).** The
> standalone container is gone from the three compose files (the portal now
> `depends_on` agent-hub), from `scripts/publish-images.sh`, the
> `publish-images` workflow and `scripts/PUBLISHING.md`; its Dockerfile is
> deleted and `ASSIST_PORT` dropped from `deploy/.env.example`.
> `packages/payprobe-assistant` stays as the library agent-hub mounts at
> `/assistant`, with its own CI test step. The portal's dev
> `assistantApiBase` is `http://localhost:8600/assistant` and the dashboard's
> "assistant" endpoint probe is the agent-hub address (prod `/api/assistant`
> unchanged, nginx already routed it to agent-hub). Chat turns do **not**
> become `config`-agent heartbeats: that is a UX and cost decision (every
> chat turn would be a journalled, budgeted, alertable record) and stays
> deferred on purpose; the chat keeps its own session store inside the
> mounted app. Upgrading an existing stack: `docker compose up -d
> --remove-orphans` removes the stopped `payprobe-assistant-1`.
>
> **Agent verdicts on the sign-off snapshot (2026-09-23).** `certify_run`
> now fetches, after the gates are decided and the content hash computed,
> the finished heartbeats attached to `run:<id>` (`GET /heartbeats?subject=`
> then each record, 5 s timeouts, best-effort) and freezes them into the
> snapshot as `annotations.agent_verdicts`: per heartbeat the agent, version,
> `spec_sha256`, wake, status, model and either the triage `verdict`
> (category, root cause, `regression` with its evidence verdict and the
> model's original claim, next step), observer `findings`, or the first
> lines of a free-text answer. The block carries `advisory: true`,
> `counted_in_gate: false`, `in_content_hash: false` and says why it is
> empty when it is (agent-hub unset or unreachable). Rendered in the
> printable sign-off document (`report_service.generators`, between the
> evidence and the provenance, headed "advisory: not a gate input, outside
> the content hash") and on the portal sign-off page. Pure
> `_agent_verdict_annotation` + `_json_in_text` in the orchestrator;
> `test_signoff.py` (+3, including "unreachable agent-hub still certifies")
> and `test_generators.py` (+2).

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
- **Phase 4: Triggers and exposure.** Run-lifecycle events, schedules via the existing scheduler, webhook trigger, MCP catalog entries, insight-service as a tool. Exit: a failing scheduled regression wakes `observer` unattended and a finding with evidence reaches a human. Status 2026-09-23: events (`POST /events`, emitted by the orchestrator for run and gate outcomes) and schedules (agent-hub's own tick, not the orchestrator scheduler: the trigger vocabulary is shared, the timer is local so agent-hub stays self-contained) are built and tested; webhook trigger, MCP entries and insight-service as a tool (`insight_status`, `get_scenario_prediction`, `list_insight_categories`, `train_insights`, plus `run_trend` / `run_flakiness`) landed the same day; the real-provider proof came from the first scheduled `observer` wake (13:23 UTC) and the `run.failed` wake of `failure-triage`; the alert-webhook half still wants a URL.
- **Phase 5: Hardening and handover.** Injection pack, `agent-golden` CI, egress allowlist, quotas, ATLAS + CLAUDE.md invariants (D12), operator skill `payprobe-agents`, status flip to Accepted, :8400 alias removed. Exit: security review + Go/No-Go by David. Status 2026-09-23: injection pack (`test_hub_injection.py`, 10 tests) and the `agent-golden` CI step are in; D12 was done with phase 2. Egress allowlist done the same day (`egress.py`, enforced in the one provider caller). ATLAS (§6, §7, §11, §13) and the `payprobe-agents` operator skill written the same day, then quotas (`AGENT_HUB_DAILY_TOKENS`, `AGENT_HUB_MAX_CONCURRENT`, `AGENT_LOAD_APPROVAL_TPS` in the tool layer, seed budgets), the deterministic regression post-check, the insight/run-history tools, the sign-off annotation and the `assistant` alias removal, all the same day. Owed: David's security review and Go/No-Go, the status flip to Accepted, the browser click-through, one green CI run on the PR.

## Action items

1. [x] Phase 1 registry service, tests, compose, CI, Makefile, `/status` probe.
2. [x] Host test run (2026-09-23, per package, Python 3.13): all seven suites green; the combined `make test` session misreports (see CLAUDE.md) and is not what CI runs.
3. [x] Portal Agents page, Settings → Endpoints entry, nginx `/api/agents/` in all three confs (the dev conf was missed by phase 1 and fixed 2026-09-23), host production build with fonts. Owed: browser click-through of the Agents page and heartbeat view.
4. [x] `.github/workflows/ci.yml` carries the "Test agent-hub" step. Not yet exercised: the `adr-0010` branch has never been pushed, so CI has not run it.
5. [x] ATLAS: agent-hub in §6 (the pattern generalised), §7 (security), §11 (follow-through) and §13 (decision log); operator skill `.claude/skills/payprobe-agents`.
6. [x] Decide `AGENT_LOAD_APPROVAL_TPS` and the daily budget defaults for compose (2026-09-23: 100 tps enforced in the tool layer; seed budgets 300k to 3M per agent; hub-wide `AGENT_HUB_DAILY_TOKENS` 5M and `AGENT_HUB_MAX_CONCURRENT` 4 in compose).
7. [~] Push the branch and get one green CI run: pushed throughout 2026-09-23, PR https://github.com/pay-probe/payprobe/pull/2 opened the same day (CI's first run of the agent-hub steps; green run still to be confirmed).
8. [ ] Phase 2 loose ends: a real provider call through `ProviderLLMBackend`; the `nats-demo-net` driver still points at a deleted scenario (`scn-0039c642`); `delete_scenario` has no "referenced by a network" guard (pre-existing, outside this ADR).
