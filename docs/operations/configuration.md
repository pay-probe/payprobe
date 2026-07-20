# Configuration reference

> **Reference** — every environment variable the PayProbe services read, what it
> does, and its default. Set these at deploy time (compose `environment:`, k8s
> env, or your secret manager). The portal's **Settings → System** panel shows
> the resulting posture read-only, so you can confirm a deployment matches this
> table without shelling in.

Conventions: defaults in **bold** are what you get when the variable is unset.
"Dev" means `PAYPROBE_ENV` is one of `dev`, `development`, `test`, `local`.

## Environment mode

| Variable | Default | Effect |
|---|---|---|
| `PAYPROBE_ENV` | **unset** (treated as prod) | `dev`/`test`/etc. relaxes fail-closed behaviour (auth gate, durable storage, code gating). Anything else (or unset) is treated as production: secure-by-default. |

## Authentication

Set the same `AUTH_JWT_SECRET` on the auth-service (which signs) and on every
verifier (orchestrator, scenario-service) so tokens validate locally.

| Variable | Service(s) | Default | Effect |
|---|---|---|---|
| `AUTH_JWT_SECRET` | auth, orchestrator, scenario | — (required in prod) | HS256 signing/verification secret. |
| `AUTH_JWT_PUBLIC_KEY` | orchestrator | — | RS256 public key (PEM) — alternative to the shared secret. |
| `API_TOKEN` | orchestrator, scenario | — | Optional static bearer accepted alongside JWTs (service-to-service). |
| `AUTH_TOKEN_TTL_SEC` | auth | **3600** | Access-token lifetime (seconds). |
| `AUTH_JWT_ISSUER` | auth | **payprobe-auth** | `iss` claim written + checked. |
| `AUTH_ADMIN_USER` | auth | **admin** | Bootstrap admin username (first start only). |
| `AUTH_ADMIN_PASSWORD` | auth | dev: **admin**; prod: unset ⇒ no seed user | Bootstrap admin password. |
| `AUTH_DB` | auth | **/data/auth.db** | SQLite path for the user store. |

The gate **fails closed**: outside dev, a request with no valid credential gets
401, and if *no* auth is configured at all the service returns 503 rather than
serving openly.

## Code-node sandbox

`code` nodes run user Python/JS. See [Code step](../scenarios/code-step.md).

| Variable | Default | Effect |
|---|---|---|
| `PAYPROBE_CODE_SANDBOX` | **auto** | `auto`: network-isolate (Linux netns) when the kernel allows, run without it otherwise. `strict`: require isolation — refuse to run if unavailable (use for untrusted authors). `off`: no isolation (trusted authors only). |
| `PAYPROBE_ALLOW_UNAUTH_CODE` | **unset** | When auth is off *and* no sandbox is active, `/nodes/execute` refuses code nodes. Set to `1` to allow them anyway (trusted local dev only). |

A network namespace blocks egress (SSRF/exfiltration) but does **not** isolate
filesystem reads — run under nsjail/gVisor for fully untrusted multi-tenant code.

## Run state & scaling

| Variable | Default | Effect |
|---|---|---|
| `RUN_DB` | dev: `:memory:`; prod: **/data/runs.db** | Run-registry SQLite path. Persist on a volume so restarts/replicas keep history. WAL mode is enabled for file DBs. |
| `RUN_DB_PATH` | **/data/runs.db** | Default file used in prod when `RUN_DB` is unset. |
| `REDIS_URL` | — | Enables the Redis event backbone, load bus, and cross-replica run control (observe/cancel/stop any run from any replica). Without it the orchestrator is single-node. |
| `SCENARIO_API_URL` | **http://scenario-service:8000** | Where the orchestrator fetches scenarios. |
| `AUTH_API_URL` | **http://auth-service:8300** | Used by the `/status` aggregator. |
| `PAYPROBE_LOAD_EXTERNAL_WORKERS` | — | `1` disables in-process load-worker fallback (true distributed fleet only). |
| `DISABLE_SCHEDULER` | — | `1` turns off the scheduled-run loop. |

## Observability

See [Observability](./observability.md).

| Variable | Default | Effect |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Enables OTLP tracing export (requires the OTel SDK installed). No-op when unset. |
| `CORS_ORIGINS` | localhost dev origins | Comma-separated browser origins allowed to call the service. |

Every service exposes `/health` (liveness), `/ready` (readiness), and
`/metrics` (Prometheus). The orchestrator adds `/status` (dependency aggregator)
and `/system` (this configuration's runtime posture, read by the portal).

## Secrets handling

Secrets (`AUTH_JWT_SECRET`, `API_TOKEN`, provider API keys) are read from the
environment. For production, inject them from a secret manager (Vault, AWS/GCP
secret manager, k8s Secrets) rather than committing them to a `.env` file. The
compose defaults (`dev-insecure-change-me`, `admin`/`admin`) are for local use
only — the **Settings → System** panel flags when you're still running them.
