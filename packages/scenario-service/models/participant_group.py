"""Participant groups — several connection instances as one logical participant.

A group (a "fleet") names N member connections and a selection policy. A scenario
step or a flow's outbound node can target the group by name; the worker picks a
member per dispatch (round_robin / weighted / random / failover / sticky). The
orchestrator inlines each member's resolved connection config at run-build time.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GroupMember(BaseModel):
    #: Member connection name (resolved to its config at run-build time).
    connection: str
    #: Relative weight for the ``weighted`` policy.
    weight: int = 1


class ParticipantGroupDraft(BaseModel):
    name: str
    description: str = ""
    #: Informational: the adapter family members share (e.g. "tcp"). Members
    #: carry their own real config via their connection.
    adapter_type: str = ""
    members: list[GroupMember] = Field(default_factory=list)
    #: ``{policy, key}`` — policy ∈ round_robin|weighted|random|failover|sticky;
    #: ``key`` is a request field path for sticky routing (e.g. ``de.2`` for BIN).
    selection: dict[str, Any] = Field(default_factory=lambda: {"policy": "round_robin"})


class ParticipantGroup(ParticipantGroupDraft):
    id: str
    updated_at: str | None = None
