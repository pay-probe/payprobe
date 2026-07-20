"""Topologies — a named set of participant flows started/stopped as one unit.

A topology lists the participant flows that make up a simulated network (an
acquirer stand-in, a switch, a fleet of issuers) and how many instances of each
to run. The orchestrator starts them together (callees before callers) and stops
them as a unit; a scenario run can require a topology to be up first.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TopologyParticipant(BaseModel):
    #: A participant-flow id to run.
    flow_id: str
    #: How many listening instances of this flow to start (a fleet).
    instances: int = 1


class TopologyInitiator(BaseModel):
    """A bound traffic driver: a saved scenario auto-run when the topology starts
    (and cancelled when it stops). Its environment points the scenario at the
    topology's entry listener."""

    #: Saved scenario id to run as the topology's traffic source.
    scenario_id: str
    #: Environment to run it under (None = the scenario/orchestrator default).
    environment: str | None = None


class TopologyDraft(BaseModel):
    name: str
    description: str = ""
    #: Order matters: list callees before callers so a flow is listening before
    #: another flow calls it.
    participants: list[TopologyParticipant] = Field(default_factory=list)
    #: Optional traffic driver auto-run with the topology.
    initiator: TopologyInitiator | None = None


class Topology(TopologyDraft):
    id: str
    updated_at: str | None = None
