# Publishing PayProbe images to Docker Hub

`scripts/publish-images.sh` builds every buildable PayProbe service image and
pushes it to Docker Hub under the `datikos` namespace as
`datikos/payprobe-<service>`.

Third-party images (postgres, redis, nats, prometheus, grafana) and the
not-implemented stub placeholders are **not** published — they come straight
from their upstream registries.

## Images published

| Docker Hub repo | Build context | Dockerfile |
|---|---|---|
| `datikos/payprobe-auth-service` | `packages/auth-service` | `packages/auth-service/Dockerfile` |
| `datikos/payprobe-scenario-service` | `packages` | `packages/scenario-service/Dockerfile` |
| `datikos/payprobe-orchestrator` | repo root | `packages/orchestrator/Dockerfile` |
| `datikos/payprobe-mcp-server` | `packages/mcp-server` | `packages/mcp-server/Dockerfile` |
| `datikos/payprobe-assistant` | `packages` | `packages/payprobe-assistant/Dockerfile` |
| `datikos/payprobe-insight-service` | `packages` | `packages/insight-service/Dockerfile` |
| `datikos/payprobe-portal` | `packages/portal` | `packages/portal/Dockerfile` |
| `datikos/payprobe-worker` | repo root | `packages/worker/Dockerfile` |

Each is pushed with two tags: `:latest` (moving) and `:<short-git-sha>`
(immutable). A dirty working tree adds a `-dirty` suffix to the sha tag.

## Recommended: automatic multi-arch publish via GitHub Actions

`.github/workflows/publish-images.yml` builds every image for **both**
`linux/amd64` and `linux/arm64` and pushes on each commit. This is the
preferred path — a laptop `docker build` produces a single-arch image (e.g.
arm64 only on Apple Silicon), which then emulates or fails to run on other
hosts. CI builds a proper multi-arch manifest so one tag runs natively
everywhere.

**Triggers**
- push to `main` → `:latest` + `:<short-sha>`
- push a `v*` tag → `:<version>` (+ `:latest`) as a release
- manual run (Actions tab → *Publish images* → Run workflow)

**One-time setup** — add these under GitHub → Settings → Secrets and variables
→ Actions:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | `datikos` |
| `DOCKERHUB_TOKEN` | a Docker Hub access token with Read/Write scope |

Optionally set repository **variable** `DOCKERHUB_NAMESPACE` to publish under a
namespace other than `datikos`.

After that, a normal `git push` to `main` publishes multi-arch images with no
further action. The local script below remains a manual fallback.

## Manual fallback: local push script

## One-time setup

1. Create a Docker Hub access token: **Account Settings → Security → New Access
   Token** (Read/Write).
2. Log in locally:
   ```bash
   docker login -u datikos
   # paste the access token as the password
   ```
   The repos are created automatically on first push (public by default).

## Publish

```bash
# everything, :latest + :<sha>
scripts/publish-images.sh

# preview the exact commands without running anything
scripts/publish-images.sh --dry-run

# build + tag locally but don't push
scripts/publish-images.sh --no-push

# only specific services
scripts/publish-images.sh orchestrator portal

# also stamp a release tag
EXTRA_TAG=v0.1.0 scripts/publish-images.sh
```

### Multi-arch (Apple Silicon + amd64 servers)

```bash
docker buildx create --use --name payprobe 2>/dev/null || true
PLATFORM=linux/amd64,linux/arm64 scripts/publish-images.sh
```
With `PLATFORM` set, buildx builds and pushes in one step (multi-arch images
can't be loaded into the local daemon, so `--no-push` only warms the cache).

### Non-interactive login (CI or scripted)

```bash
DOCKER_USERNAME=datikos DOCKER_PASSWORD=<token> scripts/publish-images.sh
```

## Deploying from the Hub

The compose file at `infra/docker/docker-compose.yml` still **builds** locally.
To run from the published images instead, either add `image:` keys pointing at
`datikos/payprobe-<svc>:latest` (keeping `build:` for local dev), or maintain a
separate `docker-compose.hub.yml` overlay.

One gotcha: the orchestrator's docker provisioner launches the load worker by
the **local** tag `payprobe-load-worker` (`PAYPROBE_WORKER_IMAGE`). When
deploying from the Hub, set

```
PAYPROBE_WORKER_IMAGE=datikos/payprobe-worker:latest
```

so it pulls the published worker instead of expecting a locally-built tag.
