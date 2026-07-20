"""Network flows (ADR-0004): model validation, start-order planning, store,
topology migration, and participant-flow versioning (Phase 0)."""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.network_flow_store import NetworkFlowStore, topology_to_network_flow
from api.participant_flow_store import ParticipantFlowStore
from models.network_flow import (
    NetworkFlowDraft,
    plan_network_flow,
    validate_network_flow,
)
from models.participant_flow import ParticipantFlowDraft, FlowTrigger
from models.scenario import Edge, Step, validate_scenario, ScenarioDraft
from models.topology import Topology, TopologyInitiator, TopologyParticipant


def _participant(nid, flow_id=None, **cfg):
    return Step(id=nid, kind="participant",
                config={"flow_id": flow_id or nid, **cfg})


def _scenario_node(nid, scenario_id="scn-1", **cfg):
    return Step(id=nid, kind="scenario",
                config={"scenario_id": scenario_id, **cfg})


# -- validation ---------------------------------------------------------------

def test_valid_network_flow_passes():
    d = NetworkFlowDraft(
        name="acquiring net",
        nodes=[_participant("hsm"), _participant("issuer"), _participant("switch"),
               _scenario_node("driver")],
        edges=[Edge(source="switch", target="issuer"),
               Edge(source="switch", target="hsm"),
               Edge(source="driver", target="switch")],
    )
    result = validate_network_flow(d)
    assert result.valid, [i.message for i in result.issues]


def test_participant_needs_flow_id_and_positive_instances():
    d = NetworkFlowDraft(name="bad", nodes=[
        Step(id="a", kind="participant", config={}),
        Step(id="b", kind="participant", config={"flow_id": "f", "instances": 0}),
    ])
    msgs = [i.message for i in validate_network_flow(d).issues]
    assert any("needs a flow_id" in m for m in msgs)
    assert any("positive integer" in m for m in msgs)


def test_scenario_node_needs_scenario_id_and_rejects_inbound_edges():
    d = NetworkFlowDraft(
        name="bad",
        nodes=[_participant("p"), Step(id="s", kind="scenario", config={})],
        edges=[Edge(source="p", target="s")],
    )
    msgs = [i.message for i in validate_network_flow(d).issues]
    assert any("needs a scenario_id" in m for m in msgs)
    assert any("initiators drive traffic" in m for m in msgs)


def test_foreign_kinds_rejected_on_network_canvas():
    d = NetworkFlowDraft(name="bad", nodes=[Step(id="x", kind="action")])
    msgs = [i.message for i in validate_network_flow(d).issues]
    assert any("not allowed on a network canvas" in m for m in msgs)


def test_network_kinds_rejected_inside_scenarios():
    result = validate_scenario(ScenarioDraft(
        name="s", steps=[Step(id="p", kind="participant")]))
    assert not result.valid
    assert any("network-flow" in i.message for i in result.issues)


def test_cycle_is_warning_not_error():
    d = NetworkFlowDraft(
        name="loopy",
        nodes=[_participant("a"), _participant("b")],
        edges=[Edge(source="a", target="b"), Edge(source="b", target="a")],
    )
    result = validate_network_flow(d)
    assert result.valid
    assert any("cycle" in i.message for i in result.issues)


# -- planning (start order) ----------------------------------------------------

def test_plan_orders_callees_first():
    # driver → switch → {issuer, hsm}: hsm/issuer must start before switch.
    d = NetworkFlowDraft(
        name="net",
        nodes=[_participant("switch"), _participant("issuer"), _participant("hsm"),
               _scenario_node("driver", environment="local")],
        edges=[Edge(source="switch", target="issuer"),
               Edge(source="switch", target="hsm"),
               Edge(source="driver", target="switch")],
    )
    plan = plan_network_flow(d)
    order = [p["node_id"] for p in plan.participants]
    assert order.index("issuer") < order.index("switch")
    assert order.index("hsm") < order.index("switch")
    assert plan.initiators == [{
        "node_id": "driver", "scenario_id": "scn-1",
        "environment": "local", "autostart": True,
    }]
    assert not plan.warnings


def test_plan_without_edges_keeps_node_list_order():
    d = NetworkFlowDraft(
        name="legacy order",
        nodes=[_participant("hsm"), _participant("issuer"), _participant("switch")],
    )
    assert [p["node_id"] for p in plan_network_flow(d).participants] == \
        ["hsm", "issuer", "switch"]


def test_plan_cycle_falls_back_to_list_order_with_warning():
    d = NetworkFlowDraft(
        name="loopy",
        nodes=[_participant("a"), _participant("b"), _participant("c")],
        edges=[Edge(source="a", target="b"), Edge(source="b", target="a"),
               Edge(source="a", target="c")],
    )
    plan = plan_network_flow(d)
    order = [p["node_id"] for p in plan.participants]
    assert order[0] == "c"          # the acyclic callee still sorts first
    assert order[1:] == ["a", "b"]  # cyclic part keeps list order
    assert plan.warnings and "cycle" in plan.warnings[0]


def test_plan_carries_instances_and_port():
    d = NetworkFlowDraft(name="net", nodes=[
        _participant("issuer", instances=3, port=9100)])
    p = plan_network_flow(d).participants[0]
    assert (p["instances"], p["port"]) == (3, 9100)


# -- topology migration ---------------------------------------------------------

def _legacy_topology():
    return Topology(
        id="acq-net", name="Acquiring net", description="legacy",
        participants=[
            TopologyParticipant(flow_id="hsm", instances=2),
            TopologyParticipant(flow_id="issuer"),
            TopologyParticipant(flow_id="hsm"),  # duplicate flow → unique node id
        ],
        initiator=TopologyInitiator(scenario_id="scn-9", environment="local"),
    )


def test_topology_converts_to_network_flow():
    draft = topology_to_network_flow(_legacy_topology())
    kinds = [n.kind for n in draft.nodes]
    assert kinds == ["participant", "participant", "participant", "scenario"]
    ids = [n.id for n in draft.nodes]
    assert len(set(ids)) == len(ids)                       # unique node ids
    assert draft.nodes[0].config == {"flow_id": "hsm", "instances": 2}
    init = draft.nodes[-1].config
    assert init["scenario_id"] == "scn-9" and init["autostart"] is True
    # list order preserved = legacy start order (plan honours it, no edges)
    order = [p["flow_id"] for p in plan_network_flow(draft).participants]
    assert order == ["hsm", "issuer", "hsm"]


def test_migration_is_one_shot_and_reuses_topology_ids():
    store = NetworkFlowStore(":memory:")
    assert store.migrate_topologies([_legacy_topology()]) == ["acq-net"]
    assert store.get("acq-net") is not None
    # second run: registry non-empty → no-op
    assert store.migrate_topologies([_legacy_topology()]) == []


# -- HTTP API -------------------------------------------------------------------

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_network_flow_crud_and_plan_endpoints(client):
    draft = {
        "name": "api net",
        "nodes": [
            {"id": "issuer", "kind": "participant", "config": {"flow_id": "f-issuer"}},
            {"id": "switch", "kind": "participant", "config": {"flow_id": "f-switch"}},
        ],
        "edges": [{"source": "switch", "target": "issuer"}],
    }
    created = client.post("/network-flows", json=draft)
    assert created.status_code == 201, created.text
    nid = created.json()["id"]

    assert any(n["id"] == nid for n in client.get("/network-flows").json())

    plan = client.get(f"/network-flows/{nid}/plan").json()
    assert [p["node_id"] for p in plan["participants"]] == ["issuer", "switch"]

    draft["description"] = "updated"
    assert client.put(f"/network-flows/{nid}", json=draft).status_code == 200
    assert client.get(f"/network-flows/{nid}").json()["description"] == "updated"

    assert client.delete(f"/network-flows/{nid}").status_code == 204
    assert client.get(f"/network-flows/{nid}").status_code == 404


def test_save_rejects_structurally_invalid_network_flow(client):
    bad = {"name": "bad",
           "nodes": [{"id": "p", "kind": "participant", "config": {}}]}
    resp = client.post("/network-flows", json=bad)
    assert resp.status_code == 422
    assert "flow_id" in resp.text


def test_validate_endpoint_reports_issues(client):
    resp = client.post("/network-flows/validate", json={
        "name": "check",
        "nodes": [{"id": "p", "kind": "participant",
                   "config": {"flow_id": "f"}}],
        "edges": [{"source": "p", "target": "ghost"}],
    })
    body = resp.json()
    assert resp.status_code == 200 and body["valid"] is False
    assert any("unknown node 'ghost'" in i["message"] for i in body["issues"])


# -- simulator + group node kinds (Phase 4) --------------------------------------

def _simulator(nid, simulator_id="sim-1"):
    return Step(id=nid, kind="simulator", config={"simulator_id": simulator_id})


def _group(nid, group_id="grp-1"):
    return Step(id=nid, kind="group", config={"group_id": group_id})


def test_simulator_and_group_nodes_validate_and_require_ids():
    ok = NetworkFlowDraft(
        name="net",
        nodes=[_participant("switch"), _simulator("hsm"), _group("issuers")],
        edges=[Edge(source="switch", target="hsm"),
               Edge(source="switch", target="issuers")],
    )
    assert validate_network_flow(ok).valid

    bad = NetworkFlowDraft(name="bad", nodes=[
        Step(id="s", kind="simulator", config={}),
        Step(id="g", kind="group", config={}),
    ])
    msgs = [i.message for i in validate_network_flow(bad).issues]
    assert any("needs a simulator_id" in m for m in msgs)
    assert any("needs a group_id" in m for m in msgs)


def test_simulator_is_target_only_but_group_fans_out_to_participants():
    d = NetworkFlowDraft(
        name="mixed",
        nodes=[_participant("p"), _simulator("sim"), _group("grp")],
        edges=[Edge(source="sim", target="p"),    # illegal: sim never sends
               Edge(source="grp", target="p"),    # legal: group fan-out
               Edge(source="grp", target="sim")], # illegal: group → non-participant
    )
    msgs = [i.message for i in validate_network_flow(d).issues]
    assert sum("only receive traffic" in m for m in msgs) == 1
    assert sum("fans out to the participants" in m for m in msgs) == 1


def test_group_hop_collapses_into_start_order():
    """switch → group → issuer: the issuer must start before the switch even
    though the wiring goes through a group node."""
    d = NetworkFlowDraft(
        name="fanout",
        nodes=[_participant("switch"), _group("grp"), _participant("issuer")],
        edges=[Edge(source="switch", target="grp"),
               Edge(source="grp", target="issuer")],
    )
    order = [p["node_id"] for p in plan_network_flow(d).participants]
    assert order.index("issuer") < order.index("switch")


def test_plan_lists_simulators_and_ignores_groups():
    d = NetworkFlowDraft(
        name="net",
        nodes=[_simulator("hsm", "payshield"), _participant("switch"),
               _group("issuers")],
        edges=[Edge(source="switch", target="hsm")],
    )
    plan = plan_network_flow(d)
    assert plan.simulators == [{"node_id": "hsm", "simulator_id": "payshield"}]
    assert [p["node_id"] for p in plan.participants] == ["switch"]


def test_network_kinds_still_rejected_inside_scenarios_incl_new():
    result = validate_scenario(ScenarioDraft(
        name="s", steps=[Step(id="x", kind="simulator"),
                         Step(id="y", kind="group")]))
    assert not result.valid
    assert sum("network-flow" in i.message for i in result.issues) == 2


def test_validate_endpoint_flags_dangling_group(client):
    resp = client.post("/network-flows/validate", json={
        "name": "check",
        "nodes": [{"id": "g", "kind": "group",
                   "config": {"group_id": "no-such-group"}}],
    })
    body = resp.json()
    assert body["valid"] is False
    assert any("participant group 'no-such-group'" in i["message"]
               for i in body["issues"])


def test_validate_endpoint_flags_dangling_references(client):
    resp = client.post("/network-flows/validate", json={
        "name": "check",
        "nodes": [
            {"id": "p", "kind": "participant",
             "config": {"flow_id": "no-such-flow"}},
            {"id": "s", "kind": "scenario",
             "config": {"scenario_id": "no-such-scn"}},
        ],
    })
    body = resp.json()
    assert resp.status_code == 200 and body["valid"] is False
    msgs = [i["message"] for i in body["issues"]]
    assert any("participant flow 'no-such-flow'" in m for m in msgs)
    assert any("scenario 'no-such-scn'" in m for m in msgs)


# -- wiring inference -------------------------------------------------------------

def _mk_participant_flow(client, name, listen_conn, outbound=None):
    """A flow listening on ``listen_conn`` whose action node targets ``outbound``."""
    nodes = [{"id": "t", "kind": "trigger"}]
    edges = []
    prev = "t"
    if outbound:
        nodes.append({"id": "call", "kind": "action",
                      "target": outbound, "action": "send_message"})
        edges.append({"source": prev, "target": "call"})
        prev = "call"
    nodes.append({"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}})
    edges.append({"source": prev, "target": "r"})
    return client.post("/participant-flows", json={
        "name": name, "trigger": {"connection": listen_conn},
        "nodes": nodes, "edges": edges,
    }).json()


def test_infer_wiring_connection_and_group_fanout(client):
    # listeners: issuer on :7301, switch on :7101; outbound conns reach them
    client.put("/connections/t_issuer_in", json={
        "adapter": "tcp", "protocol": "iso8583", "mode": "inbound",
        "host": "127.0.0.1", "port": 7301})
    client.put("/connections/t_issuer_out", json={
        "adapter": "tcp", "protocol": "iso8583", "mode": "outbound",
        "host": "127.0.0.1", "port": 7301})
    client.put("/connections/t_switch_in", json={
        "adapter": "tcp", "protocol": "iso8583", "mode": "inbound",
        "host": "127.0.0.1", "port": 7101})
    grp = client.post("/participant-groups", json={
        "name": "t_issuers",
        "members": [{"connection": "t_issuer_out"}],
    }).json()

    issuer = _mk_participant_flow(client, "issuer", "t_issuer_in")
    switch = _mk_participant_flow(client, "switch", "t_switch_in",
                                  outbound="t_issuers")

    draft = {
        "name": "auto net",
        "nodes": [
            {"id": "switch", "kind": "participant",
             "config": {"flow_id": switch["id"]}},
            {"id": "issuer", "kind": "participant",
             "config": {"flow_id": issuer["id"]}},
            {"id": "fleet", "kind": "group", "config": {"group_id": grp["id"]}},
        ],
    }
    out = client.post("/network-flows/infer-wiring", json=draft).json()
    edges = {(e["source"], e["target"]) for e in out["edges"]}
    # switch targets the group → edge into the group node + fan-out to issuer
    assert ("switch", "fleet") in edges
    assert ("fleet", "issuer") in edges

    # without the group node on the canvas, the fan-out goes direct
    draft["nodes"] = draft["nodes"][:2]
    out2 = client.post("/network-flows/infer-wiring", json=draft).json()
    assert {(e["source"], e["target"]) for e in out2["edges"]} == {
        ("switch", "issuer")}


def test_infer_wiring_skips_existing_edges_and_notes_unresolved(client):
    client.put("/connections/t_a_in", json={
        "adapter": "tcp", "protocol": "iso8583", "mode": "inbound",
        "host": "127.0.0.1", "port": 7401})
    a = _mk_participant_flow(client, "a", "t_a_in", outbound="nowhere_conn")
    draft = {
        "name": "n",
        "nodes": [{"id": "a", "kind": "participant",
                   "config": {"flow_id": a["id"]}}],
        "edges": [],
    }
    out = client.post("/network-flows/infer-wiring", json=draft).json()
    assert out["edges"] == []
    assert any("does not resolve" in n for n in out["notes"])


# -- participant-flow versioning (Phase 0) ---------------------------------------

def _flow_draft(reply="00"):
    return ParticipantFlowDraft(
        name="issuer stub",
        trigger=FlowTrigger(connection="issuer_listen"),
        nodes=[Step(id="t", kind="trigger"),
               Step(id="r", kind="reply", payload={"field39": reply})],
        edges=[Edge(source="t", target="r")],
    )


def test_flow_versions_bump_and_history_is_retrievable():
    s = ParticipantFlowStore(":memory:")
    f = s.create(_flow_draft("00"))
    assert f.version == 1
    f2 = s.upsert(f.id, _flow_draft("05"))
    assert f2.version == 2
    # current = v2, history keeps v1's document
    assert s.get(f.id).nodes[1].payload["field39"] == "05"
    old = s.get(f.id, version=1)
    assert old is not None and old.nodes[1].payload["field39"] == "00"
    assert s.get(f.id, version=99) is None
    versions = s.versions(f.id)
    assert [v.version for v in versions] == [2, 1]


def test_flow_version_endpoints(client):
    created = client.post("/participant-flows",
                          json=_flow_draft("00").model_dump())
    fid = created.json()["id"]
    client.put(f"/participant-flows/{fid}", json=_flow_draft("91").model_dump())

    rows = client.get(f"/participant-flows/{fid}/versions").json()
    assert [r["version"] for r in rows] == [2, 1]

    v1 = client.get(f"/participant-flows/{fid}", params={"version": 1}).json()
    assert v1["nodes"][1]["payload"]["field39"] == "00"
    assert client.get(f"/participant-flows/{fid}",
                      params={"version": 9}).status_code == 404
