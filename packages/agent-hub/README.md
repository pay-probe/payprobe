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
| `AGENT_HUB_ENGINE_TICK_S` | workflow engine housekeeping interval (default 30): expires timed-out approvals |

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
| `PAYPROBE_ENV`, `API_TOKEN`, `AUTH_JWT_SECRET` / `AUTH_JWT_PUBLIC_KEY` | the platform auth gate (fails closed outside dev) |

## Test

```sh
cd packages && PYTHONPATH=. pytest agent-hub/tests -q       # or: make test-agent-hub
```

Needs a reachable Postgres at `AGENT_HUB_TEST_DATABASE_URL` (default: the
compose dev credentials on localhost); without one the suite skips with a
reason rather than silently passing against a file.
