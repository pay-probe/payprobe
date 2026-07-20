"""NATS adapter family (ADR-0006).

PayProbe as either side of a NATS conversation: :class:`~worker.adapters.nats.adapter.NatsAdapter`
is the outbound client (publish / request / JetStream publish);
:class:`~worker.adapters.nats.responder.NatsResponder` is the rules-driven
inbound subscriber; :class:`~worker.adapters.nats.flow_responder.NatsFlowResponder`
computes each reply by walking a participant flow.

The broker itself is *infrastructure* (an external 3-node JetStream cluster in
``infra/``), never a PayProbe-managed simulator — see the ADR for the reasoning.
``nats-py`` is an optional dependency, imported lazily so its absence degrades to
"adapter unavailable" rather than crashing import (grpcio / aiohttp precedent).
"""
