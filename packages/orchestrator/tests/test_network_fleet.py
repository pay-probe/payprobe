"""Fleet-hosted network flows (ADR-0001): with NETWORK_FLEET=1 and a flow host
on the bus, start_network_flow places listeners on the fleet, health reads the
endpoint registry, and stop tears the run down across hosts. All over the
in-memory bus — no Redis."""
import asyncio
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.flow_fleet import PlacementError  # noqa: E402
from worker.flow_host import run_flow_host  # noqa: E402


_FLOW = {
    "id": "echo", "name": "echo",
    "trigger": {"connection": ""},
    "nodes": [{"id": "t", "kind": "trigger"},
              {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}}],
    "edges": [{"source": "t", "source_port": "out", "target": "r"}],
}


def _fake_get(net, plan):
    nid = net["id"]

    async def fake(url):
        if url.endswith(f"/network-flows/{nid}/plan"):
            return plan
        if url.endswith(f"/network-flows/{nid}"):
            return net
        if url.endswith("/participant-flows/echo"):
            return _FLOW
        raise AssertionError(f"unexpected fetch: {url}")

    return fake


async def _wait_for(predicate, timeout_s=5.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture()
def fleet_env(monkeypatch):
    monkeypatch.setenv("NETWORK_FLEET", "1")
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    yield
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()


async def test_fleet_start_health_endpoints_and_stop(fleet_env, monkeypatch):
    net = {"id": "fleet-net", "name": "fleet net"}
    plan = {"participants": [
        {"node_id": "echo", "flow_id": "echo", "instances": 2, "port": None},
    ], "initiators": [], "simulators": [], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get(net, plan))

    stop = asyncio.Event()
    host = asyncio.create_task(run_flow_host(
        m.load_bus, host_id="fh-1", advertise_host="worker-a",
        stop_event=stop, claim_timeout_s=0.05))
    try:
        # host present → fleet mode kicks in
        await _wait_for(lambda: m.flow_fleet.hosts())

        started = await m.start_network_flow("fleet-net")
        assert started["fleet"] is True
        assert len(started["participants"]) == 2
        assert all(p["fleet"] and p["port"] for p in started["participants"])
        assert all(p["endpoint"].startswith("worker-a:")
                   for p in started["participants"])
        assert len(m.PARTICIPANTS) == 0          # nothing hosted in-process

        run = m.TOPOLOGY_RUNS[started["id"]]
        assert run["fleet"] and len(run["instance_ids"]) == 2
        # health reads the registry snapshot (filled by place())
        assert m._run_health(run) == {"total": 2, "live": 2, "ready": True}
        assert m._topology_is_up("fleet-net") is True
        # endpoints come from the advertisements
        eps = m._run_endpoints(run)
        assert len(eps) == 2 and all(e["worker"] == "fh-1" for e in eps)

        # stop → the host tears both down and the registry drains
        await m.stop_topology(started["id"])

        async def _drained():
            return not await m.load_bus.list_instances(started["id"]) or None

        await _wait_for(_drained)
        assert started["id"] not in m.TOPOLOGY_RUNS
    finally:
        stop.set()
        await asyncio.wait_for(host, timeout=5)


async def test_fleet_placement_failure_is_502_and_rolls_back(fleet_env, monkeypatch):
    """A host that cannot bind reports an error advertisement — the start
    fails closed (502) and the run is stopped network-wide."""
    net = {"id": "fleet-bad", "name": "fleet bad"}
    plan = {"participants": [
        {"node_id": "echo", "flow_id": "echo", "instances": 1, "port": None},
    ], "initiators": [], "simulators": [], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get(net, plan))
    # poison the listen config so the remote start fails
    orig_prepare = m._prepare_flow_launch

    async def bad_prepare(flow_id, flow, port):
        prep = await orig_prepare(flow_id, flow, port)
        prep["config"] = {**prep["config"], "host": "no-such-host-zzz", "port": 1}
        return prep

    monkeypatch.setattr(m, "_prepare_flow_launch", bad_prepare)

    stop = asyncio.Event()
    host = asyncio.create_task(run_flow_host(
        m.load_bus, host_id="fh-2", stop_event=stop, claim_timeout_s=0.05))
    try:
        await _wait_for(lambda: m.flow_fleet.hosts())
        with pytest.raises(HTTPException) as ei:
            await m.start_network_flow("fleet-bad")
        assert ei.value.status_code == 502
        assert "could not start instance" in str(ei.value.detail)
        assert m.TOPOLOGY_RUNS == {}
    finally:
        stop.set()
        await asyncio.wait_for(host, timeout=5)


async def test_no_hosts_means_in_process_fallback(fleet_env, monkeypatch):
    """NETWORK_FLEET=1 but no flow host heartbeating → the old in-process path
    runs unchanged (dev/CI default behaviour, ADR item 7)."""
    net = {"id": "local-net", "name": "local net"}
    plan = {"participants": [
        {"node_id": "echo", "flow_id": "echo", "instances": 1, "port": None},
    ], "initiators": [], "simulators": [], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get(net, plan))

    started = await m.start_network_flow("local-net")
    try:
        assert started["fleet"] is False
        assert len(m.PARTICIPANTS) == 1           # hosted in-process
        run = m.TOPOLOGY_RUNS[started["id"]]
        assert m._run_health(run)["ready"] is True
    finally:
        await m.stop_topology(started["id"])


def test_rewrite_adapters_map_repoints_ports():
    """Discovery: adapter configs (plain, group members, target/url styles)
    whose port matches a fleet advertisement get the advertised host."""
    idx = {8101: "worker-a", 9090: "worker-b"}
    adapters = {
        "switch": {"adapter": "tcp", "host": "127.0.0.1", "port": 8101},
        "grpc_thing": {"adapter": "grpc", "target": "localhost:9090"},
        "restpay": {"adapter": "http", "base_url": "http://127.0.0.1:9090/api"},
        "issuers": {"adapter": "group", "members": [
            {"connection": "a", "config": {"host": "127.0.0.1", "port": 8101}},
            {"connection": "b", "config": {"host": "127.0.0.1", "port": 7777}},
        ]},
        "untouched": {"adapter": "tcp", "host": "10.0.0.9", "port": 7777},
    }
    hits = m._rewrite_adapters_map(adapters, idx)
    assert adapters["switch"]["host"] == "worker-a"
    assert adapters["grpc_thing"]["target"] == "worker-b:9090"
    assert adapters["restpay"]["base_url"] == "http://worker-b:9090/api"
    assert adapters["issuers"]["members"][0]["config"]["host"] == "worker-a"
    assert adapters["issuers"]["members"][1]["config"]["host"] == "127.0.0.1"
    assert adapters["untouched"]["host"] == "10.0.0.9"  # port not advertised
    assert hits == 4


async def test_fleet_waves_rewrite_caller_downstream(fleet_env, monkeypatch):
    """Two-hop network: the callee (issuer) is placed first; the caller's
    (switch) downstream config pointing at the issuer's port is re-pointed at
    the advertised worker host before the switch is placed."""
    issuer_flow = {
        "id": "iss", "name": "iss", "trigger": {"connection": "iss_in"},
        "nodes": [{"id": "t", "kind": "trigger"},
                  {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}}],
        "edges": [{"source": "t", "source_port": "out", "target": "r"}],
    }
    switch_flow = {
        "id": "sw", "name": "sw", "trigger": {"connection": ""},
        "nodes": [{"id": "t", "kind": "trigger"},
                  {"id": "call", "kind": "action", "target": "iss_out",
                   "action": "send_message", "payload": {}},
                  {"id": "r", "kind": "reply", "payload": "${call.response}"}],
        "edges": [{"source": "t", "target": "call"},
                  {"source": "call", "target": "r"}],
    }
    conn_in = {"name": "iss_in", "protocol": "iso8583", "mode": "inbound",
               "host": "127.0.0.1", "port": 8901}
    conn_out = {"name": "iss_out", "protocol": "iso8583", "mode": "outbound",
                "host": "127.0.0.1", "port": 8901}
    net = {"id": "hop-net", "name": "hop net"}
    plan = {"participants": [
        {"node_id": "issuer", "flow_id": "iss", "instances": 1, "port": None},
        {"node_id": "switch", "flow_id": "sw", "instances": 1, "port": None},
    ], "initiators": [], "simulators": [], "warnings": []}

    async def fake(url):
        for suffix, doc in (
            ("/network-flows/hop-net/plan", plan),
            ("/network-flows/hop-net", net),
            ("/participant-flows/iss", issuer_flow),
            ("/participant-flows/sw", switch_flow),
            ("/connections/iss_in", conn_in),
            ("/connections/iss_out", conn_out),
        ):
            if url.endswith(suffix):
                return doc
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(m, "_http_get_json", fake)

    claimed: list[dict] = []
    orig_claim = m.load_bus.claim_placement

    async def spy_claim(timeout_s=5.0):
        p = await orig_claim(timeout_s)
        if p is not None:
            claimed.append(p)
        return p

    monkeypatch.setattr(m.load_bus, "claim_placement", spy_claim)

    stop = asyncio.Event()
    host = asyncio.create_task(run_flow_host(
        m.load_bus, host_id="fh-hop", advertise_host="worker-a",
        stop_event=stop, claim_timeout_s=0.05))
    try:
        await _wait_for(lambda: m.flow_fleet.hosts())
        started = await m.start_network_flow("hop-net")
        # the switch's placement (second wave) had its downstream re-pointed
        sw = next(p for p in claimed if p["flow_id"] == "sw")
        assert sw["downstream"]["iss_out"]["host"] == "worker-a"
        assert sw["downstream"]["iss_out"]["port"] == 8901
        # the issuer itself was placed with the original bind config
        iss = next(p for p in claimed if p["flow_id"] == "iss")
        assert iss["config"]["port"] == 8901
        await m.stop_topology(started["id"])
    finally:
        stop.set()
        await asyncio.wait_for(host, timeout=5)


async def test_attach_fleet_endpoints_repoints_run_env(fleet_env, monkeypatch):
    """Run-build discovery: with a live fleet advertisement on port 8955, a
    scenario env adapter pointing at 127.0.0.1:8955 is re-pointed."""
    m.flow_fleet._snapshot["r-x"] = [
        {"instance_id": "r-x-000", "status": "up", "host": "worker-b", "port": 8955},
    ]
    env = {"adapters": {"entry": {"adapter": "tcp", "host": "127.0.0.1", "port": 8955}}}
    m._attach_fleet_endpoints(env)
    assert env["adapters"]["entry"]["host"] == "worker-b"
    # without the flag it is a strict no-op
    monkeypatch.delenv("NETWORK_FLEET")
    env2 = {"adapters": {"entry": {"adapter": "tcp", "host": "127.0.0.1", "port": 8955}}}
    m._attach_fleet_endpoints(env2)
    assert env2["adapters"]["entry"]["host"] == "127.0.0.1"
    m.flow_fleet._snapshot.pop("r-x", None)


async def test_reconcile_replaces_lost_instances(fleet_env):
    """Worker death (ADR-0001 item 6): when an instance's advertisement is
    gone, reconcile pushes its stored placement back onto the queue — and the
    cooldown prevents an immediate second push."""
    from worker.engine.load import InMemoryLoadBus
    from orchestrator.api.flow_fleet import FlowFleet

    bus = InMemoryLoadBus()
    fleet = FlowFleet(bus)
    placement = {"run_id": "r9", "instance_id": "r9-000", "flow_id": "f"}
    fleet._placements["r9"] = {"r9-000": placement}

    # not advertised → re-placed once
    assert await fleet.reconcile("r9") == ["r9-000"]
    assert await bus.claim_placement(timeout_s=0.1) == placement
    # cooldown: an immediate second reconcile does nothing
    assert await fleet.reconcile("r9") == []
    # advertised again → nothing to do even after the cooldown
    fleet._replaced_at.clear()
    await bus.advertise_instance("r9", {"instance_id": "r9-000", "status": "up"})
    assert await fleet.reconcile("r9") == []
    # a stopped run is never re-placed
    fleet._replaced_at.clear()
    await bus.retract_instance("r9", "r9-000")
    await bus.signal_network_stop("r9")
    assert await fleet.reconcile("r9") == []


async def test_lost_instance_recovers_on_surviving_host(fleet_env):
    """End to end: host A dies (advertisement retracted), reconcile re-places,
    host B picks the instance up and the registry shows it live again."""
    from worker.engine.load import InMemoryLoadBus
    from orchestrator.api.flow_fleet import FlowFleet

    bus = InMemoryLoadBus()
    fleet = FlowFleet(bus)
    flow = dict(_FLOW)
    placement = {
        "run_id": "r10", "instance_id": "r10-000", "flow_id": "echo",
        "flow_name": "echo", "node_id": "n", "flow": flow,
        "config": {"protocol": "iso8583", "host": "127.0.0.1", "port": 0},
        "downstream": {}, "kind": "responder", "proxy_config": None,
    }

    stop_a = asyncio.Event()
    host_a = asyncio.create_task(run_flow_host(
        bus, host_id="fh-a", advertise_host="a", stop_event=stop_a,
        claim_timeout_s=0.05))
    try:
        placed = await fleet.place("r10", [placement], timeout_s=5)
        assert placed[0]["worker_id"] == "fh-a"
    finally:
        # simulate sudden death: cancel the host so nothing retracts cleanly
        host_a.cancel()
        try:
            await host_a
        except asyncio.CancelledError:
            pass
    # its advertisement lingers until the TTL — force-expire it for the test
    await bus.retract_instance("r10", "r10-000")

    stop_b = asyncio.Event()
    host_b = asyncio.create_task(run_flow_host(
        bus, host_id="fh-b", advertise_host="b", stop_event=stop_b,
        claim_timeout_s=0.05))
    try:
        # the background poller (started by place→watch) may reconcile first —
        # either way exactly one re-placement lands on the queue (cooldown).
        await fleet.reconcile("r10")

        async def _up_on_b():
            rows = await bus.list_instances("r10")
            hit = [r for r in rows
                   if r.get("status") == "up" and r.get("worker_id") == "fh-b"]
            return hit or None

        recovered = await _wait_for(_up_on_b)
        assert recovered[0]["host"] == "b" and recovered[0]["port"]
        await fleet.stop("r10")
    finally:
        stop_b.set()
        await asyncio.wait_for(host_b, timeout=5)


async def test_fleet_traces_merge_into_network_trace(fleet_env, monkeypatch):
    """Item 8: hop records shipped by fleet hosts appear in the traces index
    and in the per-correlation stitched view, alongside local hops."""
    m.TOPOLOGY_RUNS["fr1"] = {"topology_id": "net", "kind": "network_flow",
                              "fleet": True, "run_id": "fr1",
                              "name": "net", "participants": [],
                              "instance_ids": ["fr1-000"]}
    await m.load_bus.report_flow_traces("fr1", [
        {"participant": "fr1-000", "worker_id": "fh-x", "correlation_id": "X",
         "flow_id": "issuer", "ts": 2.0, "ok": True, "calls": []},
        {"participant": "fr1-000", "worker_id": "fh-x", "correlation_id": "Y",
         "flow_id": "issuer", "ts": 3.0, "ok": False, "calls": []},
    ])

    class _Local:
        def traces(self, cid=None):
            recs = [{"correlation_id": "X", "flow_id": "switch", "ts": 1.0,
                     "ok": True, "calls": [{"target": "issuer"}]}]
            return [t for t in recs if cid is None or t["correlation_id"] == cid]

    m.PARTICIPANTS["switch"] = _Local()
    try:
        idx = await m.participant_traces()
        rows = {r["correlation_id"]: r for r in idx["transactions"]}
        assert rows["X"]["hops"] == 3          # local hop + its call + fleet hop
        assert sorted(rows["X"]["flows"]) == ["issuer", "switch"]
        assert rows["Y"]["ok"] is False        # fleet-only transaction indexed

        out = await m.participant_trace("X")
        assert [h["participant"] for h in out["hops"]] == ["switch", "fr1-000"]
        assert out["hops"][1]["worker_id"] == "fh-x"
    finally:
        m.PARTICIPANTS.clear()
        m.TOPOLOGY_RUNS.clear()
        await m.load_bus.clear_flow_traces("fr1")


async def test_ship_traces_watermarks_and_tags_records():
    """The host-side shipper: only records newer than the watermark ship, each
    tagged with the instance + worker; a second call re-ships nothing."""
    from worker.engine.load import InMemoryLoadBus
    from worker.flow_host import _Hosted, ship_traces

    class _Responder:
        def __init__(self):
            self._recs = [
                {"correlation_id": "X", "ts": 1.0, "flow_id": "iss"},
                {"correlation_id": "Y", "ts": 2.0, "flow_id": "iss"},
            ]

        def traces(self, cid=None):
            return list(self._recs)

    bus = InMemoryLoadBus()
    responder = _Responder()
    h = _Hosted("rt", {"instance_id": "rt-000"}, responder)

    assert await ship_traces(bus, "fh-t", h) == 2
    shipped = await bus.flow_traces("rt")
    assert [(s["correlation_id"], s["participant"], s["worker_id"])
            for s in shipped] == [("X", "rt-000", "fh-t"), ("Y", "rt-000", "fh-t")]

    # nothing new → nothing re-shipped
    assert await ship_traces(bus, "fh-t", h) == 0
    # a newer hop ships alone
    responder._recs.append({"correlation_id": "Z", "ts": 3.0, "flow_id": "iss"})
    assert await ship_traces(bus, "fh-t", h) == 1
    assert len(await bus.flow_traces("rt")) == 3


async def test_place_times_out_without_hosts():
    """Placement with nobody to claim it fails closed with a clear message."""
    from orchestrator.api.flow_fleet import FlowFleet
    from worker.engine.load import InMemoryLoadBus

    fleet = FlowFleet(InMemoryLoadBus())
    with pytest.raises(PlacementError) as ei:
        await fleet.place("r1", [{"instance_id": "r1-000", "flow_id": "f"}],
                          timeout_s=0.4)
    assert "never came up" in str(ei.value)
