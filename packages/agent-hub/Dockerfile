# Build context: packages/ (NOT this directory) — the shared payprobe_common
# package (agent toolkit) must be copied alongside so the registry can validate
# tool allowlists against the one tool layer. Compose passes
# `context: ../../packages, dockerfile: agent-hub/Dockerfile`.
FROM python:3.12-slim

RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY agent-hub/pyproject.toml agent-hub/README.md ./
COPY agent-hub/agent_hub ./agent_hub
COPY payprobe_common ./payprobe_common
# D2: the :8400 assistant is folded in, mounted under /assistant (its session
# and chat stores use REDIS_URL when set, memory otherwise).
COPY payprobe-assistant ./payprobe-assistant

RUN pip install --no-cache-dir ./payprobe_common \
 && pip install --no-cache-dir ./payprobe-assistant \
 && pip install --no-cache-dir "redis>=5.0" \
 && pip install --no-cache-dir .

ENV AGENT_HUB_HOST=0.0.0.0 \
    AGENT_HUB_PORT=8600 \
    AGENT_HUB_DATABASE_URL=postgresql://payprobe:payprobe@postgres:5432/payprobe

# The service holds no files of its own (registry and heartbeats live in
# PostgreSQL, the assistant's stores in Redis or memory), so it runs unprivileged.
RUN useradd --system --uid 10001 --no-create-home agenthub
USER agenthub

EXPOSE 8600

# /health is public (no bearer) and reports the store's schema version, so a
# passing check means the app is up AND its database answered.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('AGENT_HUB_PORT','8600'), timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn agent_hub.main:app --host ${AGENT_HUB_HOST} --port ${AGENT_HUB_PORT}"]
