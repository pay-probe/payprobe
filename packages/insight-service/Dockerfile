# Build context: packages/ (NOT this directory) — the shared report_service
# library (failure taxonomy) must be copied alongside. Compose passes
# `context: ../../packages, dockerfile: insight-service/Dockerfile`.
FROM python:3.12-slim

WORKDIR /app

COPY insight-service/pyproject.toml insight-service/README.md ./
COPY insight-service/insight_service ./insight_service
COPY report_service ./report_service

# The learned layer ships in the image (it's quarantined HERE, not in the
# orchestrator/worker images — ADR-0005). The service still degrades to the
# heuristic + frequency baselines if the import ever fails.
RUN pip install --no-cache-dir ./report_service ".[ml]"

ENV RUN_API_URL=http://orchestrator:8100 \
    INSIGHT_HOST=0.0.0.0 \
    INSIGHT_PORT=8500

EXPOSE 8500

CMD ["sh", "-c", "uvicorn insight_service.main:app --host ${INSIGHT_HOST} --port ${INSIGHT_PORT}"]
