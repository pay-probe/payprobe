# Run Orchestrator — REST + WebSocket run-stream transport.
#
# IMPORTANT: build with the repository root as context so the local `worker`
# package (the engine) can be copied in alongside the orchestrator:
#   docker build -f packages/orchestrator/Dockerfile -t payprobe/orchestrator .
# The compose files already set `context: ../..` for this service.

FROM python:3.12-slim

WORKDIR /app

# Third-party deps only; the two local packages are imported via PYTHONPATH.
# (redis is needed when REDIS_URL is set -> RedisStreamBackbone.)
RUN pip install --no-cache-dir \
    "fastapi>=0.110" "uvicorn[standard]>=0.27" "redis[hiredis]>=5.0" \
    "pydantic>=2.0" "httpx>=0.27" "pycryptodome>=3.20" "PyJWT>=2.8" \
    "cryptography>=42.0" \
    "aiohttp>=3.9" \
    "nats-py>=2.6"  # HttpResponder + NATS responders/participants (ADR-0006; worker runs in-process)
# cryptography powers SecretBox (sign-off snapshot encryption-at-rest) when
# PAYPROBE_SECRET_KEY is set; harmless no-op import otherwise.

# Node.js + esbuild power the TypeScript path of `code` nodes. The engine runs
# in-process inside the orchestrator, so the JS runtime must live here. Python
# `code` nodes need nothing extra. esbuild is installed into a fixed prefix so
# NODE_PATH resolves it deterministically; npm is removed afterwards to slim the
# image (node itself stays).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && mkdir -p /opt/jsdeps \
    && cd /opt/jsdeps && npm init -y >/dev/null 2>&1 \
    && npm install --no-audit --no-fund esbuild \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm
ENV NODE_PATH=/opt/jsdeps/node_modules

# Docker CLI (client only, no daemon) so the worker provisioner can spawn
# load-worker containers on the host's docker socket when
# PAYPROBE_WORKER_PROVISIONER=docker|compose. Installed from Docker's apt repo so
# it resolves the right architecture (amd64/arm64). docker-compose-plugin is
# included so the `compose` backend works too; the `docker` backend needs only
# docker-ce-cli.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Local source: the engine (worker) + the orchestrator, plus example configs
# the orchestrator loads when a run request omits an explicit environment.
COPY packages/worker /app/packages/worker
COPY packages/report_service /app/packages/report_service
COPY packages/payprobe_common /app/packages/payprobe_common
COPY packages/orchestrator /app/packages/orchestrator
COPY examples /app/examples

ENV PYTHONPATH=/app/packages \
    PAYPROBE_EXAMPLES_DIR=/app/examples \
    PYTHONUNBUFFERED=1

EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/health')"

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "8100"]
