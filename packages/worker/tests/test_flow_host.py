"""Flow-host worker (ADR-0001): claim placements, host listeners, advertise
endpoints, honour network-wide stop and worker drain — all over the in-memory
bus (item 7 of the ADR: fleet behaviour testable without Redis)."""

import asyncio

import pytest

from worker.engine.load import InMemoryLoadBus
from worker.flow_host import run_flow_host

pytestmark = pytest.mark.asyncio


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


def _placement(run_id, iid, port=0):
    return {
        "run_id": run_id,
        "instance_id": iid,
        "flow_id": "echo",
        "flow_name": "echo",
        "node_id": "n-echo",
        "flow": FLOW,
        "config": {"protocol": "iso8583", "host": "127.0.0.1", "port": port},
        "downstream": {},
        "kind": "responder",
        "proxy_config": None,
    }


async def _wait_for(predicate, timeout_s=5.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_host_claims_places_advertises_and_stops():
    bus = InMemoryLoadBus()
    stop = asyncio.Event()
    host = asyncio.create_task(
        run_flow_host(
            bus, host_id="fh-test", advertise_host="worker-a", stop_event=stop, claim_timeout_s=0.05
        )
    )
    try:
        # presence: the host heartbeats with the flow_host role
        workers = await _wait_for(bus.list_workers)
        assert workers[0]["role"] == "flow_host" and workers[0]["hosted"] == 0

        await bus.push_placements([_placement("run1", "run1-000"), _placement("run1", "run1-001")])
        instances = await _wait_for(lambda: _two_up(bus))
        by_id = {i["instance_id"]: i for i in instances}
        assert set(by_id) == {"run1-000", "run1-001"}
        for i in by_id.values():
            assert i["status"] == "up"
            assert i["host"] == "worker-a" and i["port"]  # bound + advertised
            assert i["worker_id"] == "fh-test"
        # two distinct ephemeral ports on one host
        assert len({i["port"] for i in by_id.values()}) == 2

        # network-wide stop → the host tears down + retracts both instances
        await bus.signal_network_stop("run1")
        await _wait_for(lambda: _none_left(bus))
    finally:
        stop.set()
        summary = await asyncio.wait_for(host, timeout=5)
    assert summary["started_total"] == 2
    # clean deregister on exit
    assert await bus.list_workers() == []


async def _two_up(bus):
    rows = await bus.list_instances("run1")
    ups = [r for r in rows if r.get("status") == "up"]
    return ups if len(ups) == 2 else None


async def _none_left(bus):
    return not await bus.list_instances("run1") or None


async def test_bad_placement_advertises_error_and_host_survives():
    bus = InMemoryLoadBus()
    stop = asyncio.Event()
    host = asyncio.create_task(
        run_flow_host(bus, host_id="fh-err", stop_event=stop, claim_timeout_s=0.05)
    )
    try:
        bad = _placement("run2", "run2-000")
        bad["config"] = {"protocol": "iso8583", "host": "no-such-host-zzz", "port": 1}
        await bus.push_placements([bad])

        async def _err():
            rows = await bus.list_instances("run2")
            errs = [r for r in rows if r.get("status") == "error"]
            return errs or None

        errs = await _wait_for(_err)
        assert errs[0]["instance_id"] == "run2-000" and errs[0]["error"]

        # the host is still alive and can serve a good placement afterwards
        await bus.push_placements([_placement("run3", "run3-000")])

        async def _up():
            rows = await bus.list_instances("run3")
            ups = [r for r in rows if r.get("status") == "up"]
            return ups or None

        assert await _wait_for(_up)
        await bus.signal_network_stop("run3")
    finally:
        stop.set()
        await asyncio.wait_for(host, timeout=5)


async def test_worker_drain_stops_hosted_instances():
    bus = InMemoryLoadBus()
    stop = asyncio.Event()
    host = asyncio.create_task(
        run_flow_host(bus, host_id="fh-drain", stop_event=stop, claim_timeout_s=0.05)
    )
    try:
        await bus.push_placements([_placement("run4", "run4-000")])

        async def _up():
            rows = await bus.list_instances("run4")
            return [r for r in rows if r.get("status") == "up"] or None

        assert await _wait_for(_up)
        await bus.signal_worker_stop("fh-drain")  # fleet-panel drain
        await asyncio.wait_for(host, timeout=5)  # loop exits on its own
        assert not await bus.list_instances("run4")
    finally:
        stop.set()
        if not host.done():
            await asyncio.wait_for(host, timeout=5)
