"""Registry contracts — what an agent principal and a workflow *are* (ADR-0010).

An **agent definition** is an identity: role, instructions, a tool allowlist
drawn from the one toolkit registry (``payprobe_common.agent_toolkit``), an
execution mode, a write scope, per-heartbeat limits, a schedulability budget,
triggers and RBAC. It is *not* a runtime: which LLM executes it is a platform
setting (ADR-0010 D4), and the loop that runs it arrives in phase 2.

A **workflow** is a JSON DAG (D1) of ``agent_task`` / ``tool`` / ``condition`` /
``approval`` / ``parallel`` / ``join`` nodes. Validation rules that need the
registry (does ``triage@2`` exist, is that tool real, does a ``full``-mode task
have an approval ancestor) live in :mod:`agent_hub.validate`; this module only
holds shapes and field-level constraints.

Modes (from the assistant, unchanged): ``advisor`` = read tier only;
``plan`` = proposes exact tool calls, a human (or an approved workflow step)
applies them; ``full`` = executes journalled writes. Default is ``plan`` (D3).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Mode = Literal["advisor", "plan", "full"]

#: order used when a workflow node narrows/escalates an agent's mode
MODE_RANK: dict[str, int] = {"advisor": 0, "plan": 1, "full": 2}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
REF_RE = re.compile(r"^([a-z0-9][a-z0-9-]{1,63})(?:@(\d+))?$")

MAX_INSTRUCTIONS_CHARS = 20_000


class Limits(BaseModel):
    """Per-heartbeat caps — the ``MAX_ITERATIONS`` shape, enforced in dispatch
    (phase 2). There is deliberately no hours-long budget here: an agent is
    autonomous only inside one bounded wake (chunked autonomy)."""

    max_steps: int = Field(default=8, ge=1, le=64)
    max_tokens: int = Field(default=60_000, ge=1_000, le=2_000_000)
    wall_clock_s: int = Field(default=300, ge=10, le=3_600)


class Budget(BaseModel):
    """Schedulability budget. When ``daily_tokens`` is exceeded the principal
    stops being schedulable (hard stop) and a budget incident is recorded —
    enforcement lands with the runner in phase 2; the shape is fixed now so
    definitions published in phase 1 do not need a migration."""

    daily_tokens: int | None = Field(default=None, ge=1_000)
    hard_stop: bool = True


class WriteScope(BaseModel):
    """Where journalled writes may land. Empty lists mean *nowhere* — a
    definition must opt in to every project and environment it may touch."""

    projects: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


class Trigger(BaseModel):
    kind: Literal["manual", "schedule", "event", "mcp"] = "manual"
    #: schedule: one of interval_sec / daily_at (HH:MM UTC), same vocabulary as
    #: the orchestrator's schedule_store so the timer wake source can reuse it
    interval_sec: int | None = Field(default=None, ge=30)
    daily_at: str | None = None
    #: event: a run-lifecycle event name, e.g. run.completed / run.failed /
    #: gate.failed / storm.finished (wired in phase 4)
    event: str | None = None

    @field_validator("daily_at")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("daily_at must be HH:MM (UTC)")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Trigger:
        if self.kind == "schedule" and not (self.interval_sec or self.daily_at):
            raise ValueError("schedule trigger needs interval_sec or daily_at")
        if self.kind == "event" and not self.event:
            raise ValueError("event trigger needs an event name")
        if self.kind in ("manual", "mcp") and (self.interval_sec or self.daily_at or self.event):
            raise ValueError(f"{self.kind} trigger takes no schedule/event fields")
        return self


class Rbac(BaseModel):
    invoke: list[str] = Field(default_factory=lambda: ["admin", "operator"])
    edit: list[str] = Field(default_factory=lambda: ["admin"])


class AgentSpec(BaseModel):
    """The versioned, immutable-once-published body of an agent definition."""

    role: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=MAX_INSTRUCTIONS_CHARS)
    #: None = the platform provider's default model (D4: one provider,
    #: configured once in Settings → AI assistant)
    model: str | None = Field(default=None, max_length=100)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    examples: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    #: allowlist of toolkit tool names; validated against the registry
    tools: list[str] = Field(default_factory=list, max_length=64)
    mode: Mode = "plan"
    write_scope: WriteScope = Field(default_factory=WriteScope)
    limits: Limits = Field(default_factory=Limits)
    budget: Budget = Field(default_factory=Budget)
    triggers: list[Trigger] = Field(default_factory=lambda: [Trigger()], max_length=16)
    rbac: Rbac = Field(default_factory=Rbac)

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("tools allowlist has duplicates")
        return v


# -- workflows -----------------------------------------------------------------

NodeType = Literal["agent_task", "tool", "condition", "approval", "parallel", "join"]

END = "end"


class Node(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    type: NodeType
    # agent_task
    agent: str | None = None  # "name" (active version) or "name@N"
    mode: Mode | None = None  # may only narrow the agent's mode
    environment: str | None = None  # static env for the full-mode rule
    reviews: str | None = None  # node id this task reviews (executor ≠ reviewer)
    input: dict[str, Any] = Field(default_factory=dict)
    # tool
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    # condition
    expr: str | None = Field(default=None, max_length=512)
    # approval
    roles: list[str] = Field(default_factory=list)
    timeout_s: int | None = Field(default=None, ge=60, le=30 * 86_400)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if v == END:
            raise ValueError(f"'{END}' is a reserved node id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", v):
            raise ValueError("node id may contain letters, digits, '_', '.', '-'")
        return v

    @field_validator("agent")
    @classmethod
    def _agent_ref(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.fullmatch(v):
            raise ValueError("agent reference must be 'name' or 'name@version'")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Node:
        t = self.type
        if t == "agent_task" and not self.agent:
            raise ValueError(f"node '{self.id}': agent_task needs 'agent'")
        if t == "tool" and not self.tool:
            raise ValueError(f"node '{self.id}': tool node needs 'tool'")
        if t == "condition" and not self.expr:
            raise ValueError(f"node '{self.id}': condition needs 'expr'")
        if t == "approval" and not self.roles:
            raise ValueError(f"node '{self.id}': approval needs 'roles'")
        if t != "agent_task" and (self.agent or self.reviews or self.mode):
            raise ValueError(f"node '{self.id}': agent/mode/reviews only on agent_task")
        return self


class Edge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    when: str | None = Field(default=None, max_length=64)

    model_config = {"populate_by_name": True}


class WorkflowSpec(BaseModel):
    description: str = Field(default="", max_length=2_000)
    inputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[Node] = Field(min_length=1, max_length=64)
    edges: list[Edge] = Field(default_factory=list, max_length=256)


def parse_ref(ref: str) -> tuple[str, int | None]:
    """``"triage@2"`` → ``("triage", 2)``; ``"triage"`` → ``("triage", None)``."""
    m = REF_RE.fullmatch(ref)
    if not m:
        raise ValueError(f"bad reference {ref!r}")
    return m.group(1), (int(m.group(2)) if m.group(2) else None)
