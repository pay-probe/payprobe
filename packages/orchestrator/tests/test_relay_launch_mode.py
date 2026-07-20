"""Relay launch gating: a TERMINAL relay (no outgoing wire) makes the flow a
transparent TcpProxy; a relay wired onward (e.g. to a reply) runs graph-computed
so ``${<relay_id>.response}`` is available to following nodes."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


def _flow(edges):
    return {
        "id": "rf",
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "fwd", "kind": "relay", "config": {"connection": "up"}},
            {"id": "r", "kind": "reply", "payload": "${fwd.response}"},
        ],
        "edges": edges,
    }


def test_terminal_relay_is_transparent_proxy():
    flow = _flow([{"source": "t", "source_port": "out", "target": "fwd"}])
    relay = m._relay_node(flow)
    assert relay is not None
    assert m._relay_is_terminal(flow, relay) is True


def test_wired_relay_runs_graph_computed():
    flow = _flow(
        [
            {"source": "t", "source_port": "out", "target": "fwd"},
            {"source": "fwd", "source_port": "out", "target": "r"},
        ]
    )
    relay = m._relay_node(flow)
    assert relay is not None
    assert m._relay_is_terminal(flow, relay) is False


def test_flow_without_relay_has_no_relay_node():
    assert m._relay_node({"nodes": [{"id": "t", "kind": "trigger"}], "edges": []}) is None


async def test_payshield_listener_normalized_to_header_echo(monkeypatch):
    """A payShield trigger connection must yield a header_echo listener (as the
    transparent-proxy branch already forces) — otherwise the graph-computed
    relay path would iso8583-decode HSM host commands and answer garbage."""
    async def fake_get(url):
        return {"name": "cn_listener_hsm_9001", "adapter": "payshield",
                "mode": "inbound", "host": "127.0.0.1", "port": 9001}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    cfg = await m._participant_listen_config(
        {"trigger": {"connection": "cn_listener_hsm_9001"}}, None
    )
    assert cfg["protocol"] == "header_echo"
    assert cfg["header_bytes"] == 4
    assert cfg["port"] == 9001
