# Offline minimal deployment (single air-gapped host)

How to run the smallest useful PayProbe on one Docker host that has **no
registry, PyPI or npm access** — for example a shared DEV application server
inside a closed network.

## What runs

| Service | Image | Store | Notes |
|---|---|---|---|
| portal | `payprobe-portal` (nginx) | — | TLS terminated here; proxies `/api/*` |
| scenario-service | `payprobe-scenario-service` | SQLite `/data/scenarios.db` | formats, packs, pools, connections |
| orchestrator | `payprobe-orchestrator` | SQLite `/data/runs.db` | worker engine **in-process**; Instant Client for thick Oracle |
| auth-service | `payprobe-auth-service` | SQLite `/data/auth.db` | local accounts, HS256 JWT |
| redis | `redis:7-alpine` | AOF on a volume | durable run-event streams (run monitor resumes) |

Not deployed: Postgres, NATS, insight-service, assistant, mcp-server,
Prometheus/Grafana, the load-worker fleet. The nginx config answers `503` on
`/api/assistant/` and `/api/insights/` so the portal's optional panels fail
fast. `PAYPROBE_WORKER_PROVISIONER=none`: no Docker socket is mounted.

Footprint on a real host: about 1–1.5 GB RAM for the five containers and
about 2 GB of images.

## 1. Build the kit where you have internet

```bash
scripts/offline_build_kit.sh --out /path/payprobe-kit --tag 20260919 \
    --oracle-client /opt/oracle/instantclient_23_8
```

The kit holds base-image tarballs (skopeo), py3.12 manylinux wheels, the
libaio package for Debian trixie, the prebuilt Angular portal, a `git archive`
of the sources, the Instant Client shared libraries, `build-images.sh` and a
`SHA256SUMS` manifest. Transfer it (scp/rsync) and verify the manifest on the
host before building.

## 2. Prepare the host

- Docker Engine + compose plugin from your internal repository. Point the
  daemon's `data-root` at a filesystem with room for the images (a small
  `/var` is common on RHEL hosts).
- Directories: `PAYPROBE_DATA` (volumes), `PAYPROBE_TLS` (`server.crt`,
  `server.key`, readable by root only), `PAYPROBE_TNS_ADMIN`
  (`tnsnames.ora`, optional `sqlnet.ora`).
- Check the intended portal port is free and that the host reaches the
  payment endpoints you will configure.

Record a pre-install snapshot (installed packages, listening ports, enabled
units) so a rollback can prove the host is back to its previous state.

## 3. Build and start

```bash
cd /path/payprobe-kit && sha256sum -c SHA256SUMS --quiet && ./build-images.sh
docker compose -f src/infra/docker/docker-compose.minimal.yml \
    --env-file /opt/payprobe/.env --project-name payprobe up -d
```

Env file keys (all required unless a default is shown):

| Key | Purpose |
|---|---|
| `PAYPROBE_TAG` | image tag from the kit |
| `PAYPROBE_SRC` | kit `src/` path (seed mounts, nginx config) |
| `PAYPROBE_DATA`, `PAYPROBE_TLS`, `PAYPROBE_TNS_ADMIN` | host directories above |
| `PORTAL_TLS_PORT` | published HTTPS port |
| `CORS_ORIGINS` | `https://<host>:<port>` of the portal |
| `AUTH_JWT_SECRET` | long random secret shared by the services |
| `AUTH_ADMIN_USER` / `AUTH_ADMIN_PASSWORD` | bootstrap administrator (first start only) |
| `PAYPROBE_SECRET_KEY` | encrypts connection secrets at rest |
| `PAYPROBE_ENV` | `prod` (default here): fail-closed auth and durable stores |

Then: sign in, install the pack you need, fill its connections, and load a
card pool. Connection passwords are typed into the portal and stored
encrypted; they never live in the kit.

## 4. Remove everything

`docker compose … down -v`, remove the four `payprobe-*` images and the
base images, stop and remove the Docker packages if they were installed for
this, delete the data, TLS and kit directories, and compare the host against
the pre-install snapshot. Keep the snapshot.

## Known limits of the offline images

- No `apt-get upgrade` at build time: refresh the base-image tarball to pick up
  OS fixes.
- No esbuild in the orchestrator image: TypeScript code steps are unavailable
  (Python code steps work). No Docker CLI: no dynamically provisioned load
  workers.
- Thick Oracle sessions need the Instant Client libraries in the kit; without
  them only `driver_mode: thin` works.
