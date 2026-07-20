"""FlowResponder — a listening responder whose reply is computed by a flow graph.

The TCP participant-flow runtime. It is a :class:`~.responder.TcpResponder` (so it
reuses the exact framing, ISO 8583 / header-echo decode+encode, metrics and chaos
machinery) but swaps rule-matching for a **graph walk**: each decoded request is
run through a participant flow (``trigger`` → logic → ``reply``) and the reply
node's resolved payload becomes the response *action*.

The reply node's resolved payload IS a responder action — the same shape a rule's
``respond`` uses: ``{ "set": {de: val}, "echo": [de…], "generate": {…}, "mti"?,
"drop"? }``. So a flow can set fields, echo request fields, generate an RRN, pick
the response MTI, or drop. If the flow produces no reply, the configured
``default`` action applies.

**Outbound calls:** a flow's ``action`` nodes dispatch to *downstream*
connections (e.g. a switch in front of an issuer). Those connections are supplied
as ``downstream`` (worker-shaped adapter configs keyed by name) and resolved
through an :class:`~worker.adapters.registry.AdapterRegistry`; the response is
exposed to later nodes as ``${<node_id>.response.*}`` so the reply can map it back.

The flow runtime + correlated trace live in
:class:`~worker.adapters.flow_mixin.FlowTraceMixin`, shared with the HTTP
participant responder; this class only binds it to the TCP wire.
"""

from __future__ import annotations

from .responder import TcpResponder
from ..flow_mixin import FlowTraceMixin


class FlowResponder(FlowTraceMixin, TcpResponder):
    TRACE_KIND = "tcp"

    def __init__(
        self,
        config: dict,
        flow: dict,
        *,
        downstream: dict | None = None,
        sink=None,
        run_id: str = "flow",
    ) -> None:
        super().__init__(config)  # TcpResponder.__init__ — sets self.config etc.
        self._init_flow(flow, downstream=downstream, sink=sink, run_id=run_id)

    async def stop(self) -> None:
        await super().stop()
        try:
            await self._registry.disconnect_all()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass

    def _request_view(self, parsed: dict) -> dict:  # pragma: no cover - hook
        """Reserved: lets a subclass reshape the decoded request before the flow
        sees it. The base passes the decode dict as-is, so a flow references
        ``${request.mti}`` / ``${request.de.<n>}``."""
        return parsed
