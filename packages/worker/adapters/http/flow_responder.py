"""HttpFlowResponder — a listening HTTP responder whose reply is computed by a
participant-flow graph.

The REST counterpart of :class:`~worker.adapters.tcp.flow_responder.FlowResponder`.
It is an :class:`~worker.adapters.http.responder.HttpResponder` (so it reuses the
aiohttp server, request decode, metrics and chaos) but swaps rule-matching for a
**graph walk**: each inbound HTTP request is run through a participant flow and
the reply node's resolved payload becomes the HTTP response action — the same
shape an HttpResponder rule's ``respond`` uses::

    { "status": 200, "json": { ... }, "headers": { ... }, "drop"?, "delay_ms"? }

So a flow triggered by ``POST /payments`` can branch on the body, call a
downstream connection (switch / issuer / HSM), and shape a JSON reply from the
result. The flow runtime + correlated trace come from
:class:`~worker.adapters.flow_mixin.FlowTraceMixin`; this class overrides only the
HTTP-specific trace hooks (method/path/status instead of MTI/DE 39).
"""

from __future__ import annotations

import uuid
from typing import Any

from .responder import HttpResponder
from ..flow_mixin import FlowTraceMixin


class HttpFlowResponder(FlowTraceMixin, HttpResponder):
    TRACE_KIND = "http"

    def __init__(
        self,
        config: dict,
        flow: dict,
        *,
        downstream: dict | None = None,
        sink=None,
        run_id: str = "flow",
    ) -> None:
        super().__init__(config)  # HttpResponder.__init__ — sets self.config etc.
        self._init_flow(flow, downstream=downstream, sink=sink, run_id=run_id)

    # -- HTTP-shaped correlation / trace hooks ------------------------------

    def _correlation_field(self) -> str:
        """Header (or query/body key) carrying the correlation id. Defaults to
        ``x-correlation-id``; override via ``config.correlation.field``."""
        return str((self.config.get("correlation") or {}).get("field", "x-correlation-id"))

    def _correlation_id(self, parsed: dict) -> str:
        field = self._correlation_field().lower()
        headers = parsed.get("headers") or {}
        query = parsed.get("query") or {}
        body = parsed.get("body") if isinstance(parsed.get("body"), dict) else {}
        val = headers.get(field) or query.get(field) or (body or {}).get(field)
        return str(val) if val else uuid.uuid4().hex[:12]

    def _inject_correlation(self, payload: Any, cid: str) -> None:
        """Best-effort propagation to a downstream call. We can't know the
        downstream's adapter here, so stamp both an HTTP header and an ISO DE 11
        — whichever the next listener reads, it sees the same id."""
        if not isinstance(payload, dict):
            return
        field = self._correlation_field()
        headers = payload.get("headers")
        if headers is None:
            headers = payload["headers"] = {}
        if isinstance(headers, dict):
            headers.setdefault(field, cid)
        de = payload.get("de")
        if isinstance(de, dict):
            de.setdefault("11", cid)

    def _trace_request(self, parsed: dict) -> dict:
        return {
            "method": parsed.get("method"),
            "path": parsed.get("path"),
            "query": parsed.get("query") or {},
            "body": parsed.get("body"),
        }

    def _trace_label(self, parsed: dict) -> str:
        return f"{parsed.get('method', '?')} {parsed.get('path', '?')}"

    # _trace_response_summary -> HttpResponder._response_code (HTTP status). ✓

    async def stop(self) -> None:
        await super().stop()
        try:
            await self._registry.disconnect_all()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
