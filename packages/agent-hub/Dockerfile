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

RUN pip install --no-cache-dir ./payprobe_common \
 && pip install --no-cache-dir .

ENV AGENT_HUB_HOST=0.0.0.0 \
    AGENT_HUB_PORT=8600 \
    AGENT_HUB_DATABASE_URL=postgresql://payprobe:payprobe@postgres:5432/payprobe

EXPOSE 8600

CMD ["sh", "-c", "uvicorn agent_hub.main:app --host ${AGENT_HUB_HOST} --port ${AGENT_HUB_PORT}"]
