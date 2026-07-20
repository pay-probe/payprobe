"""PayProbe MCP server.

Registers the operations in :mod:`mcp_server.tools` as MCP tools so an
MCP-capable agent (Claude Desktop, etc.) can discover the step catalog and
author/run PayProbe scenarios. Run over stdio::

    SCENARIO_API_URL=http://localhost:8000 RUN_API_URL=http://localhost:8001 \
        python -m mcp_server

The ``mcp`` SDK is an optional dependency; importing this module without it
still works (``build_server`` raises a clear error only when actually called).
"""
from __future__ import annotations

import json

from . import prompts, registry, tools


def build_server():
    """Build the FastMCP server with all PayProbe tools, prompts and resources
    registered. Tools carry behaviour annotations (read-only / destructive /
    idempotent / open-world) and human-friendly titles from
    :mod:`mcp_server.registry`."""
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised only without the SDK
        raise RuntimeError(
            "the 'mcp' package is required to run the server — "
            "install with: pip install 'mcp[cli]'") from exc

    mcp = FastMCP("payprobe")

    # Health route for the HTTP transports. The Streamable HTTP endpoint at
    # /mcp only answers POST (and negotiated GET/DELETE), so a plain GET/HEAD
    # health probe gets a 405 — reachable, but ugly to surface. /healthz gives
    # monitors a clean 200 without going through the MCP handshake.
    @mcp.custom_route("/healthz", methods=["GET"])
    async def _healthz(_request):  # pragma: no cover - thin liveness route
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok", "service": "payprobe-mcp"})

    # -- tools: registered from the metadata table so titles + behaviour hints
    # travel with every tool. Clients use the hints to decide which calls need
    # confirmation (destructive) and which are safe to retry (idempotent).
    for name, title, hints in registry.TOOL_SPECS:
        fn = getattr(tools, name)
        annotations = ToolAnnotations(title=title, **hints)
        mcp.tool(title=title, annotations=annotations)(fn)

    # -- prompts: reusable domain workflows a client can offer as templates.
    for fn, title, description in prompts.PROMPT_SPECS:
        mcp.prompt(title=title, description=description)(fn)

    # -- resources: read-only views over the live store, addressable by URI.
    # Each resource wraps a read tool and returns JSON; a single ``{id}``
    # placeholder in the URI maps to the tool's one argument.
    for uri, tool_name, title, description in registry.RESOURCE_SPECS:
        _register_resource(mcp, uri, getattr(tools, tool_name), title, description)

    return mcp


def _register_resource(mcp, uri, tool_fn, title, description):
    """Wrap a read tool as a JSON MCP resource at ``uri``.

    FastMCP matches the wrapper's parameter *names* to the ``{placeholders}``
    in the URI, so for a templated URI we synthesise a signature whose single
    parameter is named after the placeholder (e.g. ``scenario_id``).
    """
    import inspect
    import re

    match = re.search(r"\{(\w+)\}", uri)
    if match:  # templated: single id placeholder -> single tool arg
        pname = match.group(1)

        def _resource(**kwargs):
            return json.dumps(tool_fn(kwargs[pname]), default=str)

        _resource.__signature__ = inspect.Signature(
            [inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               annotation=str)])
        _resource.__annotations__ = {pname: str, "return": str}
    else:
        def _resource():
            return json.dumps(tool_fn(), default=str)

        _resource.__annotations__ = {"return": str}

    mcp.resource(uri, name=title, title=title, description=description,
                 mime_type="application/json")(_resource)


def _configure_transport_security(server, host: str) -> None:  # pragma: no cover
    """Allow the deployment's own hostnames through the SDK's DNS-rebinding guard.

    The MCP SDK ships with DNS-rebinding protection that only accepts requests
    whose ``Host`` header is a localhost variant. That rejects perfectly valid
    server-to-server calls — e.g. the orchestrator's health probe reaching this
    container as ``mcp-server:8200`` gets a 421 Misdirected Request. We widen the
    allow-list to include the compose service name and the configured bind host,
    plus anything in ``MCP_ALLOWED_HOSTS`` / ``MCP_ALLOWED_ORIGINS`` (comma-
    separated; ``host:*`` matches any port). Set ``MCP_DNS_REBIND_PROTECTION=off``
    to disable the guard entirely on a fully trusted network.
    """
    import os

    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:  # noqa: BLE001 — older SDK without the guard; nothing to do
        return

    if os.environ.get("MCP_DNS_REBIND_PROTECTION", "").lower() in ("0", "off", "false", "no"):
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        return

    hosts = ["localhost:*", "127.0.0.1:*", "[::1]:*", "mcp-server:*"]
    origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*", "http://mcp-server:*"]
    if host and host not in ("0.0.0.0", "::"):
        hosts.append(f"{host}:*")
        origins.append(f"http://{host}:*")
    for extra in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        if extra.strip():
            hosts.append(extra.strip())
    for extra in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(","):
        if extra.strip():
            origins.append(extra.strip())

    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def main() -> None:  # pragma: no cover - entry point
    """Run the server. Transport via MCP_TRANSPORT:

    - ``stdio`` (default) — launched as a subprocess by an MCP client.
    - ``sse`` / ``streamable-http`` — long-lived HTTP service (for Docker);
      bind with MCP_HOST / MCP_PORT (default 0.0.0.0:8200).
    """
    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    server = build_server()
    if transport in ("sse", "streamable-http"):
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        try:
            server.settings.host = host
            server.settings.port = int(os.environ.get("MCP_PORT", "8200"))
        except Exception:  # noqa: BLE001 - older SDKs read host/port differently
            pass
        _configure_transport_security(server, host)
        server.run(transport=transport)
    else:
        server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
