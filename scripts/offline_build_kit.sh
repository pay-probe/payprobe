#!/usr/bin/env bash
# Assemble an offline build kit for the minimal PayProbe stack (single-host
# site deployments): everything an air-gapped Docker host needs to build the four
# service images from infra/docker/offline/ without a registry, PyPI or npm.
#
#   internet machine                          air-gapped host
#   ─────────────────                         ────────────────
#   skopeo  → base image tarballs             docker load
#   pip     → wheels/ (py3.12 manylinux)      pip --no-index
#   ng build→ portal-dist/                    COPY into nginx
#   git     → src/ (packages, examples, infra)
#   cp      → oracle-client/ (Instant Client), debs/ (libaio)
#             ⇓ tar + SHA256SUMS ⇓            build-images.sh
#
# Usage:
#   scripts/offline_build_kit.sh --out /path/kit [--tag 2026-09-19] \
#       [--oracle-client /opt/oracle/instantclient_23_8] [--skip-images] [--skip-npm]
#
# Prerequisites here: skopeo, a python3 with pip, node>=22 + npm, git, tar.
# The Instant Client is optional; without it the orchestrator image cannot open
# thick Oracle sessions (python-oracledb thin mode still works where the
# listener allows it).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT=""
TAG="$(date -u +%Y%m%d)"
ORACLE_CLIENT=""
SKIP_IMAGES=0
SKIP_NPM=0
PY="${PYTHON:-python3}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-https://deb.debian.org/debian}"
BASE_IMAGES=(library/python:3.12-slim library/nginx:alpine library/redis:7-alpine)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --oracle-client) ORACLE_CLIENT="$2"; shift 2 ;;
    --skip-images) SKIP_IMAGES=1; shift ;;
    --skip-npm) SKIP_NPM=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "--out DIR is required" >&2; exit 2; }
mkdir -p "$OUT"/{images,wheels,debs,src,portal-dist,oracle-client}

log() { printf '==> %s\n' "$*"; }

# 1. Base images -------------------------------------------------------------
if [[ $SKIP_IMAGES -eq 0 ]]; then
  for img in "${BASE_IMAGES[@]}"; do
    name="$(basename "${img%%:*}")"; tag="${img##*:}"
    log "skopeo: $img"
    skopeo copy --override-os linux --override-arch amd64 \
      "docker://docker.io/$img" "docker-archive:$OUT/images/$name-$tag.tar:$name:$tag"
  done
fi

# 2. Wheels (union of the three Python services + oracledb) -----------------
log "pip download → wheels/"
cat > "$OUT/wheels/requirements-offline.txt" <<'EOF'
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.0
PyJWT>=2.8
python-multipart>=0.0.9
asyncpg>=0.29
structlog>=23.0
pyyaml>=6.0
cryptography>=42.0
redis[hiredis]>=5.0
httpx>=0.27
pycryptodome>=3.20
aiohttp>=3.9
nats-py>=2.6
uvloop>=0.19
python-pkcs11>=0.7
oracledb>=3.0
setuptools>=70
wheel
EOF
"$PY" -m pip download -q -d "$OUT/wheels" -r "$OUT/wheels/requirements-offline.txt" \
  --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --platform manylinux_2_17_x86_64 --platform any --only-binary=:all:

# 3. libaio for the Instant Client (python:3.12-slim is Debian trixie) -------
log "libaio1t64 (.deb) from $DEBIAN_MIRROR"
pkg_line="$(curl -sSL "$DEBIAN_MIRROR/dists/trixie/main/binary-amd64/Packages.gz" | gunzip \
  | awk '/^Package: libaio1t64$/{f=1} f&&/^Filename:/{print $2; exit}')"
[[ -n "$pkg_line" ]] && curl -sSL -o "$OUT/debs/$(basename "$pkg_line")" "$DEBIAN_MIRROR/$pkg_line"

# 4. Portal (Angular production build) --------------------------------------
if [[ $SKIP_NPM -eq 0 ]]; then
  log "npm ci + ng build (packages/portal)"
  (cd "$REPO_ROOT/packages/portal" && npm ci --no-audit --no-fund && npm run build)
fi
rm -rf "$OUT/portal-dist" && mkdir -p "$OUT/portal-dist"
cp -r "$REPO_ROOT/packages/portal/dist/payprobe-portal/browser/." "$OUT/portal-dist/"

# 5. Sources exactly as committed (git archive of the current HEAD) ---------
log "git archive HEAD → src/"
rm -rf "$OUT/src" && mkdir -p "$OUT/src"
git -C "$REPO_ROOT" archive --format=tar HEAD \
  packages/worker packages/report_service packages/payprobe_common packages/orchestrator \
  packages/scenario-service packages/auth-service examples infra/docker/offline \
  infra/docker/docker-compose.minimal.yml infra/nginx/nginx.minimal.conf \
  | tar -x -C "$OUT/src"
git -C "$REPO_ROOT" rev-parse HEAD > "$OUT/src/GIT_COMMIT"

# 6. Oracle Instant Client (optional) ---------------------------------------
if [[ -n "$ORACLE_CLIENT" ]]; then
  log "Instant Client from $ORACLE_CLIENT"
  # Only the shared libraries the driver loads; no SQL*Plus, no data-pump tools.
  find "$ORACLE_CLIENT" -maxdepth 1 \( -name 'lib*.so*' -o -name '*.so' \) -exec cp -P {} "$OUT/oracle-client/" \;
  cp "$ORACLE_CLIENT"/BASIC_LICENSE "$ORACLE_CLIENT"/BASIC_README "$OUT/oracle-client/" 2>/dev/null || true
else
  : > "$OUT/oracle-client/.no-instant-client"
fi

# 7. Build script executed on the air-gapped host ---------------------------
cat > "$OUT/build-images.sh" <<EOF
#!/usr/bin/env bash
# Load the base images and build the four minimal-stack images. Run from the
# kit root on the target host: ./build-images.sh
set -euo pipefail
cd "\$(dirname "\$0")"
TAG="\${PAYPROBE_TAG:-$TAG}"
for t in images/*.tar; do docker load -i "\$t"; done
export DOCKER_BUILDKIT=1
docker build -f src/infra/docker/offline/Dockerfile.auth-service     -t payprobe-auth-service:"\$TAG"     .
docker build -f src/infra/docker/offline/Dockerfile.scenario-service -t payprobe-scenario-service:"\$TAG" .
docker build -f src/infra/docker/offline/Dockerfile.orchestrator     -t payprobe-orchestrator:"\$TAG"     .
docker build -f src/infra/docker/offline/Dockerfile.portal           -t payprobe-portal:"\$TAG"           .
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E "payprobe-.*:\$TAG|redis:7-alpine"
EOF
chmod +x "$OUT/build-images.sh"
cat > "$OUT/.dockerignore" <<'EOF'
images/
*.tar
*.log
EOF

# 8. Manifest ----------------------------------------------------------------
log "SHA256SUMS"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
printf 'kit=%s\ntag=%s\ncommit=%s\nbuilt=%s\n' "$OUT" "$TAG" "$(cat "$OUT/src/GIT_COMMIT")" "$(date -u +%FT%TZ)" > "$OUT/KIT_INFO"
log "done: $(du -sh "$OUT" | cut -f1) in $OUT"
