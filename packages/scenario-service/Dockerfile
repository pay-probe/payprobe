# Build context: packages/ (NOT this directory) — the shared payprobe_common
# package (agent toolkit, crypto) must be copied alongside. Compose passes
# `context: ../../packages, dockerfile: scenario-service/Dockerfile`.
FROM python:3.12-slim

WORKDIR /app

COPY scenario-service/pyproject.toml scenario-service/README.md ./
COPY scenario-service/api ./api
COPY scenario-service/models ./models
COPY payprobe_common ./payprobe_common

# Install the local shared package FIRST, in its own step, then the service.
# Installing both source trees in ONE pip command makes pip resolve them jointly;
# if it ever backtracks on payprobe-common it re-resolves the whole graph
# (pydantic/pydantic-core included), which can surface confusing
# "ResolutionImpossible" errors under a transient index state. Installing
# payprobe-common first means the service's `payprobe-common` dep is already
# satisfied and never re-resolved.
RUN pip install --no-cache-dir ./payprobe_common \
 && pip install --no-cache-dir .

# Example scenarios for first-start seeding are mounted at runtime:
#   -v ./examples/scenarios:/seed:ro  +  SCENARIO_SEED_DIR=/seed
ENV SCENARIO_SEED_DIR=/seed

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
