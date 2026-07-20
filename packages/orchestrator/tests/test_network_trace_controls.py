"""Network Trace: responder selection by adapter + trace-capture controls."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from worker.adapters.http.flow_responder import HttpFlowResponder  # noqa: E402
from worker.adapters.tcp.flow_responder import FlowResponder  # noqa: E402

SHELL_FLOW = {"id": "f", "nodes": [{"id": "t", "kind": "trigger"}], "edges": []}

ISSUER_FLOW = {
    "id": "iss",
    "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "r", "kind": "reply", "payload": {"echo": ["11"], "set": {"39": "00"}}},
    ],
    "edges": [{"source": "t", "source_port": "out", "target": "r"}],
}


def test_responder_selected_by_adapter():
    assert isinstance(
        m._build_flow_responder({"adapter": "http", "protocol": "http", "port": 0},
                                SHELL_FLOW, {}, "f"),
        HttpFlowResponder,
    )
    assert isinstance(
        m._build_flow_responder({"adapter": "tcp", "protocol": "iso8583", "port": 0},
                                SHELL_FLOW, {}, "f"),
        FlowResponder,
    )


def test_grpc_inbound_rejected_clearly():
    with pytest.raises(m.HTTPException) as ei:
        m._build_flow_responder({"adapter": "grpc", "protocol": "grpc", "port": 0},
                                SHELL_FLOW, {}, "f")
    assert ei.value.status_code == 400
    assert "gRPC" in ei.value.detail


async def test_capture_pause_resume_and_clear():
    m.PARTICIPANTS.clear()
    fr = FlowResponder({"protocol": "iso8583", "port": 0}, ISSUER_FLOW, run_id="iss")
    await fr._resolve_async({"mti": "0200", "de": {"11": "c1"}})
    m.PARTICIPANTS["p1"] = fr
    assert len(fr.traces("c1")) == 1

    # pause -> the flag propagates and new hops are NOT recorded
    res = await m.set_participants_capture(m.CaptureToggle(enabled=False))
    assert res["capturing"] is False and fr._capture is False
    await fr._resolve_async({"mti": "0200", "de": {"11": "c2"}})
    assert fr.traces("c2") == []
    assert (await m.get_participants_capture())["capturing"] is False

    # clear empties the buffer without touching the listener
    await m.clear_traces()
    assert fr.traces() == []

    # resume -> recording works again
    res = await m.set_participants_capture(m.CaptureToggle(enabled=True))
    assert res["capturing"] is True
    await fr._resolve_async({"mti": "0200", "de": {"11": "c3"}})
    assert len(fr.traces("c3")) == 1

    m.PARTICIPANTS.clear()
    m._CAPTURE_ENABLED = False  # back to the default-stopped state


async def test_relay_flow_launches_a_proxy(monkeypatch):
    from worker.adapters.tcp.proxy import TcpProxy

    async def fake_get(url):
        if url.endswith("/connections/cn_hsm_8000"):
            return {"adapter": "payshield", "protocol": "iso8583",
                    "host": "127.0.0.1", "port": 8000, "name": "cn_hsm_8000"}
        raise AssertionError(url)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    listen = {"protocol": "iso8583", "adapter": "payshield",
              "host": "127.0.0.1", "port": 9001}

    decode_flow = {"id": "r1", "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "p", "kind": "relay", "config": {"connection": "cn_hsm_8000", "mode": "decode"}},
    ], "edges": []}
    relay = m._relay_node(decode_flow)
    assert relay is not None and relay["id"] == "p"
    px = await m._build_relay_proxy(decode_flow, listen, relay)
    assert isinstance(px, TcpProxy)
    assert (px.up_host, px.up_port) == ("127.0.0.1", 8000)
    assert px.decode_frames is True            # decode mode keeps per-command visibility

    raw_flow = {"id": "r2", "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "p", "kind": "proxy", "config": {"connection": "cn_hsm_8000", "mode": "raw"}},
    ], "edges": []}
    px2 = await m._build_relay_proxy(raw_flow, listen, m._relay_node(raw_flow))
    assert px2.decode_frames is False          # raw mode = blind byte-exact


async def test_relay_to_group_fans_out_to_members(monkeypatch):
    """A relay whose upstream is a participant GROUP forwards across the group's
    member connections (balanced by the group's selection policy)."""
    async def fake_get(url):
        if url.endswith("/connections/hsm-group"):
            raise _NotAConn()                       # not a plain connection
        if url.endswith("/connections/cn_hsm_8000"):
            return {"host": "127.0.0.1", "port": 8000, "adapter": "payshield"}
        if url.endswith("/connections/cn_hsm_8001"):
            return {"host": "127.0.0.1", "port": 8001, "adapter": "payshield"}
        raise AssertionError(url)

    async def fake_groups():
        return {"hsm-group": {"name": "hsm-group", "selection": {"policy": "round_robin"},
                              "members": [{"connection": "cn_hsm_8000"},
                                          {"connection": "cn_hsm_8001"}]}}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    monkeypatch.setattr(m, "_load_groups_index", fake_groups)
    flow = {"id": "r4", "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "p", "kind": "relay", "config": {"connection": "hsm-group", "mode": "decode"}},
    ], "edges": []}
    px = await m._build_relay_proxy(flow, {"host": "127.0.0.1", "port": 9001}, m._relay_node(flow))
    assert sorted(px._upstreams) == [("127.0.0.1", 8000), ("127.0.0.1", 8001)]
    # round-robin across the two members
    assert {px._pick_upstream() for _ in range(4)} == {("127.0.0.1", 8000), ("127.0.0.1", 8001)}


class _NotAConn(Exception):
    pass


async def test_relay_upstream_surfaces_as_downstream_for_graph(monkeypatch):
    """A relay's upstream group is resolved into the participant's ``_downstream``
    so the network graph draws the proxy → group → members edge (like an action
    node). Mirrors what _launch_participant computes for a relay flow."""
    async def fake_get(url):
        if url.endswith("/connections/hsm-group"):
            raise _NotAConn()
        if url.endswith("/connections/cn_hsm_8000"):
            return {"adapter": "payshield", "host": "127.0.0.1", "port": 8000}
        if url.endswith("/connections/cn_hsm_8001"):
            return {"adapter": "payshield", "host": "127.0.0.1", "port": 8001}
        raise AssertionError(url)

    async def fake_groups():
        return {"hsm-group": {"name": "hsm-group", "selection": {"policy": "round_robin"},
                              "members": [{"connection": "cn_hsm_8000"},
                                          {"connection": "cn_hsm_8001"}]}}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    monkeypatch.setattr(m, "_load_groups_index", fake_groups)
    ds = await m._participant_downstream({"nodes": [{"kind": "action", "target": "hsm-group"}]})
    assert ds["hsm-group"]["adapter"] == "group"
    ports = {mm["config"].get("port") for mm in ds["hsm-group"]["members"]}
    assert ports == {8000, 8001}


async def test_relay_node_without_upstream_is_rejected(monkeypatch):
    async def fake_get(url):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    flow = {"id": "r3", "nodes": [
        {"id": "t", "kind": "trigger"},
        {"id": "p", "kind": "relay", "config": {}},  # no connection / upstream
    ], "edges": []}
    with pytest.raises(m.HTTPException) as ei:
        await m._build_relay_proxy(flow, {"host": "127.0.0.1", "port": 9001}, m._relay_node(flow))
    assert ei.value.status_code == 400


async def test_capture_defaults_to_stopped():
    """Capture starts in the stopped state — the user resumes to collect a clean
    window rather than the buffer churning under load by default."""
    assert m._CAPTURE_ENABLED is False
    assert (await m.get_participants_capture())["capturing"] is False


async def test_trace_index_tags_kind():
    m.PARTICIPANTS.clear()
    m._CAPTURE_ENABLED = True
    http_flow = {
        "id": "pay",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "r", "kind": "reply", "payload": {"status": 200, "json": {"ok": True}}},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "r"}],
    }
    hr = HttpFlowResponder({"protocol": "http", "port": 0}, http_flow, run_id="pay")
    hr._capture = True
    await hr._resolve_async({"method": "POST", "path": "/pay", "query": {},
                             "headers": {"x-correlation-id": "h1"}, "body": {}})
    m.PARTICIPANTS["p1"] = hr
    idx = await m.participant_traces()
    row = next(t for t in idx["transactions"] if t["correlation_id"] == "h1")
    assert row["kind"] == "http" and row["mti"] == "POST /pay"
    m.PARTICIPANTS.clear()
