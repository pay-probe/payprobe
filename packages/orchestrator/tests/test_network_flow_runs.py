"""Network-flow runs (ADR-0004): start callees-first from the plan, initiators
fire/cancel, persistence splits kinds, and the requires gate accepts them."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from fastapi import HTTPException, Response  # noqa: E402
import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402


_FLOW_NODES = [{"id": "t", "kind": "trigger"},
               {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}}]
_FLOW_EDGES = [{"source": "t", "source_port": "out", "target": "r"}]


class _FakeNatsSub:
    async def unsubscribe(self):
        pass


class _FakeNatsClient:
    """Minimal NATS client so a NATS participant can 'start' (subscribe) without
    a real broker — mirrors the subset NatsResponder.start()/stop() touch."""

    async def subscribe(self, subject, queue="", cb=None):
        return _FakeNatsSub()

    async def publish(self, subject, data, **kw):
        pass

    async def drain(self):
        pass

    async def close(self):
        pass


def _patch_nats(monkeypatch):
    import worker.adapters.nats.responder as nr

    async def fake_connect(servers, **opts):
        return _FakeNatsClient()

    monkeypatch.setattr(nr, "_nats_connect", fake_connect)


def _flow(fid, connection=""):
    return {"id": fid, "trigger": {"connection": connection},
            "nodes": _FLOW_NODES, "edges": _FLOW_EDGES}


def _fake_get_for(net, plan, flows, conns=None):
    nid = net["id"]

    async def fake_get(url):
        if url.endswith(f"/network-flows/{nid}/plan"):
            return plan
        if url.endswith(f"/network-flows/{nid}"):
            return net
        for fid, doc in flows.items():
            if url.endswith(f"/participant-flows/{fid}"):
                return doc
        for cname, doc in (conns or {}).items():
            if url.endswith(f"/connections/{cname}"):
                return doc
        raise AssertionError(f"unexpected fetch: {url}")

    return fake_get


async def test_start_list_stop_network_flow(monkeypatch):
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nf1", "name": "acquiring net"}
    plan = {"participants": [
        {"node_id": "issuer", "flow_id": "iss", "instances": 1, "port": None},
        {"node_id": "switch", "flow_id": "sw", "instances": 1, "port": None},
    ], "initiators": [], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"iss": _flow("iss"), "sw": _flow("sw")}))

    started = await m.start_network_flow("nf1")
    rid = started["id"]
    assert started["kind"] == "network_flow"
    assert started["network_flow_id"] == "nf1"
    # plan order preserved: issuer (callee) launched before switch
    assert [p["flow_id"] for p in started["participants"]] == ["iss", "sw"]
    assert all(p["port"] for p in started["participants"])
    assert len(m.PARTICIPANTS) == 2

    runs = await m.list_topology_runs()
    row = next(r for r in runs if r["id"] == rid)
    assert row["kind"] == "network_flow" and row["topology_id"] == "nf1"

    resp = await m.stop_topology(rid)
    assert isinstance(resp, Response) and resp.status_code == 204
    assert len(m.PARTICIPANTS) == 0


async def test_network_flow_node_port_override_and_instances(monkeypatch):
    """A participant node's explicit port overrides the connection's
    listen_port as the base; instances get base, base+1, …"""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nf2", "name": "nf2"}
    plan = {"participants": [
        {"node_id": "issuer", "flow_id": "iss", "instances": 3, "port": 8700},
    ], "initiators": [], "warnings": []}
    conn = {"name": "iss_in", "protocol": "iso8583", "mode": "inbound",
            "host": "127.0.0.1", "listen_port": 8400}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"iss": _flow("iss", "iss_in")}, {"iss_in": conn}))

    started = await m.start_network_flow("nf2")
    try:
        ports = sorted(p["port"] for p in started["participants"])
        assert ports == [8700, 8701, 8702]  # override, not the connection's 8400
        assert started["health"] == {"total": 3, "live": 3, "ready": True}
    finally:
        await m.stop_topology(started["id"])


async def test_network_flow_port_collision_is_409(monkeypatch):
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nf3", "name": "nf3"}
    plan = {"participants": [
        {"node_id": "a", "flow_id": "a", "instances": 1, "port": 8800},
        {"node_id": "b", "flow_id": "b", "instances": 1, "port": 8800},
    ], "initiators": [], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"a": _flow("a"), "b": _flow("b")}))

    with pytest.raises(HTTPException) as ei:
        await m.start_network_flow("nf3")
    assert ei.value.status_code == 409
    assert len(m.PARTICIPANTS) == 0  # caught in planning, nothing bound


async def test_network_flow_nats_participants_are_portless(monkeypatch):
    """A NATS participant claims a subject, not a port (ADR-0006): instances plan
    with port=None and start without binding. Distinct subjects don't collide."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nfn1", "name": "event net"}
    plan = {"participants": [
        {"node_id": "auth", "flow_id": "iss", "instances": 1, "port": None},
        {"node_id": "rev", "flow_id": "sw", "instances": 1, "port": None},
    ], "initiators": [], "warnings": []}
    conns = {
        "auth_in": {"name": "auth_in", "adapter": "nats", "mode": "inbound",
                    "host": "nats-1", "port": 4222, "subject": "pay.auth"},
        "rev_in": {"name": "rev_in", "adapter": "nats", "mode": "inbound",
                   "host": "nats-1", "port": 4222, "subject": "pay.reversal"},
    }
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"iss": _flow("iss", "auth_in"), "sw": _flow("sw", "rev_in")},
        conns))
    _patch_nats(monkeypatch)

    started = await m.start_network_flow("nfn1")
    try:
        # both participants are up, and neither bound a port
        assert all(p["port"] is None for p in started["participants"])
        assert len(m.PARTICIPANTS) == 2
    finally:
        await m.stop_topology(started["id"])
    assert len(m.PARTICIPANTS) == 0


def test_run_health_counts_portless_nats_pieces():
    """A port-less NATS responder (ADR-0006) counts toward readiness by its
    subscription liveness, not a port — so a NATS-only network shows ready."""
    m.PARTICIPANTS.clear()
    m.SIMULATORS.clear()

    class _NatsLive:
        port = None
        subjects = ["pay.auth"]
        queue_group = "issuers"

        def listening(self):
            return True

    class _NatsDown(_NatsLive):
        def listening(self):
            return False

    m.SIMULATORS["sim-live"] = _NatsLive()
    run = {"participants": [], "simulator_ids": ["sim-live"]}
    assert m._run_health(run) == {"total": 1, "live": 1, "ready": True}
    # a live port-less responder shows a subject endpoint (not host:port)
    eps = m._run_endpoints(run)
    assert eps == [{"participant": "sim-live", "flow_id": "sim-live",
                    "endpoint": "nats:pay.auth@issuers"}]

    # a disconnected responder is not counted live
    m.SIMULATORS["sim-live"] = _NatsDown()
    assert m._run_health(run)["ready"] is False
    m.SIMULATORS.clear()


async def test_nats_same_subject_no_queue_group_is_409(monkeypatch):
    """Two participants subscribing to the same subject with NO queue group would
    both receive every message — a subject collision, 409 at plan time."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nfn2", "name": "nfn2"}
    plan = {"participants": [
        {"node_id": "a", "flow_id": "a", "instances": 1, "port": None},
        {"node_id": "b", "flow_id": "b", "instances": 1, "port": None},
    ], "initiators": [], "warnings": []}
    conn = {"name": "shared", "adapter": "nats", "mode": "inbound",
            "host": "nats-1", "port": 4222, "subject": "pay.auth"}  # no queue_group
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"a": _flow("a", "shared"), "b": _flow("b", "shared")},
        {"shared": conn}))

    with pytest.raises(HTTPException) as ei:
        await m.start_network_flow("nfn2")
    assert ei.value.status_code == 409
    assert "subject collision" in str(ei.value.detail)
    assert len(m.PARTICIPANTS) == 0


async def test_nats_queue_group_fan_out_is_legal(monkeypatch):
    """Multiple instances sharing a subject + queue group is legal fan-out — no
    collision (that is exactly what queue groups are for)."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nfn3", "name": "nfn3"}
    plan = {"participants": [
        {"node_id": "iss", "flow_id": "iss", "instances": 3, "port": None},
    ], "initiators": [], "warnings": []}
    conn = {"name": "q_in", "adapter": "nats", "mode": "inbound",
            "host": "nats-1", "port": 4222, "subject": "pay.auth",
            "queue_group": "issuers"}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"iss": _flow("iss", "q_in")}, {"q_in": conn}))
    _patch_nats(monkeypatch)

    started = await m.start_network_flow("nfn3")
    try:
        # all three fan-out instances came up, none bound a port
        assert len(started["participants"]) == 3
        assert all(p["port"] is None for p in started["participants"])
    finally:
        await m.stop_topology(started["id"])


async def test_network_flow_initiators_run_and_cancel(monkeypatch):
    """Every autostart scenario node fires after the listeners are up; all of
    them are cancelled when the network stops; autostart=False is skipped."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nf4", "name": "nf4"}
    plan = {"participants": [
        {"node_id": "issuer", "flow_id": "iss", "instances": 1, "port": None},
    ], "initiators": [
        {"node_id": "d1", "scenario_id": "drive-1", "environment": "mock",
         "autostart": True},
        {"node_id": "d2", "scenario_id": "drive-2", "environment": None,
         "autostart": True},
        {"node_id": "d3", "scenario_id": "drive-3", "environment": None,
         "autostart": False},
    ], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"iss": _flow("iss")}))

    created, cancelled = [], []

    async def fake_create_run(req):
        created.append(req)
        return m.CreateRunResponse(run_id=f"run-{len(created)}", status="running")

    async def fake_cancel_run(rid):
        cancelled.append(rid)
        return {"status": "cancelled"}

    monkeypatch.setattr(m, "create_run", fake_create_run)
    monkeypatch.setattr(m, "cancel_run", fake_cancel_run)

    started = await m.start_network_flow("nf4")
    assert started["initiator_runs"] == ["run-1", "run-2"]
    assert [r.scenario_ids for r in created] == [["drive-1"], ["drive-2"]]
    assert created[0].environment_name == "mock"

    await m.stop_topology(started["id"])
    assert sorted(cancelled) == ["run-1", "run-2"]  # every driver cancelled
    m.PARTICIPANTS.clear()


async def test_network_flow_starts_and_stops_its_simulators(monkeypatch):
    """`simulator` nodes bring up saved simulators before the participants and
    stop them with the run — but never a simulator that was already running."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    m.SIMULATORS.clear()
    net = {"id": "nf5", "name": "nf5"}
    plan = {"participants": [
        {"node_id": "switch", "flow_id": "sw", "instances": 1, "port": None},
    ], "initiators": [], "simulators": [
        {"node_id": "hsm", "simulator_id": "sim-hsm"},
        {"node_id": "visa", "simulator_id": "sim-visa"},
    ], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(
        net, plan, {"sw": _flow("sw")}))

    saved = {
        "sim-hsm": {"id": "sim-hsm", "label": "HSM", "config": {}},
        "sim-visa": {"id": "sim-visa", "label": "VISA", "config": {}},
    }
    monkeypatch.setattr(m.simulator_store, "get", lambda sid: saved.get(sid))

    class _FakeSim:
        _label = "fake"
        port = 9999
        stopped = False

        async def stop(self):
            self.stopped = True

    launched = []

    async def fake_start_responder(sid, label, config):
        launched.append(sid)
        sim = _FakeSim()
        m.SIMULATORS[sid] = sim
        return sim

    monkeypatch.setattr(m, "_start_responder", fake_start_responder)
    monkeypatch.setattr(m, "_drop_sim_runtime", lambda sid, label: None)

    # sim-visa is ALREADY running (e.g. autostarted) — must not be re-started…
    pre_existing = _FakeSim()
    m.SIMULATORS["sim-visa"] = pre_existing

    started = await m.start_network_flow("nf5")
    assert launched == ["sim-hsm"]          # only the missing one was started
    assert started["simulators"] == ["sim-hsm"]
    # health counts BOTH simulators (started or found running) + the participant
    assert started["health"] == {"total": 3, "live": 3, "ready": True}

    await m.stop_topology(started["id"])
    # …and must not be stopped with the run either
    assert "sim-visa" in m.SIMULATORS and not pre_existing.stopped
    assert "sim-hsm" not in m.SIMULATORS    # ours was stopped + deregistered
    m.SIMULATORS.clear()
    m.PARTICIPANTS.clear()


async def test_simulator_only_network_health(monkeypatch):
    """A network of only simulator nodes is healthy when its simulators are up
    (regression: showed 'running 0/0' because health counted participants only)
    and degrades when one drops."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    m.SIMULATORS.clear()
    net = {"id": "nf7", "name": "HSM Simulators"}
    plan = {"participants": [], "initiators": [], "simulators": [
        {"node_id": "hsm-a", "simulator_id": "hsm-8000"},
        {"node_id": "hsm-b", "simulator_id": "hsm-8001"},
    ], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(net, plan, {}))
    saved = {"hsm-8000": {"id": "hsm-8000", "label": "HSM A", "config": {}},
             "hsm-8001": {"id": "hsm-8001", "label": "HSM B", "config": {}}}
    monkeypatch.setattr(m.simulator_store, "get", lambda sid: saved.get(sid))

    class _FakeSim:
        _label = "fake"
        port = 9999

        async def stop(self):
            pass

    async def fake_start_responder(sid, label, config):
        sim = _FakeSim()
        m.SIMULATORS[sid] = sim
        return sim

    monkeypatch.setattr(m, "_start_responder", fake_start_responder)
    monkeypatch.setattr(m, "_drop_sim_runtime", lambda sid, label: None)

    started = await m.start_network_flow("nf7")
    assert started["health"] == {"total": 2, "live": 2, "ready": True}
    assert m._topology_is_up("nf7") is True
    # both sims appear as endpoints
    runs = await m.list_topology_runs()
    assert len(runs[0]["endpoints"]) == 2

    # one simulator drops → degraded, gate closes
    m.SIMULATORS.pop("hsm-8001")
    run = m.TOPOLOGY_RUNS[started["id"]]
    assert m._run_health(run) == {"total": 2, "live": 1, "ready": False}
    assert m._topology_is_up("nf7") is False

    await m.stop_topology(started["id"])
    m.SIMULATORS.clear()


async def test_network_flow_unknown_simulator_is_404(monkeypatch):
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "nf6", "name": "nf6"}
    plan = {"participants": [], "initiators": [], "simulators": [
        {"node_id": "hsm", "simulator_id": "ghost"},
    ], "warnings": []}
    monkeypatch.setattr(m, "_http_get_json", _fake_get_for(net, plan, {}))
    monkeypatch.setattr(m.simulator_store, "get", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await m.start_network_flow("nf6")
    assert ei.value.status_code == 404
    assert "ghost" in str(ei.value.detail)


async def test_requires_gate_accepts_network_flow_runs(monkeypatch):
    """`requires_topology` names an id — a healthy *network-flow* run of that id
    satisfies the gate (migration reuses topology ids on purpose)."""
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()

    class _Live:
        port = 9999

    m.PARTICIPANTS["p1"] = _Live()
    m.TOPOLOGY_RUNS["r1"] = {"topology_id": "acq-net", "kind": "network_flow",
                             "name": "acq", "participants": ["p1"]}
    assert m._topology_is_up("acq-net") is True
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()


def test_persist_runtime_writes_network_flows_only(monkeypatch, tmp_path):
    """Every run persists under "network_flows" — the legacy "topologies" key
    is no longer written (but still read on boot, see test_topology.py)."""
    import json as _json

    f = tmp_path / "rt.json"
    monkeypatch.setattr(m, "RUNTIME_STATE_FILE", str(f))
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    m.TOPOLOGY_RUNS["r2"] = {"topology_id": "nf", "kind": "network_flow",
                             "name": "n", "participants": []}

    m._persist_runtime()
    data = _json.loads(f.read_text())
    assert data["network_flows"] == ["nf"]
    assert "topologies" not in data
    m.TOPOLOGY_RUNS.clear()


async def test_autostart_runtime_relaunches_network_flows(monkeypatch, tmp_path):
    import json as _json

    f = tmp_path / "rt.json"
    f.write_text(_json.dumps(
        {"standalone": [], "topologies": [], "network_flows": ["nf"]}))
    monkeypatch.setattr(m, "RUNTIME_STATE_FILE", str(f))
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()

    calls = []

    async def fake_start_network_flow(nid):
        calls.append(nid)
        return {"id": "x"}

    monkeypatch.setattr(m, "start_network_flow", fake_start_network_flow)

    await m._autostart_runtime()
    assert calls == ["nf"]
