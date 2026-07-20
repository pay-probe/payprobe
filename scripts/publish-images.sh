#!/usr/bin/env bash
#
# publish-images.sh — build and push the PayProbe service images to Docker Hub.
#
# Publishes each buildable PayProbe image under a Docker Hub namespace as
#   <namespace>/payprobe-<service>:<tag>
# with two tags per image: an immutable git-sha tag and a moving :latest.
#
# Third-party images (postgres, redis, nats, prometheus, grafana) and the
# not-implemented stub placeholders are intentionally NOT published.
#
# Usage:
#   scripts/publish-images.sh                 # build + push :latest and :<sha>
#   NAMESPACE=datikos scripts/publish-images.sh
#   scripts/publish-images.sh --dry-run       # print the commands, run nothing
#   scripts/publish-images.sh --no-push       # build + tag locally, don't push
#   scripts/publish-images.sh orchestrator portal   # only these services
#
# Env:
#   NAMESPACE   Docker Hub namespace (user/org). Default: datikos
#   PLATFORM    Target platform for buildx, e.g. linux/amd64,linux/arm64.
#               Unset = build for the host arch with the classic builder.
#   EXTRA_TAG   An extra tag to also apply (e.g. v0.1.0). Optional.
#
# Prerequisites:
#   - Docker running locally.
#   - `docker login` already done (or set DOCKER_USERNAME/DOCKER_PASSWORD and
#     this script will log in for you).
#   - The Docker Hub repos may be auto-created on first push if your account
#     allows it; otherwise create them in the Hub UI first.
#
set -euo pipefail

# --- resolve repo root (this script lives in <repo>/scripts) ----------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
cd "${ROOT}"

NAMESPACE="${NAMESPACE:-datikos}"
PREFIX="payprobe"          # image name prefix -> datikos/payprobe-<svc>
DRY_RUN=0
DO_PUSH=1

# --- service table: name | context | dockerfile (relative to repo root) -----
# Contexts/dockerfiles mirror infra/docker/docker-compose.yml exactly.
SERVICES=(
  "auth-service|packages/auth-service|packages/auth-service/Dockerfile"
  "scenario-service|packages|packages/scenario-service/Dockerfile"
  "orchestrator|.|packages/orchestrator/Dockerfile"
  "mcp-server|packages/mcp-server|packages/mcp-server/Dockerfile"
  "assistant|packages|packages/payprobe-assistant/Dockerfile"
  "insight-service|packages|packages/insight-service/Dockerfile"
  "portal|packages/portal|packages/portal/Dockerfile"
  "worker|.|packages/worker/Dockerfile"
)

# --- parse args -------------------------------------------------------------
WANTED=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) DO_PUSH=0 ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *)  WANTED+=("$arg") ;;
  esac
done

# --- version tags -----------------------------------------------------------
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  SHA="${SHA}-dirty"
fi
TAGS=("latest" "${SHA}")
[[ -n "${EXTRA_TAG:-}" ]] && TAGS+=("${EXTRA_TAG}")

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '  + %s\n' "$*"
  else
    "$@"
  fi
}

echo "==> Publishing PayProbe images"
echo "    namespace : ${NAMESPACE}"
echo "    tags      : ${TAGS[*]}"
echo "    platform  : ${PLATFORM:-<host arch>}"
echo "    push      : $([[ ${DO_PUSH} -eq 1 ]] && echo yes || echo no)"
echo "    dry-run   : $([[ ${DRY_RUN} -eq 1 ]] && echo yes || echo no)"
echo

# --- login (only if creds supplied; otherwise assume already logged in) -----
if [[ ${DO_PUSH} -eq 1 && -n "${DOCKER_USERNAME:-}" && -n "${DOCKER_PASSWORD:-}" ]]; then
  echo "==> docker login as ${DOCKER_USERNAME}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "  + docker login -u ${DOCKER_USERNAME} --password-stdin"
  else
    echo "${DOCKER_PASSWORD}" | docker login -u "${DOCKER_USERNAME}" --password-stdin
  fi
fi

# --- build helper -----------------------------------------------------------
# Uses buildx (with --push) when PLATFORM is set for multi-arch; otherwise the
# classic build + separate push so images are also loaded into the local daemon.
build_and_push() {
  local svc="$1" ctx="$2" dockerfile="$3"
  local repo="${NAMESPACE}/${PREFIX}-${svc}"

  echo "==> ${repo}  (context: ${ctx}, dockerfile: ${dockerfile})"

  local tag_args=()
  for t in "${TAGS[@]}"; do
    tag_args+=("-t" "${repo}:${t}")
  done

  if [[ -n "${PLATFORM:-}" ]]; then
    # multi-arch: buildx builds and pushes in one shot (can't --load multi-arch)
    local push_flag="--push"
    [[ ${DO_PUSH} -eq 0 ]] && push_flag="--output=type=cacheonly"
    run docker buildx build \
      --platform "${PLATFORM}" \
      "${tag_args[@]}" \
      -f "${dockerfile}" \
      "${push_flag}" \
      "${ctx}"
  else
    run docker build "${tag_args[@]}" -f "${dockerfile}" "${ctx}"
    if [[ ${DO_PUSH} -eq 1 ]]; then
      for t in "${TAGS[@]}"; do
        run docker push "${repo}:${t}"
      done
    fi
  fi
  echo
}

# --- iterate ----------------------------------------------------------------
published=()
for entry in "${SERVICES[@]}"; do
  IFS='|' read -r svc ctx dockerfile <<<"${entry}"

  if [[ ${#WANTED[@]} -gt 0 ]]; then
    match=0
    for w in "${WANTED[@]}"; do [[ "$w" == "$svc" ]] && match=1; done
    [[ $match -eq 0 ]] && continue
  fi

  if [[ ! -f "${dockerfile}" ]]; then
    echo "!! skipping ${svc}: ${dockerfile} not found" >&2
    continue
  fi

  build_and_push "${svc}" "${ctx}" "${dockerfile}"
  published+=("${NAMESPACE}/${PREFIX}-${svc}")
done

echo "==> Done. Images:"
for p in "${published[@]}"; do
  echo "    ${p}:${TAGS[0]}  (+ ${SHA})"
done

# Note: the orchestrator's docker provisioner runs the worker image by the LOCAL
# tag payprobe-load-worker (PAYPROBE_WORKER_IMAGE). If you deploy from the Hub,
# set PAYPROBE_WORKER_IMAGE=${NAMESPACE}/${PREFIX}-worker:${TAGS[0]} so it pulls
# the published one instead of expecting a locally-built tag.
