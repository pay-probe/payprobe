FROM python:3.12-slim

# Apply Debian security updates so base-layer CVEs (e.g. perl-base
# CVE-2026-8376 / CVE-2026-42496 / CVE-2026-13221, fixed in
# 5.40.1-6+deb13u1) don't ship in the image.
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mcp_server ./mcp_server

# A slow PyPI read (the whole stack builds its images at once) must not fail
# the image: wait longer per read and retry the download.
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN pip install --no-cache-dir .

# Streamable HTTP transport so the server runs as a long-lived container that
# MCP clients connect to over the network (the current MCP standard; the older
# `sse` transport is deprecated). Override MCP_TRANSPORT=stdio to run it as a
# client-launched subprocess instead.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8200 \
    SCENARIO_API_URL=http://scenario-service:8000 \
    RUN_API_URL=http://orchestrator:8100

EXPOSE 8200

CMD ["python", "-m", "mcp_server"]
