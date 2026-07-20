# PayProbe — self-contained deployment

Everything needed to run the whole PayProbe platform from prebuilt Docker Hub
images lives in this folder. It does **not** depend on the rest of the repo —
copy `deploy/` anywhere and go.

## Quick start

```bash
docker compose up -d
```

Then open **http://localhost:8080** and start a run. Stop with:

```bash
docker compose down          # keep data volumes
docker compose down -v       # also wipe volumes
```

## What runs

| Service | Image | Port |
|---|---|---|
| portal (nginx UI + `/api` proxy) | `datikos/payprobe-portal` | 8080 |
| scenario-service | `datikos/payprobe-scenario-service` | 8000 |
| orchestrator | `datikos/payprobe-orchestrator` | 8100 |
| auth-service | `datikos/payprobe-auth-service` | 8300 |
| mcp-server | `datikos/payprobe-mcp-server` | 8200 |
| assistant | `datikos/payprobe-assistant` | 8400 |
| insight-service | `datikos/payprobe-insight-service` | 8500 |
| postgres / redis / nats ×3 | upstream images | — |

## Configuration

```bash
cp .env.example .env      # then edit
```

All values have working defaults, so an empty `.env` still works. For anything
beyond local use, change at least `POSTGRES_PASSWORD` and `AUTH_JWT_SECRET`.

Pick images with `PAYPROBE_NS` / `PAYPROBE_TAG` (default `datikos` / `latest`):

```bash
PAYPROBE_TAG=v0.1.0 docker compose up -d
```

## Optional profiles

```bash
# Prometheus + Grafana (Grafana on :3000)
docker compose --profile observability up -d

# distributed load workers (scale manually)
LOAD_RUN_ID=<id> docker compose --profile load up -d --scale load-worker=4 load-worker

# one-shot batch/smoke worker CLI
docker compose --profile tools run --rm worker
```

## What's in this folder

```
docker-compose.yml          the stack (Hub images, all-local mounts)
.env.example                copy to .env
nginx/nginx.conf            portal /api routing (scenario-service + orchestrator)
postgres/init.sql           first-boot DB init
examples/                   scenarios + environments + connections (first-start seeds)
prometheus/                 scrape config + alerting rules (observability profile)
grafana/provisioning/       datasource + dashboards (observability profile)
```

## Notes

- The orchestrator provisions load workers by pulling
  `datikos/payprobe-worker:<tag>` (set as `PAYPROBE_WORKER_IMAGE`), so dynamic
  scaling from the Load page works without a local build.
- Container-spawned workers need the Docker socket, which the orchestrator
  mounts (`/var/run/docker.sock`). On locked-down hosts, set
  `PAYPROBE_WORKER_PROVISIONER=local` in `.env` to run workers in-process
  instead.
- To refresh the published images, run `scripts/publish-images.sh` from a repo
  checkout (see `scripts/PUBLISHING.md`).
