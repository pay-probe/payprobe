"""RedisLoadBus flow-hosting paths exercised against fakeredis — verifies the
Redis key/queue/hash plumbing (placements, registry TTL pruning, network stop)
without a real server. The in-memory bus covers behaviour; this covers the
Redis serialization + pipeline code that dev/CI otherwise never runs."""

import asyncio
import json

import pytest

fakeredis = pytest.importorskip("fakeredis")

from worker.engine.load import RedisLoadBus  # noqa: E402
from worker.flow_host import run_flow_host  # noqa: E402

pytestmark = pytest.mark.asyncio


def _bus() -> RedisLoadBus:
    return RedisLoadBus(fakeredis.aioredis.FakeRedis(decode_responses=True))


FLOW = {
    "id": "echo",
    "name": "echo",
    "trigger": {"connection": ""},
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}},
    ],
    "edges": [{"source": "t", "source_port": "out", "target": "r"}],
}


def _placement(run_id, iid):
    return {
        "run_id": run_id,
        "instance_id": iid,
        "flow_id": "echo",
        "flow_name": "echo",
        "node_id": "n",
        "flow": FLOW,
        "config": {"protocol": "iso8583", "host": "127.0.0.1", "port": 0},
        "downstream": {},
        "kind": "responder",
        "proxy_config": None,
    }


async def test_redis_placement_queue_roundtrip():
    bus = _bus()
    await bus.push_placements([_placement("r1", "r1-000")])
    got = await bus.claim_placement(timeout_s=1)
    assert got["instance_id"] == "r1-000"
    assert got["flow"]["nodes"][0]["kind"] == "trigger"  # JSON round-trip
    assert await bus.claim_placement(timeout_s=1) is None  # queue drained


async def test_redis_registry_advertise_list_retract_and_ttl_prune():
    bus = _bus()
    await bus.advertise_instance("r1", {"instance_id": "a", "status": "up", "port": 9})
    rows = await bus.list_instances("r1")
    assert [r["instance_id"] for r in rows] == ["a"]
    assert rows[0]["ts"] > 0

    # a stale advertisement (dead host) is pruned on read
    key = bus._instances_key("r1")
    stale = {"instance_id": "b", "status": "up", "ts": 1.0}
    await bus.redis.hset(key, "b", json.dumps(stale))
    rows = await bus.list_instances("r1")
    assert [r["instance_id"] for r in rows] == ["a"]
    assert await bus.redis.hget(key, "b") is None  # physically removed

    await bus.retract_instance("r1", "a")
    assert await bus.list_instances("r1") == []


async def test_redis_network_stop_flag():
    bus = _bus()
    assert await bus.is_network_stopped("r1") is False
    await bus.signal_network_stop("r1")
    assert await bus.is_network_stopped("r1") is True


async def test_flow_host_end_to_end_over_redis_bus():
    """The full host loop against the Redis implementation: claim → start →
    advertise → network stop → retract."""
    bus = _bus()
    stop = asyncio.Event()
    host = asyncio.create_task(
        run_flow_host(
            bus, host_id="fh-r", advertise_host="rhost", stop_event=stop, claim_timeout_s=1.0
        )
    )
    try:
        await bus.push_placements([_placement("r2", "r2-000")])

        async def _up():
            rows = await bus.list_instances("r2")
            return [r for r in rows if r.get("status") == "up"] or None

        deadline = asyncio.get_event_loop().time() + 5
        rows = None
        while asyncio.get_event_loop().time() < deadline:
            rows = await _up()
            if rows:
                break
            await asyncio.sleep(0.05)
        assert rows and rows[0]["host"] == "rhost" and rows[0]["port"]

        await bus.signal_network_stop("r2")
        while asyncio.get_event_loop().time() < deadline:
            if not await bus.list_instances("r2"):
                break
            await asyncio.sleep(0.05)
        assert await bus.list_instances("r2") == []
    finally:
        stop.set()
        await asyncio.wait_for(host, timeout=5)
