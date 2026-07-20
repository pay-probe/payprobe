---
name: payprobe-config-and-flags
description: >
  Catalog of every PayProbe configuration axis: environment variables per
  service (defaults, effect, prod-vs-experimental, where read), feature flags
  and escape hatches, file-backed stores (registries, runs.db, runtime.json,
  simulators.json), docker-compose ports/profiles/volumes, and portal runtime
  config. Load this when you need to know what an env var does, what its
  default is, which flag gates a behaviour (auth gate, code sandbox, connection
  resolution, provisioner mode, scheduler), where a registry file lives, which
  port a service uses, or when ADDING a new flag/config option (checklist
  included). Keywords: PAYPROBE_ENV, API_TOKEN, AUTH_JWT_SECRET,
  PAYPROBE_SECRET_KEY, PAYPROBE_CODE_SANDBOX, REDIS_URL, RUN_DB,
  ORCH_RUNTIME_FILE, SIMULATORS_FILE, DISABLE_SCHEDULER, provisioner,
  feature flag, default, configuration, .env, compose profile.
---

# PayProbe configuration and flags catalog

Every configuration axis in the platform, verified against source on
2026-07-03. Each entry cites the file and symbol that reads it — when in doubt,
the code wins; re-verify with the drift-check greps at the end of each section.

**When NOT to use this skill:**
- Recreating a dev environment / installing deps / building → `payprobe-build-and-env`.
- Running the stack, deploying, operating compose day-to-day → `payprobe-run-and-operate`.
- WHY a flag exists (design rationale, history) → `payprobe-architecture-contract`, `payprobe-failure-archaeology`.
- Change-control rules for flipping a default → `payprobe-change-control`.

Conventions used below:
- **Tier** — `prod` (a real production knob), `dev` (dev/test convenience only),
  `ops` (operational escape hatch / kill switch), `test` (test-suite only).
- Boolean flags parse differently per flag — the exact truthy/falsy set is
  noted where it matters. There is NO single shared bool parser; check the site.
- "compose default" = the value `infra/docker/docker-compose.yml` injects,
  which may differ from the in-code default.

---

## 1. Cross-service security axes (read by multiple services)

These are read independently by orchestrator, scenario-service, auth-service,
mcp-server and payprobe-assistant. Set them identically across the stack.

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `PAYPROBE_ENV` | unset (⇒ fail-closed) | `dev`/`development`/`test`/`local` ⇒ auth optional, RUN_DB defaults to `:memory:`, auth-service signs with `dev-insecure-secret` and seeds `admin`/`admin`. Anything else or unset ⇒ auth REQUIRED (503 if unconfigured). Compose sets `dev`. | prod | `packages/orchestrator/api/auth.py:_is_dev`, `packages/scenario-service/api/auth.py:_is_dev`, `packages/auth-service/models/security.py:_secret`, `packages/auth-service/api/main.py:_bootstrap_admin`, `packages/orchestrator/api/main.py:_default_run_db` |
| `API_TOKEN` | unset | Static bearer accepted by the fail-closed auth gate (`Authorization: Bearer <API_TOKEN>`). Also the fallback outbound credential for orchestrator→scenario-service, mcp-server and assistant. | prod | `packages/orchestrator/api/auth.py:_check`, `packages/scenario-service/api/auth.py`, `packages/mcp-server/mcp_server/tools.py:_auth_header`, `packages/payprobe-assistant/assistant_service/rest.py:_auth_header` |
| `AUTH_JWT_SECRET` | unset (auth-service: `dev-insecure-secret` only in dev envs, else refuses to issue) | HS256 shared secret. auth-service SIGNS tokens with it; orchestrator/scenario-service VERIFY with it; mcp-server/assistant MINT short-lived service JWTs from it. Compose default `dev-insecure-change-me`. | prod | `packages/auth-service/models/security.py:_secret`, `packages/orchestrator/api/auth.py:_verify_jwt`, `packages/mcp-server/mcp_server/tools.py:_service_jwt` |
| `AUTH_JWT_PUBLIC_KEY` | unset | RS256 public key (PEM) for JWT verification; presence switches verify alg to RS256 (public key wins over secret). | prod | `packages/orchestrator/api/auth.py:_verify_jwt`, `packages/scenario-service/api/auth.py:_verify_jwt` |
| `AUTH_JWT_AUDIENCE` | unset | Optional expected `aud` claim (verify side) / stamped claim (mint side). | prod | orchestrator+scenario `auth.py:_verify_jwt`; mcp `tools.py:_service_jwt`; assistant `rest.py:_service_jwt` |
| `AUTH_JWT_ISSUER` | unset (auth-service signs `payprobe-auth`) | Optional expected `iss` claim / stamped claim. | prod | same sites as AUDIENCE + `packages/auth-service/models/security.py:ISSUER` |
| `PAYPROBE_SECRET_KEY` | unset (⇒ SecretBox disabled, plaintext passthrough) | Fernet key enabling secrets-at-rest encryption (SecretBox) for secret-named fields in connections, variables, test keys, sign-offs. If set without the `cryptography` package installed ⇒ RuntimeError (fail loud). Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. | prod | `packages/payprobe_common/crypto.py:SecretBox.__init__`, `packages/scenario-service/api/crypto.py:SecretBox.__init__` |
| `REDIS_URL` | unset (⇒ in-memory everywhere) | Switches: event backbone → RedisStreamBackbone, load bus → RedisLoadBus (external worker fleet possible), run control → cross-replica Redis registry, assistant/agent sessions → Redis store. Compose default `redis://redis:6379`. | prod | `packages/orchestrator/api/main.py:_make_backbone/_make_load_bus/make_run_control`, `packages/worker/load_worker.py:_make_bus`, `packages/payprobe-assistant/assistant_service/session.py:build_session_store`, `packages/scenario-service/api/agent_session.py:build_session_store`, `packages/orchestrator/api/worker_provisioner.py` |
| `CORS_ORIGINS` | orchestrator+auth: `http://localhost:8080,http://localhost:4200`; scenario+assistant: `http://localhost:4200,http://127.0.0.1:4200` | Comma-separated allowed browser origins. Note the two different in-code defaults; compose overrides all four with the same value. | prod | `packages/orchestrator/api/main.py` (CORSMiddleware), `packages/scenario-service/api/main.py`, `packages/auth-service/api/main.py`, `packages/payprobe-assistant/assistant_service/main.py` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (⇒ tracing no-op) | Enables OTLP trace export; surfaced as `tracing_configured` in orchestrator `/system`. | prod | `packages/orchestrator/api/observability.py:init_tracing` |

Drift check:
`grep -rn "PAYPROBE_ENV\|API_TOKEN\|AUTH_JWT" packages/*/api/auth.py packages/auth-service/models/security.py`

---

## 2. Orchestrator (`packages/orchestrator`, port 8100)

### 2.1 Durability and storage

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `RUN_DB` | unset | Explicit run-registry path (SQLite) — always wins when set. Compose: `/data/runs.db`. | prod | `packages/orchestrator/api/main.py:_default_run_db` |
| `RUN_DB_PATH` | `/data/runs.db` | Fallback path used only when `RUN_DB` unset AND not a dev env. Dev envs (`PAYPROBE_ENV` in dev/development/test/local) get `:memory:`. Fail-safe: non-dev persists by default. | prod | `packages/orchestrator/api/main.py:_default_run_db` |
| `SCHEDULES_FILE` | `:memory:` | Durable schedule registry (file-backed JSON). | prod | `packages/orchestrator/api/main.py` (ScheduleStore) |
| `SIMULATORS_FILE` | `:memory:` | Durable saved-simulator registry; `enabled` simulators auto-start on boot. Compose: `/data/simulators.json`. | prod | `packages/orchestrator/api/main.py` (SimulatorStore) |
| `SIGNOFFS_FILE` | `:memory:` | Immutable Go/No-Go sign-off snapshots + baselines (ADR-0003). Secret-named fields encrypted via SecretBox when `PAYPROBE_SECRET_KEY` set. | prod | `packages/orchestrator/api/main.py` (SignoffStore) |
| `NETWORK_FLEET` | unset (⇒ in-process hosting) | `1` = place network-flow listeners on the worker fleet (ADR-0001) when ≥1 `python -m worker.flow_host` is heartbeating on the shared Redis bus; otherwise (or without hosts) networks start in-process as before. | prod scale-out | `packages/orchestrator/api/main.py:_fleet_enabled`, `flow_fleet.py`, `packages/worker/flow_host.py` |
| `ORCH_RUNTIME_FILE` | `:memory:` (⇒ persistence disabled) | Desired-state file for standalone participant flows + running network flows (legacy 'topologies' key still restored, ids match); re-launched on boot by `_autostart_runtime`. Compose: `/data/runtime.json`. | prod | `packages/orchestrator/api/main.py:RUNTIME_STATE_FILE`, `_persist_runtime`, `_autostart_runtime` |
| `PAYPROBE_EXAMPLES_DIR` | `<repo>/examples` | Where bundled example scenarios/simulators live (container override). Compose: `/app/examples`. | prod | `packages/orchestrator/api/main.py:EXAMPLES_DIR` |

### 2.2 Service discovery (the /status aggregator + run launch)

| Var | Default | Effect | Where read |
|---|---|---|---|
| `SCENARIO_API_URL` | `http://scenario-service:8000` | Where to fetch scenarios when a run is launched by id/project/set. | `packages/orchestrator/api/main.py:SCENARIO_API_URL` |
| `AUTH_API_URL` | `http://auth-service:8300` | Auth-service base for `/status`. | `packages/orchestrator/api/main.py:AUTH_API_URL` |
| `MCP_API_URL` | `""` (not probed) | MCP server base; probed by `/status` only when set. | `packages/orchestrator/api/main.py:MCP_API_URL` |
| `ASSIST_API_URL` | `""` (not probed) | Assistant base; probed by `/status` only when set. | `packages/orchestrator/api/main.py:ASSIST_API_URL` |
| `INSIGHT_API_URL` | `""` (not probed) | Insight-service base; probed by `/status` only when set. | `packages/orchestrator/api/main.py:INSIGHT_API_URL` |
| `SCENARIO_API_TOKEN` | unset | Outbound credential for orchestrator→scenario-service (wins over `API_TOKEN`; else falls back to minting a JWT from `AUTH_JWT_SECRET`). All tiers: prod. | `packages/orchestrator/api/main.py` (~line 399, `_scenario_auth_header`-style helper) |

### 2.3 Feature flags and escape hatches

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `PAYPROBE_CONNECTION_OVERRIDE_WINS` | `1` (ON) | Connection's resolved config (base ⊕ environment_overrides) wins over a same-named env-inline adapter. Set `0`/`false`/`no` to restore legacy "inline env adapter wins". Migration is COMPLETE — this is the reversibility escape hatch, not an experiment. | ops | `packages/orchestrator/api/main.py:_CONNECTION_OVERRIDE_WINS` (~line 502) |
| `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION` | `1` (ON) | Action steps naming no connection resolve to the default connection for their adapter type (docs/history/DEFAULT-CONNECTION-MODEL-SPEC.md); graceful fallback to env inline adapter when no default exists. `0`/`false`/`no` disables entirely. | ops | `packages/orchestrator/api/main.py:_DEFAULT_CONNECTION_RESOLUTION` (~line 514) |
| `DISABLE_SCHEDULER` | unset (scheduler ON) | `1` ⇒ don't start the background schedule loop. Used by virtually every orchestrator test; also an ops kill switch. | test/ops | `packages/orchestrator/api/main.py:_start_scheduler` (~line 1868) |
| `DISABLE_SIMULATOR_AUTOSTART` | unset (autostart ON) | `1` ⇒ skip auto-starting saved simulators flagged `enabled` on boot. | test/ops | `packages/orchestrator/api/main.py:_autostart_simulators` (~line 3810) |
| `DISABLE_PARTICIPANT_AUTOSTART` | unset (autostart ON) | `1` ⇒ skip restoring persisted participant listeners + topology runs on boot. | test/ops | `packages/orchestrator/api/main.py:_autostart_runtime` (~line 3852) |
| `PAYPROBE_ALLOW_UNAUTH_CODE` | unset (OFF) | `1`/`true`/`yes` ⇒ allow code-node execution when auth is off AND no network sandbox is active (otherwise refused to prevent unauthenticated RCE/SSRF). Trusted local dev ONLY. | dev | `packages/orchestrator/api/main.py` (~line 4315 and `/system` ~4810) |

### 2.4 Load subsystem and worker provisioning

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `PAYPROBE_LOAD_EXTERNAL_WORKERS` | unset (fallback ON) | `1`/`true`/`yes` ⇒ disable the in-process worker fallback; shards must be claimed by external `python -m worker.load_worker` processes. Set on a real distributed fleet. | prod | `packages/orchestrator/api/main.py:_external_only` (~line 232) |
| `PAYPROBE_INPROC_MAX_TPS` | `2000` (float) | Cap on requested TPS the in-process fallback will absorb (review-hardening cap). | prod | `packages/orchestrator/api/main.py` (LoadCoordinator ctor ~line 242) |
| `PAYPROBE_INPROC_MAX_CONNECTIONS` | `5000` (int) | Same cap for connection count. | prod | `packages/orchestrator/api/main.py` (~line 243) |
| `PAYPROBE_WORKER_PROVISIONER` | `auto` | Fleet provisioner: `auto` \| `local` \| `docker` \| `compose` \| `none`. `auto` prefers docker (needs `PAYPROBE_WORKER_IMAGE`), else compose (needs `PAYPROBE_COMPOSE_FILE`), else local subprocesses (needs `REDIS_URL`), else none. Compose default: `docker`. | prod | `packages/orchestrator/api/worker_provisioner.py:make_provisioner` (~line 429) |
| `PAYPROBE_WORKER_IMAGE` | `""` | Image for the `docker` backend (`docker run -d` per worker). Compose default `payprobe-load-worker`. Backend reports unavailable when empty. | prod | `worker_provisioner.py:DockerProvisioner.__init__` (~line 348) |
| `PAYPROBE_WORKER_NETWORK` | `""` | Docker network for spawned workers (so `redis://redis:6379` resolves). Compose default `payprobe_default`. | prod | `worker_provisioner.py:DockerProvisioner.__init__` (~line 349) |
| `PAYPROBE_COMPOSE_FILE` | `""` | Compose file path (inside the orchestrator container) for the `compose` backend. Backend unavailable when empty. | prod | `worker_provisioner.py:ComposeProvisioner.__init__` (~line 389) |
| `PAYPROBE_LOAD_WORKER_SERVICE` | `load-worker` | Compose service name the `compose` backend runs. | prod | `worker_provisioner.py:ComposeProvisioner.__init__` (~line 390) |
| `COMPOSE_PROJECT_NAME` | `payprobe` | Compose project prefix used to name/locate worker containers. | prod | `worker_provisioner.py` (~line 198) |
| `HOSTNAME` | random 8-hex | Replica owner id for cross-replica run control (set automatically by Docker). | prod | `packages/orchestrator/api/run_control.py` (~line 83) |

Drift check:
`grep -n "environ" packages/orchestrator/api/main.py packages/orchestrator/api/auth.py packages/orchestrator/api/worker_provisioner.py packages/orchestrator/api/run_control.py packages/orchestrator/api/observability.py`

---

## 3. Scenario-service (`packages/scenario-service`, port 8000)

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `DATABASE_URL` | falls back to `SCENARIO_DB`, then `scenarios.db` | Scenario store: SQLite file path or Postgres URL. Compose: `/data/scenarios.db` (SQLite on the `scenario_data` volume; the Postgres store needs schema reconciliation before use — see compose comment). | prod | `packages/scenario-service/api/main.py:DATABASE_URL` (~line 113) |
| `SCENARIO_DB` | `scenarios.db` | Legacy alias consulted only when `DATABASE_URL` unset. | prod | same site |
| `SCENARIO_SEED_DIR` | `<repo>/examples/scenarios` | Seed scenarios imported on FIRST start (only when the store is empty). Compose mounts `examples/scenarios` at `/seed`. | prod | `packages/scenario-service/api/main.py:SEED_DIR` |
| `ENV_SEED_DIR` | sibling of SEED_DIR: `<repo>/examples/environments` | First-start seeding of the Environments registry. Compose mounts at `/environments`. | prod | `packages/scenario-service/api/main.py:ENV_SEED_DIR` |
| `CONN_SEED_DIR` | `<repo>/examples/connections` | First-start seeding of the Connections registry. Compose mounts at `/connections`. | prod | `packages/scenario-service/api/main.py:CONN_SEED_DIR` |
| `MAX_BODY_BYTES` | `1048576` (1 MiB) | Request body size cap. | prod | `packages/scenario-service/api/main.py:MAX_BODY_BYTES` (~line 128) |
| `CATALOG_FILE` | sibling of SQLite DB (`catalog.json`) else `:memory:` | Step-catalog persistence. | prod | `packages/scenario-service/api/main.py:_default_catalog_file` |
| `ASSIST_LLM_KEY` / `OPENAI_API_KEY` | unset | Env-provided LLM key for the in-service `/agent/chat` + scenario assistant (checked before the Settings-stored key). | prod | `packages/scenario-service/api/main.py` (~lines 1234, 1277) |
| `ASSIST_SESSION_TTL` | `86400` (24 h, seconds) | Idle agent-session journal TTL. | prod | `packages/scenario-service/api/agent_session.py:SESSION_TTL_S` |
| `RUN_API_URL` / `INSIGHT_API_URL` | `http://localhost:8100` / `http://localhost:8500` | In-process assistant's runtime-read bridge (orchestrator) + advisory insight tools (optional deployment). | prod | `packages/scenario-service/api/agent_tools.py:_run_api_get` / `_insight_api_get` |
| `PAYPROBE_ENV`, `API_TOKEN`, `AUTH_JWT_*`, `PAYPROBE_SECRET_KEY`, `REDIS_URL`, `CORS_ORIGINS` | see section 1 | Same fail-closed auth gate + SecretBox + session store as elsewhere. | prod | `packages/scenario-service/api/auth.py`, `api/crypto.py`, `api/agent_session.py` |

### Registry file overrides (the `_sibling_file` convention)

Each registry persists as a JSON file NEXT TO the SQLite scenarios DB
(`<db-dir>/<name>.json`); `:memory:` when the DB isn't a plain SQLite path.
Each can be overridden by an env var that is simply the UPPERCASE registry
name (unusual — no prefix):

| Env var | Default file | Registry |
|---|---|---|
| `FORMATS` | `<db-dir>/formats.json` | Message formats (ISO 8583 dialects) |
| `VARIABLES` | `<db-dir>/variables.json` | Global/project/set variables |
| `TABLES` | `<db-dir>/tables.json` | Lookup tables |
| `CONNECTIONS` | `<db-dir>/connections.json` | Connections (adapter instances; SecretBox-encrypted secret fields) |
| `FLOWS` | `<db-dir>/flows.json` | Starter flows |
| `PARTICIPANT_FLOWS` | `<db-dir>/participant_flows.json` | Participant flows |
| `PARTICIPANT_GROUPS` | `<db-dir>/participant_groups.json` | Typed participant groups |
| `TOPOLOGIES` | `<db-dir>/topologies.json` | Topologies (legacy — read once to seed network flows) |
| `NETWORK_FLOWS` | `<db-dir>/network_flows.json` | Network flows (ADR-0004) |
| `ASSIST` | `<db-dir>/assist.json` | AI-assistant config (provider/key via Settings) |
| `TEST_DATA` | `<db-dir>/test_data.json` | Card pools, BIN ranges, terminal pools, test keys |
| `ENVIRONMENTS` | `<db-dir>/environments.json` | Environments registry |

Where read: `packages/scenario-service/api/main.py:_sibling_file` (~line 145)
and the `*_FILE` constants at lines 153–163.

Drift check:
`grep -n "_sibling_file\|environ" packages/scenario-service/api/main.py | head -40`

---

## 4. Auth-service (`packages/auth-service`, port 8300)

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `AUTH_DB` | `/data/auth.db` | SQLite user store path (`:memory:` supported). | prod | `packages/auth-service/models/store.py:UserStore.__init__` (~line 35) |
| `AUTH_TOKEN_TTL_SEC` | `7200` (code) / `3600` (compose) | Issued-JWT lifetime, seconds. | prod | `packages/auth-service/models/security.py:DEFAULT_TTL_SEC` |
| `AUTH_JWT_SECRET` | dev envs: `dev-insecure-secret`; else REFUSES to issue (RuntimeError) | HS256 signing secret. | prod | `packages/auth-service/models/security.py:_secret` |
| `AUTH_JWT_ISSUER` | `payprobe-auth` | `iss` claim on issued tokens. | prod | `security.py:ISSUER` |
| `AUTH_ADMIN_USER` | `admin` | First-start bootstrap admin username (only when the store is empty). | prod | `packages/auth-service/api/main.py:_bootstrap_admin` (~line 59) |
| `AUTH_ADMIN_PASSWORD` | dev envs: `admin`; prod: unset ⇒ NO users seeded (operator must seed explicitly — no known-default account) | Bootstrap admin password. | prod | `api/main.py:_bootstrap_admin` |

Drift check:
`grep -rn "environ" packages/auth-service --include="*.py" | grep -v tests`

---

## 5. Worker / load-worker (`packages/worker`)

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `INSIGHT_API_URL` | `http://localhost:8500` | Default base for the `insight` predict-step adapter when its connection omits base_url. | prod | `packages/worker/adapters/insight/adapter.py` |
| `PAYPROBE_CODE_SANDBOX` | `auto` | Code-node network sandbox mode: `auto` (unshare netns when the kernel allows, rootless preferred), `strict` (REQUIRE isolation; refuse to run code otherwise — raises with "code sandbox required"), `off` (no isolation). Recommended `strict` for untrusted authors. Rlimits always apply: 15 s CPU, 256 MiB mem, 16 MiB fsize, 128 fds, 5 s default / 60 s max wall timeout. | prod | `packages/worker/engine/code_runner.py:_sandbox_mode` (~line 65), strict check ~line 272 |
| `LOAD_RUN_ID` | REQUIRED (SystemExit if missing) | Which load run this standalone worker binds to. | prod | `packages/worker/load_worker.py:_main` (~line 353) |
| `REDIS_URL` | REQUIRED for standalone worker (SystemExit if missing) | Load bus coordination. | prod | `packages/worker/load_worker.py:_make_bus` (~line 343) |
| `LOG_LEVEL` | `INFO` | Standalone load-worker logging level. | prod | `packages/worker/load_worker.py:_main` (~line 352) |

Drift check:
`grep -rn "environ" packages/worker --include="*.py" | grep -v tests | grep -v node_modules`

---

## 6. MCP server (`packages/mcp-server`, port 8200)

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `sse` \| `streamable-http`. Compose: `streamable-http` (clients connect to `http://<host>:8200/mcp`). | prod | `packages/mcp-server/mcp_server/server.py:main` (~line 150) |
| `MCP_HOST` | `0.0.0.0` | Bind host (HTTP transports). | prod | `server.py:main` |
| `MCP_PORT` | `8200` | Bind port (HTTP transports). | prod | `server.py:main` |
| `MCP_DNS_REBIND_PROTECTION` | on | `0`/`off`/`false`/`no` ⇒ disable DNS-rebinding protection entirely. | ops | `server.py:_configure_transport_security` (~line 116) |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | localhost/127.0.0.1/[::1]/mcp-server (+bind host) | Comma-separated EXTRA allowed hosts/origins appended to the built-in allowlist. | prod | `server.py:_configure_transport_security` (~lines 127–130) |
| `SCENARIO_API_URL` | `http://localhost:8000` | Upstream scenario-service. | prod | `packages/mcp-server/mcp_server/tools.py:SCENARIO_API` |
| `RUN_API_URL` | `http://localhost:8001` — NOTE: does NOT match the orchestrator's real port 8100; always set explicitly (compose sets `http://orchestrator:8100`) | Upstream orchestrator. | prod | `tools.py:RUN_API` |
| `INSIGHT_API_URL` | `http://localhost:8500` | Upstream insight-service (advisory tools `get_run_insights` / `list_insight_predictions` / `train_insights`). | prod | `tools.py:INSIGHT_API` |
| `MCP_API_TOKEN` | unset | Static bearer for upstream calls; wins over `API_TOKEN`, which wins over JWT minting. | prod | `tools.py:_auth_header` (~line 65) |
| `MCP_JWT_TTL` | `3600` | Minted service-JWT lifetime (s). | prod | `tools.py:_service_jwt` (~line 44) |
| `MCP_JWT_SUB` | `mcp-server` | `sub` claim on minted JWTs. | prod | `tools.py:_service_jwt` (~line 46) |

Drift check:
`grep -rn "environ" packages/mcp-server --include="*.py" | grep -v tests`

---

## 7. Assistant (`packages/payprobe-assistant`, port 8400)

The unified LLM gateway — the ONLY service meant to hold provider keys.

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `ASSIST_LLM_PROVIDER` | `openai` | `openai` or `anthropic`. | prod | `packages/payprobe-assistant/assistant_service/config.py:resolve_llm` |
| `ASSIST_LLM_API_KEY` | unset (⇒ falls back to Settings → AI assistant; LLM disabled only if that's unset too) | Provider key (prod override — wins over the Settings-stored key); falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` by provider. | prod | `config.py:resolve_llm` |
| `ASSIST_SETTINGS_LLM` | `1` | Set `0` to disable the Settings-stored key fallback (pulled from scenario-service `/assist/config/material`, 15 s TTL cache) — env-only mode for tests / air-gapped. | dev | `config.py:_settings_enabled` |
| `ASSIST_LLM_MODEL` | `gpt-4o-mini` / `claude-3-5-sonnet-latest` | Model id per provider. | prod | `config.py:resolve_llm` |
| `ASSIST_LLM_BASE_URL` | provider default endpoint | Endpoint override (proxies/gateways). | prod | `config.py:resolve_llm` |
| `SCENARIO_API_URL` / `RUN_API_URL` | `http://localhost:8000` / `http://localhost:8001` (same port-8001 mismatch as MCP — set explicitly) | Upstream services. | prod | `assistant_service/rest.py` (~lines 22–23) |
| `INSIGHT_API_URL` | `http://localhost:8500` | Insight-service base for the advisory read tools (optional deployment — tools fail with a clear message when down). | prod | `assistant_service/rest.py:INSIGHT_API` |
| `ASSIST_API_TOKEN` | unset | Static upstream bearer; wins over `API_TOKEN`, then JWT minting from `AUTH_JWT_SECRET`. | prod | `rest.py:_auth_header` (~line 57) |
| `ASSIST_JWT_TTL` / `ASSIST_JWT_SUB` | `3600` / `payprobe-assistant` | Minted service-JWT lifetime / subject. | prod | `rest.py:_service_jwt` |
| `ASSIST_SESSION_TTL` | `86400` | Idle session TTL (s); Redis-backed when `REDIS_URL` set. | prod | `assistant_service/session.py:SESSION_TTL_S` |

Drift check:
`grep -rn "environ" packages/payprobe-assistant --include="*.py" | grep -v tests`

---

## 7.5 Insight-service (`packages/insight-service`, port 8500) — ADR-0005

Advisory ML over run history (failure categorization / explanation / outcome
prediction). Read-only against the orchestrator; never gates anything.

| Var | Default | Effect | Tier | Where read |
|---|---|---|---|---|
| `RUN_API_URL` | `http://localhost:8100` | Orchestrator base for run-history reads. | prod | `insight_service/rest.py` |
| `INSIGHT_DB` | `:memory:` (compose: `/data/insight.db`) | SQLite corpus/outcomes/predictions store. | prod | `insight_service/store.py` |
| `INSIGHT_TRAIN_INTERVAL_SEC` | `0` (off; compose: `86400`) | Periodic self-training tick (ingest + refit); 0 ⇒ only explicit `POST /train`. | prod | `insight_service/main.py:lifespan` |
| `INSIGHT_DATA_DIR` | beside `INSIGHT_DB` (`<db dir>/models`); compose: `/data/models` | Pickled model artifacts (custom + auto-trained cluster model), reloaded at boot. `:memory:` db + unset ⇒ no persistence. | prod | `insight_service/custom_models.py:data_dir` |
| `INSIGHT_API_TOKEN` | unset | Outbound static bearer to the orchestrator; wins over `API_TOKEN`, then JWT from `AUTH_JWT_SECRET`. | prod | `insight_service/rest.py:_auth_header` |
| `INSIGHT_JWT_TTL` | `3600` | Minted service-JWT lifetime. | prod | `insight_service/rest.py:_service_jwt` |
| caller gate | same axes as every service (`API_TOKEN` / `AUTH_JWT_SECRET` / `PAYPROBE_ENV`) | fail-closed; `/health` + `/status` public. | prod | `insight_service/auth.py` |

The learned layer needs the `[ml]` extra (scikit-learn; in the Docker image, NOT in
the default test env) + a ≥50-failure corpus; below that the deterministic
heuristic/frequency baselines answer — by design. Consumers (`INSIGHT_API_URL`):
orchestrator `/status`, mcp-server tools, both assistant backends, portal
(`insightApiBase`: dev `http://localhost:8500`, prod `/api/insights`).

Drift check:
`grep -rn "environ" packages/insight-service --include="*.py" | grep -v tests`

---

## 8. Portal (`packages/portal`)

No server-side env vars — configuration is (a) build-time, (b) browser-runtime.

- **Build-time bases** — `packages/portal/src/environments/environment.ts`
  (dev: `scenarioApiBase=http://localhost:8000`, `runApiBase=http://localhost:8100`,
  `authApiBase=http://localhost:8300`, `assistantApiBase=http://localhost:8000`)
  and `environment.prod.ts` (same-origin nginx paths: `/api/scenarios`,
  `/api/orch`, `/api/auth`; assistant via `/api/scenarios`, switchable to
  `/api/assistant`). WebSocket URLs are derived from `runApiBase`
  (http(s)→ws(s)), so REST and stream always agree.
- **Runtime endpoint overrides** — `packages/portal/src/app/shared/runtime-config.service.ts`
  (`RuntimeConfigService`): per-endpoint scheme/host/port overrides edited in
  Settings → Endpoints, persisted in localStorage under key
  `payprobe.endpoints`, applied per-request (no rebuild/reload). Endpoints:
  portal, orchestrator, scenario, auth, assistant, redis, mcp — redis/mcp are
  probed server-side via the orchestrator `/status` aggregator (browsers can't
  reach them), portal+redis are monitor-only.
- **`E2E_FULL`** — `packages/portal/e2e/full-flows.spec.ts`: `E2E_FULL=1 npm run e2e`
  enables full end-to-end specs that need the backend stack running; skipped
  otherwise. Tier: test.

Drift check:
`grep -rn "E2E_FULL\|STORAGE_KEY" packages/portal/src/app/shared/runtime-config.service.ts packages/portal/e2e/full-flows.spec.ts`

---

## 9. File-backed stores — what lands where

| Store | Owner | Location (compose) | Location (bare dev) | Notes |
|---|---|---|---|---|
| Scenarios | scenario-service | `/data/scenarios.db` (volume `scenario_data`) | `./scenarios.db` (cwd) | Seeded from `examples/scenarios` on FIRST start only (empty store) |
| Registries (formats, variables, tables, connections, flows, participant flows/groups, topologies (legacy), network flows, assist, test data, environments, catalog) | scenario-service | `/data/<name>.json` beside the DB | `<name>.json` beside `scenarios.db` | `:memory:` when DB isn't plain SQLite; env overrides in section 3 |
| Environments seed | scenario-service | `/environments` (ro mount of `examples/environments`) | `examples/environments` | Seeds registry on first start (mock, grpc, http-live, payshield-sim, production-template) |
| Connections seed | scenario-service | `/connections` (ro mount of `examples/connections`) | `examples/connections/bundled.json` | Seeds registry on first start |
| Run registry | orchestrator | `/data/runs.db` (volume `orchestrator_data`) | `:memory:` in dev envs | See `_default_run_db` fail-safe logic |
| Saved simulators | orchestrator | `/data/simulators.json` | `:memory:` | `enabled` ⇒ auto-start on boot |
| Runtime desired-state | orchestrator | `/data/runtime.json` | `:memory:` | Participant flows + topologies restored on boot |
| Schedules / sign-offs | orchestrator | unset in compose ⇒ `:memory:` (schedules/sign-offs DO NOT survive container recreation unless you set `SCHEDULES_FILE`/`SIGNOFFS_FILE`) | `:memory:` | Verified: compose sets neither var |
| Users | auth-service | `/data/auth.db` (volume `auth_data`) | `/data/auth.db` (mkdir'd) | Bootstrap admin rules in section 4 |

---

## 10. infra/docker compose axes (verified against `infra/docker/docker-compose.yml`)

- `infra/docker/docker-compose.mock.yml` is SUPERSEDED — defines no services;
  the main compose runs in mock mode by default. `.env.example` (copy to
  `.env`) documents the main knobs.

**Published host ports** (all overridable via `.env`):

| Service | Var | Default host port | Container port |
|---|---|---|---|
| portal (nginx) | `PORTAL_PORT` | 8080 | 80 |
| orchestrator | `ORCH_PORT` | 8100 | 8100 |
| scenario-service | `SCENARIO_PORT` | 8000 | 8000 |
| auth-service | `AUTH_PORT` | 8300 | 8300 |
| mcp-server | `MCP_PORT` | 8200 | 8200 |
| assistant | `ASSIST_PORT` | 8400 | 8400 |
| insight-service | `INSIGHT_PORT` | 8500 | 8500 |
| prometheus | `PROMETHEUS_PORT` | 9090 | 9090 |
| grafana | `GRAFANA_PORT` | 3000 | 3000 |

(postgres and redis publish no host ports.)

**Profiles:**

| Profile | Adds |
|---|---|
| (default) | postgres, redis, auth-service, scenario-service, orchestrator, mcp-server, assistant, portal — the working mock stack |
| `full` | 501-returning stub placeholders: report-service, helper-restpay, helper-hsm, helper-log, helper-db-probe |
| `observability` | prometheus + grafana (`GRAFANA_USER`/`GRAFANA_PASSWORD`, default admin/admin) |
| `tools` | one-shot `worker` CLI image (`docker compose run --rm worker`) |
| `load` | `load-worker` service — manual fleet: `LOAD_RUN_ID=<id> docker compose --profile load up -d --scale load-worker=4 load-worker` |

**Volumes:** `postgres_data`, `redis_data`, `scenario_data`, `orchestrator_data`,
`auth_data`, `prometheus_data`, `grafana_data`. Orchestrator also mounts
`/var/run/docker.sock` (required by the `docker`/`compose` provisioner
backends).

**Compose-only vars:** `POSTGRES_DB`/`POSTGRES_USER`/`POSTGRES_PASSWORD`
(default all `payprobe` — change for any non-local deploy),
`SCENARIO_DATABASE_URL` (Postgres store opt-in, currently blocked on schema
reconciliation), `GRAFANA_USER`/`GRAFANA_PASSWORD`.

Drift check:
`grep -n '\${' infra/docker/docker-compose.yml` and `cat infra/docker/.env.example`

---

## 11. Prod-vs-experimental posture (2026-07-03)

- **Production knobs:** everything in sections 1–7 not listed below.
- **Escape hatches (default ON, keep unless rolling back):**
  `PAYPROBE_CONNECTION_OVERRIDE_WINS`, `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION`
  — both migrations are complete; the flags exist to honor the reversibility
  non-negotiable. Do not remove them without change control.
- **Dev-only, never in prod:** `PAYPROBE_ALLOW_UNAUTH_CODE`,
  `PAYPROBE_ENV=dev` (opens the auth gate), `MCP_DNS_REBIND_PROTECTION=0`,
  compose defaults `AUTH_JWT_SECRET=dev-insecure-change-me` and
  `POSTGRES_PASSWORD=payprobe`.
- **Test/ops kill switches:** `DISABLE_SCHEDULER`, `DISABLE_SIMULATOR_AUTOSTART`,
  `DISABLE_PARTICIPANT_AUTOSTART`, `E2E_FULL`.
- **Known footguns:** `RUN_API_URL` defaults to port 8001 in mcp-server and
  assistant but the orchestrator listens on 8100 — always set it;
  compose leaves `SCHEDULES_FILE`/`SIGNOFFS_FILE` unset, so schedules and
  sign-offs are in-memory even in the "durable" compose stack;
  `CORS_ORIGINS` has two different in-code defaults across services.

---

## 12. How to add a configuration axis (checklist)

Honors the non-negotiables: measured not assumed, reversibility mandatory,
suite green before/after (see `payprobe-change-control`).

1. **Flag it OFF by default.** New behaviour ships behind an env var whose
   unset/default state preserves today's behaviour. Look at
   `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION` in
   `packages/orchestrator/api/main.py` for the house pattern: module-level
   constant, `#:` doc comment stating default, effect, and the escape hatch.
2. **Parse booleans explicitly and document the accepted set** — there is no
   shared parser; copy an existing site's truthy/falsy set rather than
   inventing a new one.
3. **Read it ONCE at module/startup scope** where possible (constant), not
   per-request, unless tests need to flip it (then provide a reset helper like
   `code_runner._reset_sandbox_cache`).
4. **Fail loud on misconfiguration** (`PAYPROBE_SECRET_KEY` without
   `cryptography` raises; `AUTH_JWT_SECRET` missing in prod refuses to issue).
   Never silently degrade — silent degradation is a documented costliest
   failure class.
5. **Surface non-secret state** in orchestrator `/system` (or the
   service's `/status`) if operators need to confirm it — on/off only, never
   the value of a secret.
6. **Wire compose:** add `${VAR:-default}` to `infra/docker/docker-compose.yml`
   and document it in `infra/docker/.env.example` if deployers should touch it.
7. **Test both states** (flag on, flag off) and add `DISABLE_*`-style
   overrides to tests that must not start background machinery.
8. **Document it in THIS catalog** (table row: name / default / effect / tier /
   file:symbol) and add a drift-check grep if it's in a new file.
9. **Run `make test`** before and after; run the drift checks in this file to
   confirm the catalog still matches reality.
10. **Only after soak + measurement** propose flipping the default ON — as a
    separate, reviewed change with the old value kept as an escape hatch.

---

## Provenance and maintenance

Authored 2026-07-03 against the live tree; every table row was verified by
reading the citing file. Ports and compose facts verified against
`infra/docker/docker-compose.yml` + `.env.example` on the same date.

Re-verify (run from repo root):

```sh
# Full env-var inventory across Python packages (ignore vendored node-gyp noise):
grep -rhoE 'environ(\.get)?[\[(]\s*["'"'"'][A-Z][A-Z0-9_]+' packages --include="*.py" \
  | grep -oE '[A-Z][A-Z0-9_]+$' | sort -u
# NOTE: multi-line calls (e.g. CORS_ORIGINS, DATABASE_URL) escape that regex — also run:
grep -rn -A1 'environ.get($' packages --include="*.py" | grep -v node_modules
# Flag defaults in the orchestrator:
grep -n 'PAYPROBE_\|DISABLE_\|RUN_DB\|_FILE' packages/orchestrator/api/main.py | head -40
# Compose axes:
grep -n '\${' infra/docker/docker-compose.yml
# Registry file convention:
sed -n '145,165p' packages/scenario-service/api/main.py
# Portal runtime config:
grep -n 'STORAGE_KEY\|EndpointKey' packages/portal/src/app/shared/runtime-config.service.ts
```

If any grep output disagrees with a table above, THE CODE WINS — update this
file in the same change that moved the flag.
