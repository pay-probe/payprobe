"""Cross-replica run control.

The asyncio task that executes a run lives in exactly one orchestrator process.
For another replica — or the same one after a restart — to observe and stop that
run, we keep a registry of in-flight runs that every replica can read, and route
cancel requests to whichever replica owns the task.

Two implementations, picked by ``REDIS_URL``:

* :class:`InMemoryRunControl` — single process. The registry is a local dict and
  ``request_cancel`` invokes the local cancel handler directly. (dev / CI)
* :class:`RedisRunControl` — the registry is a Redis hash (``payprobe:runs``)
  visible to every replica, and cancel requests fan out on a pub/sub channel
  (``payprobe:run-cancel``). The replica that owns the run's task receives the
  message and cancels it; other replicas ignore it.

Only *liveness/cancel* routing lives here. The full run document is still
persisted by :class:`RunStore` (now durable by default), so any replica can read
history and status; this module adds the missing piece — acting on an in-flight
run it doesn't own.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from typing import Awaitable, Callable

log = logging.getLogger("payprobe.run_control")

CancelHandler = Callable[[str], "Awaitable[None] | None"]

REGISTRY_KEY = "payprobe:runs"
CANCEL_CHANNEL = "payprobe:run-cancel"


async def _maybe_await(value: "Awaitable[None] | None") -> None:
    if inspect.isawaitable(value):
        await value


class InMemoryRunControl:
    """Single-process control: registry is local, cancel is a direct call."""

    def __init__(self) -> None:
        self._runs: dict[str, str] = {}  # run_id -> kind
        self._handler: CancelHandler | None = None

    def set_cancel_handler(self, handler: CancelHandler) -> None:
        self._handler = handler

    async def start(self) -> None:  # symmetry with the Redis impl
        return None

    async def aclose(self) -> None:
        return None

    async def register(self, run_id: str, kind: str = "run") -> None:
        self._runs[run_id] = kind

    async def unregister(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    async def known(self, run_id: str) -> bool:
        return run_id in self._runs

    async def request_cancel(self, run_id: str) -> bool:
        if run_id not in self._runs:
            return False
        if self._handler is not None:
            await _maybe_await(self._handler(run_id))
        return True


class RedisRunControl:
    """Redis-backed control so any replica can observe and stop any run."""

    def __init__(self, redis, owner_id: str | None = None, *, ttl_s: int = 86_400) -> None:
        self.redis = redis  # redis.asyncio.Redis, decode_responses=True
        self.owner = owner_id or os.environ.get("HOSTNAME") or uuid.uuid4().hex[:8]
        self.ttl = ttl_s
        self._owned: set[str] = set()
        self._handler: CancelHandler | None = None
        self._pubsub = None
        self._listener: asyncio.Task | None = None

    def set_cancel_handler(self, handler: CancelHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        """Subscribe to the cancel channel and process requests for owned runs."""
        self._pubsub = self.redis.pubsub()
        await self._pubsub.subscribe(CANCEL_CHANNEL)
        self._listener = asyncio.create_task(self._listen())

    async def aclose(self) -> None:
        if self._listener:
            self._listener.cancel()
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(CANCEL_CHANNEL)
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def register(self, run_id: str, kind: str = "run") -> None:
        self._owned.add(run_id)
        try:
            await self.redis.hset(
                REGISTRY_KEY, run_id, json.dumps({"owner": self.owner, "kind": kind})
            )
        except Exception:  # noqa: BLE001 - registry is best-effort metadata
            log.exception("run_control: failed to register %s", run_id)

    async def unregister(self, run_id: str) -> None:
        self._owned.discard(run_id)
        try:
            await self.redis.hdel(REGISTRY_KEY, run_id)
        except Exception:  # noqa: BLE001
            pass

    async def known(self, run_id: str) -> bool:
        if run_id in self._owned:
            return True
        try:
            return bool(await self.redis.hexists(REGISTRY_KEY, run_id))
        except Exception:  # noqa: BLE001
            return False

    async def request_cancel(self, run_id: str) -> bool:
        if not await self.known(run_id):
            return False
        if run_id in self._owned:
            await self._fire(run_id)  # fast path: we own it
        else:
            try:
                await self.redis.publish(CANCEL_CHANNEL, run_id)
            except Exception:  # noqa: BLE001
                log.exception("run_control: failed to publish cancel for %s", run_id)
                return False
        return True

    async def _listen(self) -> None:
        try:
            async for msg in self._pubsub.listen():
                if msg.get("type") != "message":
                    continue
                run_id = msg.get("data")
                if isinstance(run_id, bytes):
                    run_id = run_id.decode()
                if run_id in self._owned:
                    await self._fire(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the listener die silently
            log.exception("run_control: cancel listener error")

    async def _fire(self, run_id: str) -> None:
        if self._handler is not None:
            await _maybe_await(self._handler(run_id))


def make_run_control(redis_url: str | None):
    """Pick the control backend from configuration."""
    if redis_url:
        import redis.asyncio as aioredis

        return RedisRunControl(aioredis.from_url(redis_url, decode_responses=True))
    return InMemoryRunControl()
