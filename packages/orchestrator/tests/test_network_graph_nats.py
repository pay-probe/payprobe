"""Network-graph builder draws a broker-mediated edge for NATS (ADR-0006):
a producer targeting a subject is wired to whichever responder subscribes there,
even though NATS participants bind no port."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


class _FakeNats:
    """Minimal responder/participant surface the graph builder reads."""

    def __init__(self, label, subjects, *, flow_id=None, downstream=None):
        self.protocol = "nats"
        self.port = None
        self.subjects = subjects
        self.queue_group = "g"
        self.received = []
        self.by_rc = {}
        self.by_mti = {}
        self._label = label
        self._flow_id = flow_id
        self._flow_name = label
        self._topology_run = None
        self._downstream = downstream or {}
        self.flow = {}

    def peers(self):
        return []

    def listening(self):
        return True


async def _no_http(url):
    # connections / scenarios / groups lookups — empty is fine for this test
    return []


async def test_nats_subject_edge_participant_to_responder(monkeypatch):
    m.SIMULATORS.clear()
    m.PARTICIPANTS.clear()
    m.TOPOLOGY_RUNS.clear()
    m.RUNS.clear()

    # a NATS responder simulator subscribed to demo.auth (port-less)
    m.SIMULATORS["issuer"] = _FakeNats("NATS demo issuer", ["demo.auth"])
    # a participant whose downstream publishes to demo.auth via a nats connection
    m.PARTICIPANTS["p1"] = _FakeNats(
        "switch", [], flow_id="switch",
        downstream={"bus": {"adapter": "nats", "subject": "demo.auth"}})

    monkeypatch.setattr(m, "_http_get_json", _no_http)
    monkeypatch.setattr(m, "_load_groups_index", lambda: _empty_index())
    monkeypatch.setattr(m.simulator_store, "list", lambda: [])

    graph = await m.network_graph()
    ids = {n["id"] for n in graph["nodes"]}
    assert "sim:issuer" in ids and "pt:p1" in ids

    # the broker-mediated edge: participant -> responder, matched by subject
    edge = next((e for e in graph["edges"]
                 if e["source"] == "pt:p1" and e["target"] == "sim:issuer"), None)
    assert edge is not None, f"no subject edge drawn; edges={graph['edges']}"

    # the responder shows a NATS host-only style node? no — it's a real sim node,
    # NOT an unresolved 'host:bus' placeholder (which is what happened before).
    assert "host:bus" not in ids

    # port-less responder + participant both render as up, not degraded
    sim = next(n for n in graph["nodes"] if n["id"] == "sim:issuer")
    part = next(n for n in graph["nodes"] if n["id"] == "pt:p1")
    assert sim["status"] == "up" and part["status"] == "up"

    m.SIMULATORS.clear()
    m.PARTICIPANTS.clear()


async def _empty_index():
    return {}
