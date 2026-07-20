# PayProbe Auth Service — JWT issuance + user management.
#
#   docker build -t payprobe/auth-service packages/auth-service
# The compose file builds it with `context: ../../packages/auth-service`.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY api ./api
COPY models ./models

RUN pip install --no-cache-dir .

# User DB persists on a volume mounted at /data (AUTH_DB=/data/auth.db).
ENV AUTH_DB=/data/auth.db \
    PYTHONUNBUFFERED=1

EXPOSE 8300

HEALTHCHECK --interval=15s --timeout=3s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8300/health')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8300"]
