# Shared placeholder image for not-yet-implemented PayProbe services.
# One image, parameterised by SERVICE_NAME, reused by auth/report/helper-*.
FROM python:3.12-slim

# Apply Debian security updates so base-layer CVEs (e.g. perl-base
# CVE-2026-8376 / CVE-2026-42496 / CVE-2026-13221, fixed in
# 5.40.1-6+deb13u1) don't ship in the image.
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27"
COPY app.py .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
