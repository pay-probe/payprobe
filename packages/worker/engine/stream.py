"""Durable, resumable event backbone.

The spec uses Redis *pub/sub* for run events. Pub/sub is fire-and-forget: a
client that is not connected at the instant an event is published never sees it,
so a dropped WebSocket or a late-joining watcher loses history. For ~30 people
watching multi-minute runs that is a real problem.

This module replaces pub/sub with an append-only **stream**: every event is
stored with a monotonic id, so a consumer can ask "give me everything after id
X" and replay what it missed before tailing live. That is exactly the primitive
WebSocket reconnect-resume needs.

Two implementations:
- ``InMemoryStreamBackbone`` — single-process, no infra; for tests/dev and the
  mock end-to-end path.
- ``RedisStreamBackbone`` — Redis Streams (XADD/XRANGE/XREAD); for real
  multi-process deployments.

Both satisfy the same ``StreamBackbone`` protocol, so the orchestrator's
WebSocket handler is identical regardless of backend.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Protocol

from .events import RunEvent, RUN_COMPLETED

# A stored event is (id, payload-dict). ``id`` is opaque to consumers; they only
# pass the last id they saw back in to resume.
StreamEntry = tuple[str, dict[str, Any]]


class StreamBackbone(Protocol):
    async def append(self, run_id: str, event: dict[str, Any]) -> str:
        """Append an event, returning its stream id."""
        ...

    def stream(self, run_id: str, after_id: str | None = None) -> AsyncIterator[StreamEntry]:
        """Yield events after ``after_id`` (None = from the start), then tail
        live until the run's terminal event, then stop."""
        ...


class StreamSink:
    """Adapts a ``StreamBackbone`` to the engine's ``EventSink`` interface."""

    def __init__(self, backbone: "StreamBackbone") -> None:
        self.backbone = backbone

    async def publish(self, event: RunEvent) -> None:
        await self.backbone.append(event.run_id, event.to_dict())


class InMemoryStreamBackbone:
    """Append-only per-run event log with live tailing and replay.

    Ids are per-run monotonic integers rendered as strings. Tailing uses short
    polling rather than a loop-bound primitive (Condition/Event), so the same
    backbone instance works across multiple event loops — which matters for
    test harnesses and multi-worker dev setups. The production
    ``RedisStreamBackbone`` uses real blocking ``XREAD`` instead.
    """

    #: poll interval while waiting for new events (seconds)
    POLL_INTERVAL = 0.02

    def __init__(self) -> None:
        self._events: dict[str, list[StreamEntry]] = {}
        self._completed: set[str] = set()

    async def append(self, run_id: str, event: dict[str, Any]) -> str:
        # no await between sizing and appending -> atomic under asyncio
        entries = self._events.setdefault(run_id, [])
        entry_id = str(len(entries) + 1)
        entries.append((entry_id, event))
        if event.get("type") == RUN_COMPLETED:
            self._completed.add(run_id)
        return entry_id

    async def stream(self, run_id: str, after_id: str | None = None) -> AsyncIterator[StreamEntry]:
        after = int(after_id) if after_id else 0
        while True:
            entries = self._events.get(run_id, [])
            pending = [(i, e) for (i, e) in entries if int(i) > after]
            for entry_id, event in pending:
                after = int(entry_id)
                yield entry_id, event
                if event.get("type") == RUN_COMPLETED:
                    return
            if not pending:
                if run_id in self._completed:
                    return
                await asyncio.sleep(self.POLL_INTERVAL)

    # convenience for tests / non-streaming reads
    def history(self, run_id: str) -> list[StreamEntry]:
        return list(self._events.get(run_id, []))


class RedisStreamBackbone:
    """Redis Streams backed backbone (XADD / XRANGE / XREAD).

    Stream key is ``run:{run_id}:stream``. Each entry stores the event JSON
    under the ``data`` field. Redis-native entry ids ("<ms>-<seq>") double as
    the resume cursor, so a WebSocket ``last_event_id`` maps straight onto
    XRANGE/XREAD start arguments.
    """

    def __init__(self, redis: Any, *, maxlen: int = 100_000) -> None:
        # ``redis`` is a redis.asyncio.Redis instance (decode_responses=True).
        self.redis = redis
        self.maxlen = maxlen

    @staticmethod
    def _key(run_id: str) -> str:
        return f"run:{run_id}:stream"

    async def append(self, run_id: str, event: dict[str, Any]) -> str:
        return await self.redis.xadd(
            self._key(run_id),
            {"data": json.dumps(event)},
            maxlen=self.maxlen,
            approximate=True,
        )

    async def stream(self, run_id: str, after_id: str | None = None) -> AsyncIterator[StreamEntry]:
        key = self._key(run_id)
        last = after_id or "0"
        # 1) replay backlog after `last`
        if after_id:
            backlog = await self.redis.xrange(key, min=f"({after_id}", max="+")
        else:
            backlog = await self.redis.xrange(key, min="-", max="+")
        for entry_id, fields in backlog:
            last = entry_id
            event = json.loads(fields["data"])
            yield entry_id, event
            if event.get("type") == RUN_COMPLETED:
                return
        # 2) tail live
        while True:
            resp = await self.redis.xread({key: last}, block=15_000, count=64)
            if not resp:
                continue
            _key, entries = resp[0]
            for entry_id, fields in entries:
                last = entry_id
                event = json.loads(fields["data"])
                yield entry_id, event
                if event.get("type") == RUN_COMPLETED:
                    return
