# Build context: packages/ (NOT this directory) — the shared payprobe_common
# package (agent toolkit) must be copied alongside. Compose passes
# `context: ../../packages, dockerfile: payprobe-assistant/Dockerfile`.
FROM python:3.12-slim

WORKDIR /app

COPY payprobe-assistant/pyproject.toml payprobe-assistant/README.md ./
COPY payprobe-assistant/assistant_service ./assistant_service
COPY payprobe_common ./payprobe_common

# Install the local shared package first (its own step) so pip never backtracks
# on it and drags the whole graph — pydantic/pydantic-core included — into a
# fragile joint re-resolution. Then the service, then redis.
RUN pip install --no-cache-dir ./payprobe_common \
 && pip install --no-cache-dir . \
 && pip install --no-cache-dir "redis>=5.0"

ENV SCENARIO_API_URL=http://scenario-service:8000 \
    RUN_API_URL=http://orchestrator:8100 \
    ASSIST_LLM_PROVIDER=openai \
    ASSIST_HOST=0.0.0.0 \
    ASSIST_PORT=8400

EXPOSE 8400

CMD ["sh", "-c", "uvicorn assistant_service.main:app --host ${ASSIST_HOST} --port ${ASSIST_PORT}"]
