"""Generic MCP client adapter — driven against a real in-process MCP server
(ADR-0009 phase 3).

The test double is the same FastMCP library the platform's own mcp-server is
built on, served over **streamable HTTP** (uvicorn, ephemeral port) and over
**stdio** (a spawned subprocess) — both transports the adapter supports.
Validates: tool inventory, explicit ``call_tool`` and the named-action
convenience, structured + text content surfacing, tool errors failing the
step, unknown tools, the auth header shaping, health semantics, and config
validation. ``mcp`` is an optional dependency — absence skips, not fails
(grpcio/aiohttp/nats-py precedent). The opt-in live check against
``mcp.stripe.com`` is gated on ``STRIPE_TEST_KEY``.
"""

import asyncio
import os
import sys
import textwrap

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed (optional dep)")

from worker.adapters.mcp_client.adapter import McpAdapter  # noqa: E402


def _make_server():
    from pydantic import BaseModel

    from mcp.server.fastmcp import FastMCP

    srv = FastMCP("payprobe-test")

    @srv.tool()
    def echo(text: str) -> str:
        """Echo the input back."""
        return "echo:" + text

    class AddResult(BaseModel):
        sum: int

    @srv.tool()
    def add(a: int, b: int) -> AddResult:
        return AddResult(sum=a + b)

    @srv.tool()
    def boom() -> str:
        raise RuntimeError("kaboom")

    return srv


@pytest.fixture()
async def http_base():
    """A FastMCP server over streamable HTTP on an ephemeral port."""
    uvicorn = pytest.importorskip("uvicorn")

    app = _make_server().streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # keep pytest's signals
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def _adapter(base, **extra):
    a = McpAdapter({"adapter": "mcp", "base_url": base, **extra})
    await a.connect()
    return a


# -- inventory + calls over streamable HTTP ------------------------------------


async def test_list_tools(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("list_tools", {})
    assert sr.success
    body = sr.response_payload
    assert body["count"] == 3
    names = {t["name"] for t in body["tools"]}
    assert names == {"echo", "add", "boom"}
    echo = next(t for t in body["tools"] if t["name"] == "echo")
    assert "Echo" in echo["description"]
    assert echo["input_schema"]["properties"]["text"]["type"] == "string"


async def test_call_tool_explicit_form(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("call_tool", {"name": "echo", "arguments": {"text": "hi"}})
    assert sr.success
    body = sr.response_payload
    assert body["tool"] == "echo"
    assert body["is_error"] is False
    assert body["text"] == "echo:hi"
    # bracket-index assertion syntax reaches the raw blocks too
    assert a._extract_field({"body": body}, "body.content[0].text") == "echo:hi"


async def test_named_action_convenience_and_structured_content(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("add", {"a": 2, "b": 3})
    assert sr.success
    assert sr.response_payload["structured"] == {"sum": 5}


async def test_tool_error_fails_the_step(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("boom", {})
    assert not sr.success
    assert sr.response_payload["is_error"] is True
    assert "kaboom" in (sr.error or "")


async def test_unknown_tool_fails_loudly(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("does_not_exist", {})
    assert not sr.success
    assert "Unknown tool" in (sr.error or "")


async def test_call_tool_requires_a_name(http_base):
    a = await _adapter(http_base)
    sr = await a.execute("call_tool", {"arguments": {"x": 1}})
    assert not sr.success
    assert "name" in (sr.error or "")


async def test_health_check_up_and_down(http_base):
    a = await _adapter(http_base)
    assert await a.health_check() is True
    dead = await _adapter("http://127.0.0.1:1")
    assert await dead.health_check() is False


# -- auth + config shaping -----------------------------------------------------


async def test_bearer_and_header_auth_shape_headers():
    a = McpAdapter(
        {
            "adapter": "mcp",
            "base_url": "http://x",
            "authentication": {"type": "bearer", "token": "sk_test_1"},
        }
    )
    await a.connect()
    assert a.headers["Authorization"] == "Bearer sk_test_1"

    b = McpAdapter(
        {
            "adapter": "mcp",
            "base_url": "http://x",
            "authentication": {"type": "header", "headerName": "X-API-Key", "headerValue": "k1"},
            "headers": {"X-Extra": "1"},
        }
    )
    await b.connect()
    assert b.headers["X-API-Key"] == "k1"
    assert b.headers["X-Extra"] == "1"


async def test_config_validation():
    with pytest.raises(ValueError, match="base_url"):
        await McpAdapter({"adapter": "mcp"}).connect()
    with pytest.raises(ValueError, match="command"):
        await McpAdapter({"adapter": "mcp", "transport": "stdio"}).connect()
    with pytest.raises(ValueError, match="transport"):
        await McpAdapter(
            {"adapter": "mcp", "transport": "carrier-pigeon", "base_url": "http://x"}
        ).connect()


# -- stdio transport (self-hosted provider servers' shape) ---------------------


async def test_stdio_transport_end_to_end(tmp_path):
    script = tmp_path / "stdio_server.py"
    script.write_text(textwrap.dedent("""
        from mcp.server.fastmcp import FastMCP

        srv = FastMCP("payprobe-stdio-test")

        @srv.tool()
        def echo(text: str) -> str:
            return "echo:" + text

        srv.run("stdio")
        """))
    a = McpAdapter(
        {"adapter": "mcp", "transport": "stdio", "command": sys.executable, "args": [str(script)]}
    )
    await a.connect()
    sr = await a.execute("echo", {"text": "over-stdio"})
    assert sr.success, sr.error
    assert sr.response_payload["text"] == "echo:over-stdio"
    assert await a.health_check() is True


# -- opt-in live check (the only sanctioned real-MCP traffic) ------------------


@pytest.mark.skipif(
    not os.environ.get("STRIPE_TEST_KEY", "").startswith("sk_test_"),
    reason="STRIPE_TEST_KEY not set (opt-in live check against mcp.stripe.com)",
)
async def test_live_stripe_mcp_list_tools():
    a = McpAdapter(
        {
            "adapter": "mcp",
            "base_url": "https://mcp.stripe.com",
            "authentication": {"type": "bearer", "token": os.environ["STRIPE_TEST_KEY"]},
        }
    )
    await a.connect()
    sr = await a.execute("list_tools", {})
    assert sr.success, sr.error
    assert sr.response_payload["count"] > 0
