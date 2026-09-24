# PayProbe Auth Service — JWT issuance + user management.
#
#   docker build -t payprobe/auth-service packages/auth-service
# The compose file builds it with `context: ../../packages/auth-service`.

FROM python:3.12-slim

# Apply Debian security updates so base-layer CVEs (e.g. perl-base
# CVE-2026-8376 / CVE-2026-42496 / CVE-2026-13221, fixed in
# 5.40.1-6+deb13u1) don't ship in the image.
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY api ./api
COPY models ./models

# A slow PyPI read (the whole stack builds its images at once) must not fail
# the image: wait longer per read and retry the download.
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN pip install --no-cache-dir .

# User DB persists on a volume mounted at /data (AUTH_DB=/data/auth.db).
ENV AUTH_DB=/data/auth.db \
    PYTHONUNBUFFERED=1

EXPOSE 8300

HEALTHCHECK --interval=15s --timeout=3s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8300/health')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8300"]
