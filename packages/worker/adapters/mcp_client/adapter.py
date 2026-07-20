"""McpAdapter — a generic Model Context Protocol *client* connection.

The control plane of ADR-0009 (phase 3). Every major payment provider now
publishes an MCP server (Stripe/PayPal remote, Adyen self-hosted, Square,
Razorpay, Mollie, Paddle, …); this one adapter reaches all of them — and any
future one — with zero per-provider code. PayProbe already *serves* MCP
(``packages/mcp-server``); this is the symmetric half: PayProbe as MCP client.

What it is FOR: **fixture provisioning and provider-side verification as
scenario steps** — "create a test customer before the run", "after the capture
scenario, search the provider's records and assert the payment exists". It is
*not* a payment transport: scenarios drive payments through the ``http`` data
plane (provider packs); MCP servers are rate-limited operations surfaces, and
remote provider MCP connections ship ``external: true`` so the load engine
refuses them (same guardrail as the sandbox connections).

Actions
-------
* ``list_tools`` — the server's tool inventory
  (``{"tools": [{name, description, input_schema}], "count": N}``).
* ``call_tool`` — explicit form: payload ``{"name": ..., "arguments": {...}}``.
* **any other action name** — convenience form: the action *is* the tool name
  and the payload *is* its arguments (mirrors the http adapter's bare-action
  fallback), so a step can say ``action: "create_customer"`` directly.

A tool reply lands in ``response_payload`` as ``{"tool", "is_error",
"content": [...], "structured": {...}, "text": "..."}`` — ``text`` joins the
text blocks so simple assertions don't need indices, while ``content[0].text``
/ ``structured.*`` stay reachable via the bracket-index assertion syntax.
A tool-level error (``isError``) fails the step; that is the point of a
verification step.

Config (worker-shaped)::

    {
      "adapter": "mcp",
      "transport": "http",                   # "http" (streamable) | "stdio"
      "base_url": "https://mcp.stripe.com",  # http transport
      "authentication": {"type": "bearer", "token": "..."},   # or {"type": "header", ...}
      "headers": {"X-Extra": "..."},
      "command": "npx",                      # stdio transport
      "args": ["-y", "@stripe/mcp", "--api-key=..."],
      "env": {"KEY": "..."},
      "request_timeout_sec": 30
    }

Sessions are **per call** (open transport → initialize → operate → close), not
held across calls: the ``mcp`` SDK's transports are anyio task groups bound to
the task that entered them, and the engine may execute steps from different
tasks — holding a session across calls trips cancel-scope task affinity. For a
control-plane adapter the extra handshake per step is the honest trade.
``mcp`` is an optional dependency (grpcio/aiohttp/nats-py precedent): this
module imports cleanly without it; the SDK loads at session time.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from ..base.base_adapter import BaseAdapter, StepResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 30.0


class McpAdapter(BaseAdapter):
    async def connect(self) -> None:
        cfg = self.config
        self.transport = str(
            cfg.get("transport") or ("stdio" if cfg.get("command") else "http")
        ).lower()
        self.base_url = str(cfg.get("base_url") or cfg.get("url") or "").strip()
        self.command = str(cfg.get("command") or "")
        self.args = [str(a) for a in (cfg.get("args") or [])]
        self.env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
        try:
            self.timeout = float(cfg.get("request_timeout_sec") or DEFAULT_TIMEOUT_SEC)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT_SEC

        self.headers: dict[str, str] = {
            str(k): str(v) for k, v in (cfg.get("headers") or {}).items()
        }
        auth = cfg.get("authentication") or {}
        kind = auth.get("type")
        if kind == "bearer" and auth.get("token"):
            self.headers["Authorization"] = f"Bearer {auth['token']}"
        elif kind == "header" and auth.get("headerName"):
            self.headers[str(auth["headerName"])] = str(auth.get("headerValue", ""))

        if self.transport == "http":
            if not self.base_url.lower().startswith(("http://", "https://")):
                raise ValueError(
                    "mcp connection (http transport) needs a full base_url "
                    "starting with http:// or https://"
                )
        elif self.transport == "stdio":
            if not self.command:
                raise ValueError("mcp connection (stdio transport) needs a 'command'")
        else:
            raise ValueError(
                f"unknown mcp transport {self.transport!r} — use 'http' or 'stdio'"
            )

    # -- session (per call — see module docstring) -----------------------------

    @asynccontextmanager
    async def _session(self):
        from mcp import ClientSession  # optional dep: loaded at session time

        if self.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self.command, args=self.args, env=self.env or None
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                self.base_url, headers=self.headers or None, timeout=self.timeout
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    # -- execution -------------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        payload = payload if isinstance(payload, dict) else {}
        # resolve the tool BEFORE opening a session: a config error should
        # surface as itself, not wrapped in the transport's ExceptionGroup
        name = arguments = None
        if action == "call_tool":
            name = str(payload.get("name") or "")
            arguments = payload.get("arguments") or {}
            if not name:
                return StepResult(
                    success=False,
                    request_payload={"action": action, "payload": payload},
                    response_payload={},
                    duration_ms=0,
                    error="call_tool needs payload {'name': ..., 'arguments': {...}}",
                )
        elif action != "list_tools":
            # convenience: the action IS the tool name
            name, arguments = action, payload
        try:
            async with self._session() as session:
                if action == "list_tools":
                    body = await self._list_tools(session)
                    success = True
                else:
                    body = await self._call_tool(session, name, arguments)
                    success = not body["is_error"]
        except Exception as exc:  # noqa: BLE001 — the failure IS the result
            exc = self._unwrap(exc)
            return StepResult(
                success=False,
                request_payload={"action": action, "payload": payload},
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        return StepResult(
            success=success,
            request_payload={"action": action, "payload": payload},
            response_payload=body,
            duration_ms=int((time.monotonic() - start) * 1000),
            error=None if success else self._tool_error_text(body),
        )

    async def _list_tools(self, session) -> dict:
        res = await session.list_tools()
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema,
            }
            for t in res.tools
        ]
        return {"tools": tools, "count": len(tools)}

    async def _call_tool(self, session, name: str, arguments: dict) -> dict:
        res = await session.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=timedelta(seconds=self.timeout),
        )
        content: list[dict] = []
        texts: list[str] = []
        for block in res.content or []:
            btype = getattr(block, "type", "unknown")
            if btype == "text":
                content.append({"type": "text", "text": block.text})
                texts.append(block.text)
            else:  # image/audio/resource blocks: keep type + a safe dump
                try:
                    content.append(block.model_dump(mode="json"))
                except Exception:  # noqa: BLE001
                    content.append({"type": btype})
        return {
            "tool": name,
            "is_error": bool(res.isError),
            "content": content,
            "structured": getattr(res, "structuredContent", None),
            "text": "\n".join(texts),
        }

    @staticmethod
    def _tool_error_text(body: dict) -> str:
        text = (body.get("text") or "").strip()
        return f"tool error: {text}" if text else "tool returned isError"

    @staticmethod
    def _unwrap(exc: BaseException) -> BaseException:
        """Dig the first real exception out of anyio's ExceptionGroup nesting —
        'unhandled errors in a TaskGroup' tells the operator nothing."""
        seen = 0
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions and seen < 10:
            exc = exc.exceptions[0]
            seen += 1
        return exc

    # -- health ----------------------------------------------------------------

    async def health_check(self) -> bool:
        """Initialize + list_tools. Never raises — unreachable/unauth is False."""
        try:
            async with self._session() as session:
                await session.list_tools()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def disconnect(self) -> None:
        return  # sessions are per call; nothing is held open
