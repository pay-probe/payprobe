# Handoff: ADR-0010 agent-hub (phases 1 and 2 done, phase 3 next)

**Written:** 2026-09-23, from the claude.ai session that designed ADR-0010 and
built phases 1 and 2 in a sandbox clone of `pay-probe/payprobe` at `f6d8311`.
**For:** the Claude Code session that continues the work in David's checkout.
**Read first:** this file, then `docs/adr/0010-agent-registry-and-orchestration.md`,
then `CLAUDE.md` invariants 9 and 10. The three memos the ADR reconciles are
`docs/agentic-engine-evaluation.md`, `docs/agentic-engine-evaluation-fable.md`,
`docs/agentic-os-paperclip-analysis.md`.

## 1. How the work arrives

Four commits on top of `f6d8311`, delivered as git patches:

| Patch | Commit | Contents |
|---|---|---|
| `0001-adr-0010-phase-1-agent-hub-registry.patch` | `b0c2103` | `packages/agent-hub` registry on PostgreSQL, compose/CI/Makefile wiring, orchestrator `/status` probe, ADR |
| `0002-adr-0010-phase-1-portal-agents-page.patch` | `cfbd6c0` | portal Agents page, `agent-hub` endpoint in RuntimeConfig, nginx `/api/agents/`, ADR status |
| `0003-adr-0010-phase-2-heartbeat-runner.patch` | `e0f9799` | scoped toolkit, shared REST backend + provider caller, OBO principals, heartbeats API, invariants 9 and 10 |
| `0004-adr-0010-handoff.patch` | (this file) | this handoff + CLAUDE.md pointer |

Apply in order with `git am <patch>`; `adr-0010-all.patch` is the same four
commits as one mbox for a clean checkout. The CI workflow hunk in 0001 lands
in `.github/workflows/ci.yml`, which is protected from remote writes on this
setup: check it applied, else copy the "Test agent-hub" step in by hand.

## 2. First things to run on the host (nothing below was possible in the sandbox)

```sh
rm -f .git/index.lock                                  # FUSE gotcha, see CLAUDE.md
git am 0001-*.patch 0002-*.patch 0003-*.patch 0004-*.patch
docker compose -f infra/docker/docker-compose.yml up -d postgres
make test                                              # all packages, one session
make test-orchestrator                                 # /status "agent-hub" assertion (never run: sandbox cannot import iso8583)
cd packages/portal && npm run build                    # with fonts; the sandbox built with font inlining off
docker compose -f infra/docker/docker-compose.yml up -d --build agent-hub
curl -s localhost:8600/health                          # {"status":"ok","paused":false,"schema_version":2}
```

Then click through Configure → Agents against the running service: list,
open `observer`, New draft, edit JSON, Validate (break a tool name to see the
problems list), Publish, Pause agents. Verify the dashboard System health
panel shows "Agent Hub" and Settings → Endpoints lists it.

## 3. Decisions that are settled (do not reopen)

Confirmed by David, recorded as D1 to D12 in the ADR:

- D1 workflows are JSON DAGs in v1 (no canvas).
- D2 the :8400 assistant folds into agent-hub in phase 3; :8400 becomes a
  compose alias for one release, then goes.
- D3 default mode is `plan`; `full` outside `mock` needs an `approval` node
  before it; a workflow node may narrow an agent's mode, never escalate it.
- D4 one LLM provider, configured once (Settings → AI assistant; `ASSIST_LLM_*`
  env wins). Per-agent `model`/`temperature` only.
- D5 separate container; the orchestrator stays LLM-free and is the
  deterministic floor.
- **D6 PostgreSQL only. A SQLite implementation was built first and rejected
  by David as a mistake; it is recorded as a rejected option. Never add a file
  or in-memory fallback to agent-hub.**
- D7 heartbeats over durable records; limits are per wake; daily budget is
  schedulability (hard stop + recorded refusal).
- D8 execution policy as a persisted state machine; executor and reviewer must
  differ.
- D9 tool access enforced in the tool layer only (`scoped_dispatch`).
- D10 per-heartbeat on-behalf-of JWT (`act` claim, never `svc`).
- D11 provenance stamp and alert webhook are Stage 0 (both done; the webhook
  landed 2026-09-23 as `agent_hub/alerts.py`).
- D12 invariants 9 and 10 in CLAUDE.md (done with phase 2).

ATLAS roadmap #5 (scenario-service `/agent` routes stay a deprecated shim) is
untouched by all of this.

## 4. What exists now

### Backend `packages/agent-hub/agent_hub/`

| File | Purpose |
|---|---|
| `models.py` | `AgentSpec`, `WorkflowSpec`, `Node`, `Edge`, `Trigger`, `Limits`, `Budget`, `WriteScope`, `Rbac`; `MODE_RANK`; `parse_ref` |
| `validate.py` | publish-time rules: unknown tool, advisor read-only, full needs write scope, model allowlist, DAG acyclic, refs resolvable, no mode escalation, D3 approval ancestor, executor ≠ reviewer; `tool_catalog()` |
| `store.py` | asyncpg `RegistryStore`; `MIGRATIONS` (1: definitions/versions/meta, 2: heartbeats); lifecycle draft → active → superseded / retired; `resolve("name")`, `resolve("name@N")`; pause flag; heartbeat CRUD; `reset()` for tests |
| `seed.py` | builtins `config`, `scenario-author`, `observer`, `certification-planner`, `reviewer`, `failure-triage` (added 2026-09-23); published on first start, missing ones added on later starts; cannot be retired |
| `auth.py` | the platform bearer gate (copy of the assistant's) + `require_roles`, `caller_sub` |
| `main.py` | FastAPI app; `/agents` and `/workflows` CRUD with versions, `/validate`, `/publish`, `/retire`; `/catalog`; `/pause`; `/health`; phase 2: `POST /agents/{name}/wake`, `GET /heartbeats`, `GET /heartbeats/{id}`, `POST .../cancel`, `POST .../revert`; test seams `app.state.llm_factory`, `app.state.backend_factory` |
| `runner.py` | `run_heartbeat(spec, input, backend, llm, flags)` → `Outcome`; sync, pure w.r.t. the store; `MODE_PREAMBLE`; plan-mode writes become `proposed` |
| `llm.py` | `FakeLLMBackend` (scripted), `ProviderLLMBackend` (shared `payprobe_common.llm_provider`), `resolve_llm()` with the assistant's precedence |
| `rest.py` | two credentials: `request` (service, platform reads only) and `request_as(token)` (per-heartbeat OBO) |
| `principal.py` | `mint_obo(user, agent, version, heartbeat_id, ttl)` |
| `alerts.py` | D11 alert webhook: `Alerter` (fire-and-forget, signed, retried), `events_for(hb, mode)`, `findings_of`, `sign`/`verify`; `app.state.alerts`, stats on `/health` |
| `exprs.py` | phase 3: `render()` (`${...}` templating over inputs + node results) and `evaluate()` (condition nodes, whitelisted AST, no `eval`) |
| `engine.py` | phase 3: `Engine` persisted state machine over `agent_hub_runs`; `start`, `advance`, `decide`, `cancel`, `reconcile`, `tick`; node executors for all six types; `app.state.engine` |

Tests: `packages/agent-hub/tests/test_hub_*.py` + `hub_testkit.py` (117 tests
after 2026-09-23: the original 69, plus `test_hub_alerts.py`, the watchdog
test in `test_hub_store.py`, the `failure-triage` seed assertion in
`test_hub_api.py`, `test_hub_exprs.py` and `test_hub_engine.py`).
Module names are prefixed `test_hub_` on purpose: every package's suite runs in
ONE pytest session and `test_api.py` already exists in insight-service.
`from conftest import ...` is forbidden for the same reason; helpers live in
`hub_testkit.py`.

### Shared `packages/payprobe_common/`

- `agent_toolkit.py`: appended `ToolScope`, `scoped_dispatch`,
  `check_write_scope`, `tiers_for_mode`, `UNTRUSTED_RESULT_TOOLS`,
  `DEFAULT_RESULT_CAP`. Nothing above that line changed.
- `rest_backend.py` (new): the REST backend, transport and base URLs injected.
  The assistant's `assistant_service.tools.RestBackend` is now a subclass that
  binds `rest.request` through a lambda so its tests can still monkeypatch it.
- `llm_provider.py` (new): `provider_tool_call` and the OpenAI/Anthropic
  helpers, `post_json` injected; `assistant_service.loop` binds the names.

### Portal `packages/portal/src/app/`

- `agents/agents.component.ts` (Configure → Agents), `shared/agent-hub-api.service.ts`,
  `agent-hub` in `shared/runtime-config.service.ts`, route + nav entry,
  `agentHubApiBase` in both `environments/*.ts`, nginx `/api/agents/` in
  `deploy/nginx/nginx.conf` and `infra/nginx/nginx.conf`.
- No heartbeat view yet (see §7).

### Deployment

Compose service `agent-hub` (:8600) in `deploy/docker-compose.yml`,
`infra/docker/docker-compose.yml`, `infra/docker/docker-compose.hub.yml`;
image in `scripts/publish-images.sh`; `make test-agent-hub`; CI step in
`test-services` with `AGENT_HUB_TEST_DATABASE_URL`.

Env: `AGENT_HUB_DATABASE_URL` (required), `AGENT_HUB_POOL_MAX`,
`AGENT_HUB_SEED`, `AGENT_HUB_ADMIN_ROLES` (default `admin`),
`AGENT_HUB_MODEL_ALLOWLIST`, `AGENT_HUB_ALERT_WEBHOOK_URL`,
`AGENT_HUB_ALERT_WEBHOOK_SECRET`, `AGENT_HUB_ALERT_TIMEOUT_S`,
`SCENARIO_API_URL`, `RUN_API_URL`,
`INSIGHT_API_URL`, `ASSIST_LLM_PROVIDER|API_KEY|MODEL|BASE_URL`, plus the
platform auth vars (`PAYPROBE_ENV`, `API_TOKEN`, `AUTH_JWT_SECRET`).

## 5. Contracts worth keeping in your head

- Published versions are immutable (409 on edit). One active version per
  definition, enforced by a partial unique index. `name` resolves to active;
  `name@N` resolves while N is active or superseded; retired resolves to nothing.
- Heartbeat statuses: `running`, `done`, `failed`, `budget_exceeded`,
  `timed_out`, `cancelled`, `paused`. `paused` and `budget_exceeded` can be
  recorded without ever running (refused wakes). One running heartbeat per
  agent (partial unique index); a second wake returns 200 `coalesced: true`;
  a fresh start is 202.
- `scoped_dispatch` order: allowlist → tier by mode → write scope (for
  write/execute) → `dispatch` → result cap → untrusted envelope for
  `UNTRUSTED_RESULT_TOOLS`. Plan-mode write refusals carry the phrase
  "describe the change in your plan"; the runner keys `proposed` on it.
- Write scope: a write naming a project/environment must name one in scope
  (`*` allows any); a write naming neither (connections, tables, starter
  flows, variables) needs `*` in `projects`.
- OBO token: `sub`/`roles`/`project_ids` of the invoking user, `act =
  {agent, version, heartbeat}`, TTL `wall_clock_s + 60`, no `svc`. Dev with no
  secret returns the literal `dev` marker.
- Revert uses the caller's own bearer (reverting is the human's act) and needs
  an admin-class role; it restores newest-first from the persisted journal and
  clears it.

## 6. Verification status (honest ledger)

Verified in the sandbox: 69 agent-hub tests against PostgreSQL 16; 529 tests
across agent-hub, payprobe-assistant, insight-service and scenario-service in
one pytest session; ruff + black (line length 100) on agent-hub and the two new
common modules; prettier on the portal files; portal
`ng build --configuration production` exit 0 with Node 22.22.3 and font
inlining disabled locally (Google Fonts unreachable; `angular.json` untouched);
all three compose files parse; orchestrator `main.py` compiles.

Verified on David's host, 2026-09-23 (Python 3.13, per-package runs, the
authoritative mode since CI also runs per package): orchestrator 383,
scenario-service 328 (+2 that only fail while the live stack occupies
localhost:8100/8500; they pass with those URLs pointed at a closed port),
mcp-server 91, payprobe-assistant 63, insight-service 67, agent-hub 69, worker
462 (+6 designed skips). Portal `ng build --configuration production` exit 0
with fonts after a clean `npm ci` (the old node_modules carried another
platform's esbuild). The portal image was rebuilt and the new chunk serves the
heartbeat view. Two incidental fixes fell out: `infra/nginx/nginx.dev.conf`
(the conf the infra compose files mount) lacked the `/api/agents/` route that
phase 1 added only to `infra/nginx/nginx.conf` and `deploy/nginx/nginx.conf`,
so the Agents page showed "agent-hub is not reachable"; and
`TcpResponder.stop()` awaited `Server.wait_closed()` before closing client
sockets, a deadlock on Python 3.12+ (the images are 3.12) that CI on 3.11
never saw.

Still not verified: a browser click-through of the Agents page and heartbeat
view, a real provider call (`ProviderLLMBackend` is written to the assistant's
pattern and exercised only through the shared code path), and the CI workflow
hunk.

## 7. Next tasks, in order

Phase 2 remainder:

1. **Portal heartbeat view.** Done 2026-09-23:
   `portal/src/app/agents/heartbeats.component.ts` (`app-agent-heartbeats`),
   embedded under the selected agent on the Agents page. List from
   `GET /heartbeats?agent=`, detail with a cumulative-ms step waterfall (the
   runner is sequential, so durations are the exact timeline), `proposed`
   calls rendered as a numbered plan, Wake (input box; coalesce and refusals
   surfaced as toasts), Cancel and Revert with confirms, own PagePoll (5 s) and
   PollHealth chip. Client methods on `AgentHubApiService`. Browser
   click-through still owed.
2. **Alert webhook (D11).** Done 2026-09-23: `agent_hub/alerts.py`
   (`Alerter`, `events_for`, `sign`/`verify`). `AGENT_HUB_ALERT_WEBHOOK_URL`
   (unset = off) + `AGENT_HUB_ALERT_WEBHOOK_SECRET` (`X-PayProbe-Signature:
   t=<ts>,v1=<HMAC-SHA256>`, the ADR-0009 Stripe scheme) +
   `AGENT_HUB_ALERT_TIMEOUT_S`. POST on `failed` / `budget_exceeded` /
   `timed_out` (including budget refusals that never ran and watchdog
   orphans) and on advisor findings at `warn`+ (`result` parsed as a list or
   `{"findings": [...]}`, fenced JSON tolerated). Fire-and-forget task, three
   attempts with 1 s / 4 s backoff on 5xx or transport error, 4xx final; stats
   under `/health.alerts`; transport injectable (`app.state.alerts`). Ten
   tests in `test_hub_alerts.py`. Knobs default empty in all three compose
   files.
3. **Restart watchdog.** Done 2026-09-23: `RegistryStore.reconcile_running`
   (called in the lifespan before serving) marks `running` rows older than
   their version's `limits.wall_clock_s` + 60 s grace as `failed` /
   `orphaned by restart` and alerts on each. The ADR sentence that said
   "re-queues" was corrected: a wake belongs to its trigger, so orphans fail
   instead of re-running.

Phase 3 (workflow engine). Engine core done 2026-09-23:

- `store.py` migration 3: `agent_hub_runs` (node_states + results jsonb, the
  whole state), `agent_hub_approvals` (run, node, roles, context, decision,
  expires_at), `agent_hub_plans` (plan artifacts with agent provenance).
- `exprs.py`: `render()` for `${inputs.x}` / `${node.field}` templating and
  `evaluate()` for `condition` nodes (whitelisted AST walk, no `eval`).
- `engine.py`: persisted state machine; executors for all six node types;
  `agent_task` goes through `main.launch_heartbeat` (the wake code path,
  extracted from the route) with `on_done` re-entering the engine; startup
  `reconcile()` re-attaches running tasks and retries deferred ones; a 30 s
  tick expires approvals. Lock is per run, in process: no Redis (D6; a
  cross-replica lock, if ever needed, is a Postgres advisory lock).
- `main.py`: `POST /workflows/{name}/run`, `GET /runs[/{id}]`,
  `POST /runs/{id}/cancel`, `GET /approvals[/{id}]` (inbox),
  `POST /approvals/{id}/decide`, `GET /plans[/{id}]`.
- `validate.py`: write/execute `tool` nodes outside mock need an approval
  ancestor; edge `when` labels must match the source node's branches.
- Seeds: `plan-executor` agent (full mode, admin-invoke only) and the
  workflows `observer` (observe → review → gate) and `certification-plan`
  (plan → review → verdict → gate → apply). `seed_builtin` now seeds
  workflows after agents; existing registries get the missing ones on start.
- Tests: `test_hub_exprs.py` (25) and `test_hub_engine.py` (11) cover the
  three exit criteria under FakeLLM. 117 agent-hub tests in total.

Still owed in phase 3: portal runs view + approvals inbox (API complete);
fold :8400 in (D2): move `assistant_service` session/chat routes into
agent-hub or retire them, keep `payprobe_common` as the single home; one run
of each reference workflow against a real provider.

Phase 4 and 5 remain as written in the ADR.

## 8. Gotchas learned this session

- Postgres in the sandbox stopped between turns; a run that shows "49 skipped"
  is the suite's designed no-database behaviour, not a pass. Always confirm
  the database is up before reading green.
- Black/ruff with line length 100 (mirror `packages/worker/pyproject.toml`);
  `ruff --ignore RUF100` because the copied `auth.py` carries a `noqa` the
  worker config needs.
- The Angular CLI refuses Node below 22.22.3 by exact patch; `.nvmrc` is right.
- `packages/agent-hub/Dockerfile` builds with context `packages/` so the image
  carries `payprobe_common`; compose already passes that context.
- `deploy/.env` gained no lines; every new knob defaults in compose.
- David's standing preferences apply here as everywhere in this repo: state
  verification honestly, keep config lean, route new portal polls through
  PollHealth, update CLAUDE.md and the ADR when a decision changes, no em
  dashes in prose written for him.

## 9. Suggested opening prompt for the Claude Code session

> Read docs/history/2026-09-23-agent-hub-handoff.md, then
> docs/adr/0010-agent-registry-and-orchestration.md. Confirm the four ADR-0010
> commits are on this branch and run `make test`. Then continue with §7 task 1
> (portal heartbeat view) and stop for review before task 2.
