# General Assistant — Build Spec

A conversational, tool-calling assistant that helps configure PayProbe end to
end: adapters/connections, message formats, data tables, variables, starter
flows, catalog targets, scenarios — and reads (but does not trigger) runs, load
tests and schedules.

This is distinct from the existing **scenario "✨ Ask AI"** (a one-shot
generator that turns a prompt into an insertable flow). The general assistant is
a multi-turn agent loop that can both read and *write* across the whole config
surface, autonomously, with a reversible safety net.

## Target architecture

A standalone **`payprobe-assistant`** service — a sibling of `payprobe-portal`
and `payprobe-auth-service` — acting as the single **LLM gateway**.

Why separate (not embedded in scenario-service):

- It's a cross-service orchestrator (touches scenario-service, orchestrator,
  auth); it owns the *agent*, not the *data*.
- Long-lived, streaming, LLM-latency-bound runtime — different scaling profile
  from fast config CRUD.
- One auditable outbound-egress boundary and one place that holds provider keys.

What it owns: the agent loop, the tool registry + schemas, the system prompt,
the provider tool-calling adapter, the guardrails, and a per-session **change
journal**. Session + journal live in **Redis** (same pattern as cross-replica
run-control). No business datastore of its own.

What it delegates: every read/write is a call into scenario-service /
orchestrator, authenticated with a **service token** (existing service-to-service
auth). Tools are datastore-free so extraction is cheap.

Unification: once it is the only thing that talks to LLM providers, the scenario
"Ask AI" and the `mcp-server` become thin frontends over the same core (HTTP
chat / stdio MCP) instead of two drifting copies of provider logic.

## De-risking path

Prototype the loop + tools + safety **inside scenario-service** to validate
behavior, then lift-and-shift into `payprobe-assistant`. Because tools are
store/REST calls (not in-process datastore access), extraction is mostly moving
files + adding a service token.

## Tool tiers

- **read** (always allowed): `list_connections`, `get_connection`,
  `list_catalog`, `list_formats`, `get_format`, `list_tables`,
  `list_starter_flows`, `get_global_variables`, … Answers "which connections
  point to localhost?", "is this scenario valid?".
- **write** (journalled, guard-railed): `upsert_connection`,
  `delete_connection`, `save_table`, `delete_table`, `set_global_variables`,
  `create_starter_flow`, `delete_starter_flow`, `upsert_catalog_target`,
  `create_format` / `update_format`, scenario create/update (async, next slice).
- **out of scope for the agent**: triggering runs, load tests, schedules. Status
  is *readable*; *execution* stays a human action.

## Safety model

Autonomous writes mean the safety net replaces the confirmation dialog:

1. **Reversible by default.** Every write records an undo closure in the session
   `ChangeJournal`; the UI shows "Revert N changes".
2. **Hard guardrails in the tool layer, not the prompt.** Builtins can't be
   destroyed; a connection still referenced by a scenario can't be deleted; all
   writes pass the existing auth/RBAC gate (agent acts with the caller's perms).
3. **Read-before-write** discipline in the system prompt so it edits rather than
   blindly recreates.

## Phases

- **Phase 0 — Shared tool layer.** Declarative registry (name, tier, JSON
  schema, handler) over the config stores. ✅ `scenario-service/api/agent_tools.py`
- **Phase 1 — Agent loop.** `POST /agent/chat`; multi-turn tool-calling loop on
  the saved provider config. Extend `_llm_call` for native tool calling
  (OpenAI + Anthropic). LLM-required; degrade with a clear "set up a provider"
  message.
- **Phase 2 — Safety layer.** Change journal + guardrails. ✅ (journal +
  builtin/referenced guards landed with Phase 0; RBAC wiring with the endpoint)
- **Phase 3 — Read tools live.** Ship a read-only assistant first to exercise
  the loop with zero write risk.
- **Phase 4 — Write tools, config only.** Turn on the write tier behind the
  journal. Run/load/schedule stay read-only.
- **Phase 5 — Portal UI.** Global slide-over panel (topbar, page-context aware);
  a dedicated `/assistant` route later shares the component. Tool calls render
  inline (collapsible) with a revert button.
- **Phase 6 — Tests + hardening.** Loop tests with a fake provider; guardrail
  tests; add to Makefile `PKGS`.

Suggested order to feel progress: 0 → 1 → 3 (ship read-only) → 2 → 4 (writes) →
5 → 6.

## Status

- ✅ Phase 0 tool registry + Phase 2 journal/guardrails:
  `scenario-service/api/agent_tools.py`
- ✅ Phase 1 agent loop + provider tool-calling adapter: `scenario-service/api/agent.py`
  (provider-neutral `run_agent`, OpenAI + Anthropic translators, system prompt,
  iteration cap, LLM-required guard).
- ✅ Phase 3 read tools + Phase 4 write tools live behind `POST /agent/chat`
  (`mode: full|advisor`); reuses the saved assist provider config.
- ✅ Tests: `test_agent_tools.py` (12) + `test_agent_loop.py` (10) — 22 passing,
  no network (scripted/monkeypatched provider). Auto-run via Makefile `PKGS`.
- ✅ Phase 5 portal slide-over UI: `portal/src/app/assistant/` (models, api
  service, state service, panel component + scss). Topbar "✨" button toggles it;
  mounted globally in the shell (`app.component.ts`). Full/Advisor toggle,
  inline collapsible tool calls, "Changes applied" journal list, page-context
  prepended to the first turn. Prettier-clean. (Full `ng build` not run here —
  sandbox can't resolve `@angular/common` for any file + Google-Fonts inlining
  is network-blocked; verify with `npm run build` locally.)
- ✅ Persistent change-journal sessions + one-click revert: journal refactored to
  serializable records (restore is a pure fn of `before`, see
  `agent_tools.restore_journal`); `api/agent_session.py` SessionStore (in-memory
  + Redis by `REDIS_URL`); `/agent/chat` mints/accepts `session_id` and persists
  the turn's records; `POST /agent/revert {session_id}` replays them. Portal:
  Undo button in the panel header (shows session change count) →
  `AssistantService.revert()`.
- ✅ Scenario write tools: `list_scenarios` / `get_scenario` /
  `validate_scenario` (read) and `create_scenario` / `update_scenario` (write,
  validated + journalled + revertible). The async store is reached from the sync
  handlers via `ToolContext.run_async` (the endpoint schedules coroutines on the
  event loop from the threadpool). Restore handles the scenario resource too.
- ✅ Tests: + `test_agent_session.py` (4) + `test_agent_scenarios.py` (3, real
  async store via TestClient: create→revert, invalid-rejected, update→revert).
  Full scenario-service suite: **211 passing** (needs `pytest-asyncio`).
- ✅ **Extracted into the standalone `payprobe-assistant` container.**
  `packages/payprobe-assistant/` — FastAPI app (`/health`, `/agent/chat`,
  `/agent/revert`), a REST-backed tool registry (same tiers/journal/guardrails/
  restore, every handler calls scenario-service/orchestrator over HTTP with a
  service JWT), provider-neutral loop + OpenAI/Anthropic adapters, Redis/in-mem
  sessions, env-based LLM config (the egress boundary). Dockerfile + compose
  service (`ASSIST_PORT` 8400) + README. Portal points at `assistantApiBase`
  (defaults to the scenario service, which still exposes `/agent/chat` during
  migration). Tests: `payprobe-assistant/tests` (15, offline: fake REST backend
  + scripted provider) added to Makefile `PKGS`; cross-package suite **290
  passing**.
- ⏭ Follow-ups: unify the two tool layers (scenario-service in-process vs
  assistant REST) into one shared library to kill drift; verify the caller's JWT
  / enforce per-user RBAC in the assistant before exposing it outside a trusted
  network; add an `/api/assistant` nginx route and flip the portal's prod
  `assistantApiBase`; optional `/assistant` full-page route; retire the
  scenario-service `/agent/*` shim once traffic is on the new container.
