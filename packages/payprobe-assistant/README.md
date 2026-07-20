# payprobe-assistant

The PayProbe **configuration assistant** as a standalone service — a multi-turn,
tool-calling agent and the project's **unified LLM gateway**.

It reads and writes PayProbe configuration (connections, formats, tables,
variables, starter flows, scenarios) by calling the scenario-service /
orchestrator REST APIs with a service token, runs a provider-neutral
tool-calling loop, and journals every write so changes are reversible.

It is the only service that talks to LLM providers — one egress boundary, one
key store.

## Run

```bash
SCENARIO_API_URL=http://localhost:8000 \
RUN_API_URL=http://localhost:8001 \
AUTH_JWT_SECRET=dev-insecure-change-me \
ASSIST_LLM_PROVIDER=openai \
ASSIST_LLM_API_KEY=sk-... \
ASSIST_LLM_MODEL=gpt-4o \
uvicorn assistant_service.main:app --port 8400
```

## Endpoints

| Method | Path            | Purpose                                            |
|--------|-----------------|----------------------------------------------------|
| GET    | `/health`       | Liveness + whether an LLM is configured.           |
| POST   | `/agent/chat`   | `{messages[], mode: full\|advisor, session_id?}` → reply + tool calls + journal + `session_id`. |
| POST   | `/agent/revert` | `{session_id}` → undo every change made in that session. |

`advisor` mode exposes only read tools; `full` enables autonomous writes (each
journalled and revertible).

## Environment

| Var | Meaning |
|-----|---------|
| `SCENARIO_API_URL` / `RUN_API_URL` | upstream PayProbe services |
| `AUTH_JWT_SECRET` or `ASSIST_API_TOKEN` | service credential for upstream calls |
| `ASSIST_LLM_PROVIDER` | `openai` (default) or `anthropic` |
| `ASSIST_LLM_API_KEY` | provider key (falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`) |
| `ASSIST_LLM_MODEL` / `ASSIST_LLM_BASE_URL` | model id / endpoint override |
| `REDIS_URL` | enables cross-replica session journals (else in-memory) |
| `CORS_ORIGINS` | comma-separated allowed origins |

## Notes / follow-ups

- The tool layer mirrors scenario-service's in-process `agent_tools`, but every
  handler is REST-backed. The two should be unified into a shared library so the
  registry has a single source of truth.
- This service does not yet verify the **caller's** JWT / enforce per-user RBAC
  (it authenticates itself to upstream with a service token). Add a fail-closed
  auth gate before exposing it outside a trusted network.
