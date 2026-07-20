FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mcp_server ./mcp_server

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
