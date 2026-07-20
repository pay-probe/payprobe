"""FlowRunner — the message-triggered driver for Participant flows.

A *participant flow* is the reactive complement to a scenario: instead of being
*run* (start → steps → verdict), it is *triggered* by an incoming message, walks
the same node graph, and produces a **reply** to send back on the inbound
connection.

It reuses the shared :class:`~worker.engine.runner.GraphExecutor` core — the same
node executors (``code``/``crypto``/``if``/``switch``/``loop`` …), ``${...}``
resolution, edge walk and trace emission the scenario engine uses. The only
differences are the entry (a ``trigger`` node, with the parsed request seeded
into context) and the exit (a ``reply`` node, whose resolved payload becomes the
response). Outbound ``action`` calls to downstream connections arrive in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .generators import GeneratorContext
from .runner import (
    GraphExecutor,
    ScenarioResult,
    StepOutcome,
    REPLY_KEY,
    PASSED,
)
from .variables import GEN_KEY


async def _no_outbound(target: str, action: str, payload: dict, assertions: list):
    """Default executor for Phase 1 flows (no outbound calls yet)."""
    raise NotImplementedError(
        "outbound 'action' nodes in a participant flow are not available until "
        "Phase 2 — a Phase 1 flow is trigger → logic → reply only"
    )


@dataclass
class FlowResult:
    """Outcome of one triggered participant-flow execution."""

    #: Resolved payload of the ``reply`` node, or ``None`` if the flow produced
    #: no reply (the responder may then drop or send a default).
    reply: Any = None
    #: ``PASSED`` unless a node errored — flows have no pass/fail verdict, but a
    #: failing node still surfaces here for diagnostics.
    status: str = PASSED
    #: Per-node outcomes (for tracing).
    steps: list[StepOutcome] = field(default_factory=list)
    #: The final execution context (for debugging / assertions in tests).
    context: dict[str, Any] = field(default_factory=dict)


class FlowRunner(GraphExecutor):
    """Drives a participant flow's graph in response to one inbound message."""

    async def run_flow(
        self,
        flow: dict,
        request: Any,
        *,
        execute_step=None,
        phase: int = 0,
        state: dict | None = None,
    ) -> FlowResult:
        """Execute ``flow`` for one incoming ``request`` and return its reply.

        ``flow`` is ``{ id?, name?, nodes|steps: [...], edges: [...], variables? }``.
        ``request`` is the parsed inbound message exposed to nodes as
        ``${request.*}`` (and ``${<trigger_id>.request.*}``). ``state`` (if given)
        is the flow instance's shared dict — ``state`` nodes mutate it in place and
        nodes read it as ``${state.*}``, so it persists across messages.
        """
        flow_id = flow.get("id", flow.get("name", "flow"))
        nodes = flow.get("nodes") or flow.get("steps") or []
        edges = flow.get("edges") or []

        context: dict[str, Any] = {
            "vars": flow.get("variables") or {},
            "tables": flow.get("tables") or {},
            "request": request,
            "trigger": {"request": request},
            "state": state if state is not None else {},
            GEN_KEY: self._generators
            or GeneratorContext(
                seed=flow_id,
                terminals=flow.get("terminals"),
                cards=flow.get("cards"),
            ),
        }
        if self._depth == 0:
            context[GEN_KEY].new_transaction()

        # Expose the request under the trigger node's id too, so a flow can
        # reference it the same way a scenario references a prior step's output.
        for n in nodes:
            if n.get("kind") == "trigger":
                context[n["id"]] = {"request": request}
                break

        result = ScenarioResult(scenario_id=flow_id, name=flow.get("name", flow_id), status=PASSED)
        # A flow is a graph (trigger → … → reply); reuse the shared graph walk.
        graph = {"steps": nodes, "edges": edges}
        await self._run_graph(
            graph,
            execute_step or _no_outbound,
            context,
            flow_id,
            phase,
            result,
            stop_on_failure=True,
        )
        return FlowResult(
            reply=context.get(REPLY_KEY),
            status=result.status,
            steps=result.steps,
            context=context,
        )
