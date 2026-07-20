"""Topology-run machinery that outlived the /topologies alias (ADR-0004):
run stop/health, the requires gate, ownership guard, runtime persistence
(incl. legacy state files) and trace aggregation. Start-path coverage lives in
test_network_flow_runs.py."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


_FLOW_NODES = [{"id": "t", "kind": "trigger"},
               {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}}]
_FLOW_EDGES = [{"source": "t", "source_port": "out", "target": "r"}]


async def test_stop_unknown_topology_run_is_404(monkeypatch):
    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException) as ei:
        await m.stop_topology("nope")
    assert ei.value.status_code == 404


async def test_requires_topology_gate(monkeypatch):
    """A run that requires a network is rejected (424) until it's up."""
    from fastapi import HTTPException
    import pytest

    m.TOPOLOGY_RUNS.clear()
    req = m.CreateRunRequest(scenarios=[{"id": "sc", "steps": []}], requires_topology="net")

    # not up -> 424
    with pytest.raises(HTTPException) as ei:
        await m.create_run(req)
    assert ei.value.status_code == 424

    # a run record with NO live listeners is not "up" — readiness, not a record
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS["r1"] = {"topology_id": "net", "name": "net", "participants": []}
    assert m._topology_is_up("net") is False

    # a run whose listeners are all bound -> gate passes
    class _Live:
        port = 9999

    m.PARTICIPANTS["p1"] = _Live()
    m.TOPOLOGY_RUNS["r1"] = {"topology_id": "net", "name": "net", "participants": ["p1"]}
    assert m._topology_is_up("net") is True

    # a listener that dropped (no bound port) makes the network not-ready
    class _Dead:
        port = None

    m.PARTICIPANTS["p1"] = _Dead()
    assert m._topology_is_up("net") is False

    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()


async def test_network_ownership_and_standalone_guard(monkeypatch):
    """Network-run instances are tagged as owned + expose endpoints, and
    starting the same flow standalone while the network is live is 409."""
    from fastapi import HTTPException
    import pytest

    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    net = {"id": "net4", "name": "net4"}
    plan = {"participants": [
        {"node_id": "issuer", "flow_id": "iss", "instances": 2, "port": None},
    ], "initiators": [], "simulators": [], "warnings": []}
    flow = {"id": "iss", "trigger": {"connection": "iss_in"},
            "nodes": _FLOW_NODES, "edges": _FLOW_EDGES}
    conn = {"name": "iss_in", "protocol": "iso8583", "mode": "inbound",
            "host": "127.0.0.1", "listen_port": 8600}

    async def fake_get(url):
        if url.endswith("/network-flows/net4/plan"):
            return plan
        if url.endswith("/network-flows/net4"):
            return net
        if url.endswith("/participant-flows/iss"):
            return flow
        if url.endswith("/connections/iss_in"):
            return conn
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    started = await m.start_network_flow("net4")
    try:
        # every instance is tagged as owned by this run and advertises an endpoint
        parts = await m.list_participants()
        assert len(parts) == 2
        assert all(p["owner"] == started["id"] for p in parts)
        assert all(p["endpoint"] for p in parts)
        # the run lists its bound endpoints
        runs = await m.list_topology_runs()
        assert len(runs[0]["endpoints"]) == 2

        # starting the same flow standalone is rejected while the network is up
        with pytest.raises(HTTPException) as ei:
            await m.start_participant(m.StartParticipant(flow_id="iss"))
        assert ei.value.status_code == 409
    finally:
        await m.stop_topology(started["id"])
    # once the network is stopped the flow is no longer owned
    assert m._flow_in_running_topology("iss") is None


def test_persist_runtime_snapshots_standalone_and_networks(monkeypatch, tmp_path):
    """The desired-state file captures standalone flow ids + running network ids."""
    import json as _json

    f = tmp_path / "rt.json"
    monkeypatch.setattr(m, "RUNTIME_STATE_FILE", str(f))
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()

    class _Standalone:
        _flow_id = "iss"
        _topology_run = None
        port = 8500
        config = {"host": "127.0.0.1"}

    class _Owned:
        _flow_id = "switch"
        _topology_run = "r1"
        port = 8101
        config = {"host": "127.0.0.1"}

    m.PARTICIPANTS["p1"] = _Standalone()
    m.PARTICIPANTS["p2"] = _Owned()
    m.TOPOLOGY_RUNS["r1"] = {"topology_id": "net", "kind": "network_flow",
                             "name": "net", "participants": ["p2"]}

    m._persist_runtime()
    data = _json.loads(f.read_text())
    assert data["standalone"] == ["iss"]        # owned participant excluded
    assert data["network_flows"] == ["net"]
    assert "topologies" not in data             # legacy key no longer written
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()


async def test_autostart_runtime_restores_legacy_topology_state(monkeypatch, tmp_path):
    """A pre-ADR-0004 state file ("topologies": [...]) restores through
    start_network_flow — migration reused the ids, so they resolve."""
    import json as _json

    f = tmp_path / "rt.json"
    f.write_text(_json.dumps({
        "standalone": ["hsm_stub"],
        "topologies": ["legacy-net"],
        "network_flows": ["new-net", "legacy-net"],  # dupe → started once
    }))
    monkeypatch.setattr(m, "RUNTIME_STATE_FILE", str(f))
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()

    calls = {"net": [], "flow": []}

    async def fake_start_network_flow(nid):
        calls["net"].append(nid)
        return {"id": "x"}

    async def fake_launch(fid, *a, **k):
        calls["flow"].append(fid)
        return {"id": "y"}

    monkeypatch.setattr(m, "start_network_flow", fake_start_network_flow)
    monkeypatch.setattr(m, "_launch_participant", fake_launch)

    await m._autostart_runtime()
    assert calls["net"] == ["new-net", "legacy-net"]
    assert calls["flow"] == ["hsm_stub"]


async def test_participant_trace_aggregates_hops_across_listeners():
    """One correlation id, stitched in time order across every live listener."""
    m.PARTICIPANTS.clear()

    class _P:
        def __init__(self, recs):
            self._recs = recs

        def traces(self, cid):
            return [t for t in self._recs if t["correlation_id"] == cid]

    m.PARTICIPANTS["switch"] = _P([
        {"correlation_id": "X", "flow_id": "switch", "ts": 2.0,
         "calls": [{"target": "issuer"}]},
    ])
    m.PARTICIPANTS["issuer"] = _P([
        {"correlation_id": "X", "flow_id": "issuer", "ts": 1.0, "calls": []},
        {"correlation_id": "Y", "flow_id": "issuer", "ts": 3.0, "calls": []},
    ])

    out = await m.participant_trace("X")
    assert out["correlation_id"] == "X"
    assert [h["participant"] for h in out["hops"]] == ["issuer", "switch"]  # by ts
    assert all(h["correlation_id"] == "X" for h in out["hops"])
    assert out["hops"][1]["calls"][0]["target"] == "issuer"
    m.PARTICIPANTS.clear()
