# PayProbe — Docker Deployment

Run the whole platform in Docker. The default mode is **mock** — in-memory
adapters, no payment hardware or secrets required.

## Quick start (mock)

```bash
docker compose -f infra/docker/docker-compose.yml up --build
```

Then open **http://localhost:8080**, go to the Run Monitor, and click
*Start mock run*. You'll see phases gate and steps stream in live over the
WebSocket (events are durable in Redis, so a refresh/reconnect resumes).

## What comes up

| Service | Image | Port | Role |
|---|---|---|---|
| portal | `packages/portal` (nginx) | 8080 | Angular UI + reverse proxy |
| scenario-service | `packages/scenario-service` | 8000 | scenario CRUD (Postgres) |
| orchestrator | `packages/orchestrator` | 8100 | run lifecycle + WebSocket stream |
| postgres | `postgres:15` | — | persistent store |
| redis | `redis:7` | — | run-event stream backbone |

The orchestrator runs the worker **engine in-process**, so there is no separate
long-running worker service. Routing through nginx:
`/api/scenarios/*` → scenario-service, `/api/orch/*` → orchestrator (REST + `ws`).

## Run from published images (no build)

`docker-compose.hub.yml` runs the full platform straight from the prebuilt
`datikos/payprobe-*` images on Docker Hub — no local build step:

```bash
docker compose -f infra/docker/docker-compose.hub.yml up -d   # or: make up-hub
```

Open **http://localhost:8080**. Stop with `make down-hub`.

Pin a version or point at another namespace via env:

```bash
PAYPROBE_TAG=v0.1.0 docker compose -f infra/docker/docker-compose.hub.yml up -d
PAYPROBE_NS=myorg   docker compose -f infra/docker/docker-compose.hub.yml up -d
```

It mirrors `docker-compose.yml` but replaces every `build:` with the published
`image:`. It still mounts two repo files read-only — the portal's nginx `/api`
routing config and the scenario-service example seeds — so run it from a repo
checkout. Publish/refresh the images with `scripts/publish-images.sh` (or
`make publish`); see `scripts/PUBLISHING.md`.

> Note: the orchestrator's `PAYPROBE_WORKER_IMAGE` defaults to
> `datikos/payprobe-worker:latest` in this file, so dynamically-provisioned load
> workers pull from the Hub rather than expecting a locally-built tag.

## Profiles

```bash
# add placeholder containers for not-yet-built services (auth, report, helpers)
docker compose -f infra/docker/docker-compose.yml --profile full up --build

# run the worker CLI once against the bundled example scenarios
docker compose -f infra/docker/docker-compose.yml --profile tools run --rm worker
```

The `full` profile services are the shared **stub** image (`stub/`): they pass
health checks and return `501` on real endpoints — they exist so the complete
topology can be stood up while those services are still being implemented.

## Configuration

Copy `.env.example` → `.env` and edit. Everything has mock defaults, so an empty
`.env` still works. For production set a real `POSTGRES_PASSWORD`, secrets, and
replace the mock environment with real adapter configs.

## Build contexts (note)

`orchestrator` and `worker` build from the **repository root** (the compose file
sets `context: ../..`) because they include the local `worker` engine package.
`scenario-service` and `portal` build from their own package directories.

## Validate without running

```bash
docker compose -f infra/docker/docker-compose.yml config            # default
docker compose -f infra/docker/docker-compose.yml --profile full config
docker compose -f infra/docker/docker-compose.hub.yml config        # Hub images
```
