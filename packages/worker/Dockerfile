# Worker engine — packaged as a CLI image.
#
# The orchestrator runs the engine in-process for live runs, so this image is
# not a long-running service. It exists for batch/CI execution and smoke tests:
#   docker compose run --rm worker            # runs bundled example scenarios
#   docker compose run --rm worker /app/examples/environments/mock.json ...
#
# Build with the repository root as context (compose sets context: ../..):
#   docker build -f packages/worker/Dockerfile -t payprobe/worker .

FROM python:3.12-slim

# Apply Debian security updates so base-layer CVEs (e.g. perl-base
# CVE-2026-8376 / CVE-2026-42496 / CVE-2026-13221, fixed in
# 5.40.1-6+deb13u1) don't ship in the image.
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uvloop for the event loop; httpx powers `http` nodes; pycryptodome powers
# `crypto` nodes (PIN block, MAC, CVV, ARQC, DES/3DES); redis is required for a
# standalone load worker — it joins a run over the Redis bus (REDIS_URL), and
# without it `python -m worker.load_worker` crashes on `import redis.asyncio`.
# nats-py lets a standalone load worker drive a NATS target (ADR-0006);
# imported lazily by the adapter, so it is harmless when unused.
# A slow PyPI read (the whole stack builds its images at once) must not fail
# the image: wait longer per read and retry the download.
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN pip install --no-cache-dir uvloop "httpx>=0.27" "pycryptodome>=3.20" "redis>=5" \
    "nats-py>=2.6"

# Node.js + esbuild for the TypeScript path of `code` nodes (batch/CLI runs).
# Python `code` nodes need nothing extra.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && mkdir -p /opt/jsdeps \
    && cd /opt/jsdeps && npm init -y >/dev/null 2>&1 \
    && npm install --no-audit --no-fund esbuild \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm
ENV NODE_PATH=/opt/jsdeps/node_modules

COPY packages/worker /app/packages/worker
COPY examples /app/examples

ENV PYTHONPATH=/app/packages \
    PYTHONUNBUFFERED=1

# Default: execute the bundled mock env + example scenarios and print a summary.
ENTRYPOINT ["python", "-m", "worker"]
CMD []
