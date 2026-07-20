"""Cross-replica run control: registry + cancel routing."""
import asyncio
import json

import pytest

from orchestrator.api.run_control import (
    CANCEL_CHANNEL, InMemoryRunControl, RedisRunControl,
)


# -- in-memory -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_inmemory_register_known_cancel():
    rc = InMemoryRunControl()
    fired: list[str] = []
    rc.set_cancel_handler(lambda rid: fired.append(rid))

    assert await rc.known("r1") is False
    assert await rc.request_cancel("r1") is False  # unknown ⇒ not routed

    await rc.register("r1", "run")
    assert await rc.known("r1") is True
    assert await rc.request_cancel("r1") is True
    assert fired == ["r1"]

    await rc.unregister("r1")
    assert await rc.known("r1") is False


# -- redis (fake client) -------------------------------------------------------

class _FakePubSub:
    def __init__(self): self._q = asyncio.Queue()
    async def subscribe(self, *_): pass
    async def unsubscribe(self, *_): pass
    async def aclose(self): pass
    async def feed(self, channel, data):
        await self._q.put({"type": "message", "channel": channel, "data": data})
    async def listen(self):
        while True:
            yield await self._q.get()


class _FakeRedis:
    """Just enough of redis.asyncio for RedisRunControl."""
    def __init__(self):
        self.h: dict[str, str] = {}
        self.ps = _FakePubSub()
        self.published = []
    async def hset(self, key, field, value): self.h[field] = value
    async def hdel(self, key, field): self.h.pop(field, None)
    async def hexists(self, key, field): return field in self.h
    async def publish(self, channel, data):
        self.published.append((channel, data))
        await self.ps.feed(channel, data)   # deliver to subscribers
    def pubsub(self): return self.ps


@pytest.mark.asyncio
async def test_redis_register_records_owner():
    r = _FakeRedis()
    rc = RedisRunControl(r, owner_id="replica-A")
    await rc.register("r1", "run")
    assert "r1" in r.h
    assert json.loads(r.h["r1"]) == {"owner": "replica-A", "kind": "run"}
    assert await rc.known("r1") is True


@pytest.mark.asyncio
async def test_redis_owned_cancel_is_direct():
    """A run owned by this replica is cancelled without a pub/sub round trip."""
    r = _FakeRedis()
    rc = RedisRunControl(r, owner_id="A")
    fired: list[str] = []
    rc.set_cancel_handler(lambda rid: fired.append(rid))
    await rc.register("r1")
    assert await rc.request_cancel("r1") is True
    assert fired == ["r1"]
    assert r.published == []  # fast path, no publish


@pytest.mark.asyncio
async def test_redis_unowned_cancel_publishes_and_owner_acts():
    """Replica B cancels a run owned by replica A: B publishes, A's listener fires."""
    r = _FakeRedis()
    owner_a = RedisRunControl(r, owner_id="A")
    fired: list[str] = []
    owner_a.set_cancel_handler(lambda rid: fired.append(rid))
    await owner_a.register("r1")
    await owner_a.start()  # A listens on the shared channel

    other_b = RedisRunControl(r, owner_id="B")  # same fake redis = shared state
    assert await other_b.request_cancel("r1") is True
    assert r.published == [(CANCEL_CHANNEL, "r1")]

    await asyncio.sleep(0.05)  # let A's listener process the message
    assert fired == ["r1"]
    await owner_a.aclose()


@pytest.mark.asyncio
async def test_redis_unknown_run_not_routed():
    r = _FakeRedis()
    rc = RedisRunControl(r, owner_id="A")
    assert await rc.request_cancel("nope") is False
