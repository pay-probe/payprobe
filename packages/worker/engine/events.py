"""Run event model and sinks.

The engine emits a stream of typed events as a run progresses. A sink decides
where they go: ``InMemorySink`` for tests/local, a Redis pub/sub sink for the
orchestrator (added in Step 3). Keeping the engine sink-agnostic means the
execution core has no Redis dependency.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncIterator, Iterable, Protocol

# -- event types -------------------------------------------------------------

RUN_STARTED = "run.started"
PHASE_UPDATE = "phase.update"
STEP_RESULT = "step.result"
SCENARIO_RESULT = "scenario.result"
RUN_COMPLETED = "run.completed"
DEBUG_PAUSED = "debug.paused"
DEBUG_RESUMED = "debug.resumed"


@dataclass
class RunEvent:
    """A single event on a run's stream.

    ``type`` matches the discriminator the portal's ``run-monitor.service``
    filters on (``phase.update``, ``step.result``, …).
    """

    type: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventSink(Protocol):
    """Anything the engine can publish events to."""

    async def publish(self, event: RunEvent) -> None: ...


class InMemorySink:
    """Collects events in a list. Used by tests and the CLI harness."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        self.events.append(event)

    def of_type(self, type_: str) -> list[RunEvent]:
        return [e for e in self.events if e.type == type_]


class NullSink:
    """Discards everything. Default when no sink is supplied."""

    async def publish(self, event: RunEvent) -> None:  # noqa: D401
        return None


class FanoutSink:
    """Publishes each event to several sinks (e.g. Redis + live queue)."""

    def __init__(self, sinks: Iterable[EventSink]) -> None:
        self.sinks = list(sinks)

    async def publish(self, event: RunEvent) -> None:
        for sink in self.sinks:
            await sink.publish(event)


class QueueSink:
    """Bridges the push-based sink model to a pull-based async iterator.

    Used by ``WorkerEngine.iter_run`` to expose the run as
    ``async for event in engine.iter_run(...)``. ``drain`` stops after the
    terminal ``run.completed`` event so the consumer's loop ends cleanly.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[RunEvent] = asyncio.Queue()

    async def publish(self, event: RunEvent) -> None:
        await self.queue.put(event)

    async def drain(self) -> AsyncIterator[RunEvent]:
        while True:
            event = await self.queue.get()
            yield event
            if event.type == RUN_COMPLETED:
                return
