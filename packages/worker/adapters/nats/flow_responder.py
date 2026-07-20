"""NatsFlowResponder — a listening NATS responder whose reply is computed by a
participant-flow graph (ADR-0006).

The NATS counterpart of :class:`~worker.adapters.tcp.flow_responder.FlowResponder`
and :class:`~worker.adapters.http.flow_responder.HttpFlowResponder`. It is a
:class:`~worker.adapters.nats.responder.NatsResponder` (so it reuses the broker
subscription, codec decode, metrics and chaos) but swaps rule-matching for a
**graph walk**: each inbound message is run through a participant flow and the
reply node's resolved payload becomes the reply published on the request inbox —
the same ``respond`` shape a NatsResponder rule produces::

    { "json": { ... }, "subject"?: "override.reply.subject", "drop"?, "delay_ms"? }

So a flow triggered by a ``pay.auth`` message can branch on the body, call a
downstream connection (switch / issuer / HSM / another NATS subject), and shape a
reply. The flow runtime + correlated trace come from
:class:`~worker.adapters.flow_mixin.FlowTraceMixin`; this class overrides only the
NATS-specific trace hooks — subject / queue group / inbox instead of MTI / DE 39.
"""

from __future__ import annotations

import uuid
from typing import Any

from .responder import NatsResponder
from ..flow_mixin import FlowTraceMixin


class NatsFlowResponder(FlowTraceMixin, NatsResponder):
    TRACE_KIND = "nats"

    def __init__(
        self,
        config: dict,
        flow: dict,
        *,
        downstream: dict | None = None,
        sink=None,
        run_id: str = "flow",
    ) -> None:
        super().__init__(config)  # NatsResponder.__init__ — sets self.config etc.
        self._init_flow(flow, downstream=downstream, sink=sink, run_id=run_id)

    # -- NATS-shaped correlation / trace hooks ------------------------------

    def _correlation_field(self) -> str:
        """Body key carrying the correlation id. Defaults to ``correlation_id``;
        override via ``config.correlation.field`` (for the ``iso8583`` codec, set
        it to a DE number like ``11``)."""
        return str((self.config.get("correlation") or {}).get("field", "correlation_id"))

    def _correlation_id(self, parsed: dict) -> str:
        field = self._correlation_field()
        body = parsed.get("body")
        val = None
        if isinstance(body, dict):
            val = body.get(field)
            if val is None and isinstance(body.get("de"), dict):
                val = body["de"].get(field)
        return str(val) if val else uuid.uuid4().hex[:12]

    def _inject_correlation(self, payload: Any, cid: str) -> None:
        """Propagate the correlation id onto a downstream call payload so the next
        listener records the same id. Stamp both a body field and an ISO DE 11 —
        whichever the downstream reads, it sees the same id."""
        if not isinstance(payload, dict):
            return
        payload.setdefault(self._correlation_field(), cid)
        de = payload.get("de")
        if isinstance(de, dict):
            de.setdefault("11", cid)

    def _trace_request(self, parsed: dict) -> dict:
        return {
            "subject": parsed.get("subject"),
            "queue_group": self.queue_group or None,
            "inbox": parsed.get("reply") or None,
            "body": parsed.get("body"),
        }

    def _trace_label(self, parsed: dict) -> str:
        return str(parsed.get("subject") or "?")

    # _trace_response_summary -> NatsResponder._response_code. ✓

    async def stop(self) -> None:
        await super().stop()
        try:
            await self._registry.disconnect_all()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
