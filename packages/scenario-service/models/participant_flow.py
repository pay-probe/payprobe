"""Participant flows — reactive, listening counterparts to scenarios.

A *participant flow* stands in for a participant in a payment network: it
**listens** on an inbound (server-mode) connection, runs a node graph
(``trigger`` → logic → ``reply``), and answers on the wire. It is the visual
evolution of the rules-driven ``TcpResponder`` simulator and reuses the scenario
graph model: ``nodes`` are :class:`~models.scenario.Step` (with the extra
``trigger``/``reply`` kinds) wired by :class:`~models.scenario.Edge`.

Persisted by ``api.participant_flow_store.ParticipantFlowStore`` (file-backed,
same pattern as scenarios / starter flows). The runtime that actually listens and
replies lives in the worker (``worker.engine.flow_runner`` +
``worker.adapters.tcp.flow_responder``); the orchestrator manages its lifecycle.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .scenario import Edge, Step


class FlowTrigger(BaseModel):
    """What fires the flow: an inbound (listening) connection by name."""

    #: Name of an inbound-mode connection the flow binds to and listens on.
    connection: str = ""


class ParticipantFlowDraft(BaseModel):
    """Editable fields of a participant flow (everything except id/metadata)."""

    name: str
    description: str = ""
    trigger: FlowTrigger = Field(default_factory=FlowTrigger)
    #: Graph nodes — reuse the scenario Step shape; entry is a ``trigger`` node,
    #: terminal(s) are ``reply`` nodes.
    nodes: list[Step] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    #: Flow-scoped variables, exposed to nodes as ``${vars.*}``.
    variables: dict[str, Any] = Field(default_factory=dict)


class ParticipantFlow(ParticipantFlowDraft):
    id: str
    #: Monotonic version, bumped on every save (parity with scenarios; see
    #: ``ParticipantFlowStore.versions``). Pre-versioning documents load as v1.
    version: int = 1
    updated_at: str | None = None


class ParticipantFlowVersionInfo(BaseModel):
    """One row of a flow's version history (metadata only, no document)."""

    version: int
    updated_at: str | None = None
    comment: str = ""
    node_count: int = 0
