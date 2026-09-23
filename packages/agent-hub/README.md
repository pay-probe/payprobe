# agent-hub — agent registry (ADR-0010, phase 1)

Registry of **agent principals** and **workflow definitions** for PayProbe.
An agent definition is an identity: role, instructions, a tool allowlist drawn
from the one toolkit registry (`payprobe_common.agent_toolkit`), a mode
(`advisor` | `plan` | `full`, default `plan`), a write scope, per-heartbeat
limits, a schedulability budget, triggers and RBAC. A workflow is a JSON DAG of
`agent_task` / `tool` / `condition` / `approval` / `parallel` / `join` nodes.

Published versions are immutable (provenance hash `spec_sha256`), one version
is active per definition, pinned references (`name@N`) survive newer
publishes, builtins cannot be retired. Validation happens at publish time and
lives in machinery, not prompts: unknown tools, read-only `advisor` agents
holding write tools, mode escalation in workflows, `full` mode outside `mock`
without an `approval` ancestor, and reviewer == executor are all refused.

State is **PostgreSQL only** (the platform database, tables prefixed
`agent_hub_`, numbered idempotent migrations applied at startup). There is no
file fallback; a missing DSN fails startup.

## Run

```sh
AGENT_HUB_DATABASE_URL=postgresql://payprobe:payprobe@localhost:5432/payprobe \
AUTH_JWT_SECRET=... uvicorn agent_hub.main:app --port 8600
```

| Env | Meaning |
|---|---|
| `AGENT_HUB_DATABASE_URL` (or `DATABASE_URL`) | Postgres DSN, required |
| `AGENT_HUB_POOL_MAX` | pool size (default 5) |
| `AGENT_HUB_SEED` | `0` disables seeding the five builtins on first start |
| `AGENT_HUB_ADMIN_ROLES` | roles that may create/publish/pause (default `admin`) |
| `AGENT_HUB_MODEL_ALLOWLIST` | optional comma list restricting `spec.model` |
| `AGENT_HUB_ALERT_WEBHOOK_URL` | D11 alert webhook: POSTed on `failed` / `budget_exceeded` / `timed_out` heartbeats and on advisor findings at `warn` or above; unset = disabled |
| `AGENT_HUB_ALERT_WEBHOOK_SECRET` | signs each POST as `X-PayProbe-Signature: t=<ts>,v1=<HMAC-SHA256("<ts>.<body>")>` (the Stripe scheme); unset = unsigned |
| `AGENT_HUB_ALERT_TIMEOUT_S` | per-attempt timeout (default 5; 3 attempts, backoff 1 s then 4 s) |
| `AGENT_HUB_ENGINE_TICK_S` | housekeeping interval (default 30): expires timed-out approvals, wakes due `schedule` triggers |
| `AGENT_HUB_SCHEDULER` | `0` turns the schedule ticker off (events via `POST /events` still wake agents) |
| `AGENT_HUB_WEBHOOK_SECRET` | HMAC secret for inbound `/webhooks/events/{event}` and `/webhooks/agents/{name}` (signature `X-PayProbe-Signature: t=<ts>,v1=<HMAC-SHA256("<ts>.<body>")>`); unset = those routes answer 503 |
| `AGENT_HUB_EGRESS_ALLOW` | extra LLM hosts a prompt may be sent to (comma list; exact host, `*.suffix`, optional `:port`; may be http). Always allowed over https: `api.openai.com`, `api.anthropic.com`, and the host of `ASSIST_LLM_BASE_URL`. Anything else fails the heartbeat with `egress refused` before a byte leaves; `/health.egress` lists the effective set |

Subjects: every heartbeat may carry a `subject` (`run:<run id>` when the wake
input is a JSON object with `run_id`, which event payloads and triage wakes
are; `wfrun:<id>` for workflow agent tasks; or an explicit `subject` on the
wake body). `GET /heartbeats?subject=run:<id>` is how the portal's run report
attaches the agents' verdicts to a run: shown as advisory, never consulted by
the gates.

Workflow runs (phase 3): `POST /workflows/{name}/run` (inputs, optional
`version`), `GET /runs[?workflow=&status=]`, `GET /runs/{id}`,
`POST /runs/{id}/cancel`, `GET /approvals[?status=pending|all]` (the inbox),
`POST /approvals/{id}/decide` (`approved` / `rejected` + note, needs one of
the node's roles or admin), `GET /plans[?run=]`. A run's `node_states` and
`results` are the whole state; a restart resumes every active run from them.

Folded-in assistant (ADR-0010 D2): the former :8400 service is mounted under
`/assistant` (`/assistant/agent/chat`, `/assistant/agent/chats`, ...), with
its own gate and stores (`REDIS_URL` for sessions and history, memory when
unset). `/assistant/health` is public and answered by agent-hub. The
`assistant` compose service is a deprecated alias for one release.

Wake sources (phase 4): `POST /events` (`{"event": "run.failed", "subject":
{...}}`, platform service token or admin) wakes every active agent with a
matching `event` trigger, one heartbeat each with the event as input; the
orchestrator emits `run.completed`, `run.failed` and `gate.failed` when
`AGENT_HUB_API_URL` is set. `schedule` triggers (`interval_sec`, `daily_at`
UTC) are woken by agent-hub's own tick, never while agents are paused.

Inbound webhooks (for systems without a PayProbe token): sign the raw body
with `AGENT_HUB_WEBHOOK_SECRET` (`t=<unix ts>,v1=<hex HMAC-SHA256 of
"<ts>.<body>">`, 5 min tolerance) and POST it to
`/webhooks/events/{event}` (JSON object; wakes agents with that `event`
trigger) or `/webhooks/agents/{name}` (any text, becomes the wake input; the
agent must declare `{"kind": "webhook"}` in its triggers, else 409).
| `PAYPROBE_ENV`, `API_TOKEN`, `AUTH_JWT_SECRET` / `AUTH_JWT_PUBLIC_KEY` | the platform auth gate (fails closed outside dev) |

## Test

```sh
cd packages && PYTHONPATH=. pytest agent-hub/tests -q       # or: make test-agent-hub
```

Needs a reachable Postgres at `AGENT_HUB_TEST_DATABASE_URL` (default: the
compose dev credentials on localhost, database **`payprobe_test`**, created on
demand); without one the suite skips with a reason rather than silently
passing against a file. The suite truncates every agent-hub table before each
test, so never point it at a database holding a real registry: the old default
(the platform's `payprobe` database) erased a day of heartbeat history on
2026-09-23. Compose does not publish 5432; forward it first (a `socat`
sidecar on the compose network, or a compose override).
