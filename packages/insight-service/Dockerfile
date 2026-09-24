# Build context: packages/ (NOT this directory) — the shared report_service
# library (failure taxonomy) must be copied alongside. Compose passes
# `context: ../../packages, dockerfile: insight-service/Dockerfile`.
FROM python:3.12-slim

# Apply Debian security updates so base-layer CVEs (e.g. perl-base
# CVE-2026-8376 / CVE-2026-42496 / CVE-2026-13221, fixed in
# 5.40.1-6+deb13u1) don't ship in the image.
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY insight-service/pyproject.toml insight-service/README.md ./
COPY insight-service/insight_service ./insight_service
COPY report_service ./report_service

# The learned layer ships in the image (it's quarantined HERE, not in the
# orchestrator/worker images — ADR-0005). The service still degrades to the
# heuristic + frequency baselines if the import ever fails.
# A slow PyPI read (the whole stack builds its images at once) must not fail
# the image: wait longer per read and retry the download.
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN pip install --no-cache-dir ./report_service ".[ml]"

ENV RUN_API_URL=http://orchestrator:8100 \
    INSIGHT_HOST=0.0.0.0 \
    INSIGHT_PORT=8500

EXPOSE 8500

CMD ["sh", "-c", "uvicorn insight_service.main:app --host ${INSIGHT_HOST} --port ${INSIGHT_PORT}"]
