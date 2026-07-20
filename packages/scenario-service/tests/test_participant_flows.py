"""ParticipantFlowStore CRUD + model: a flow reuses Step/Edge with trigger/reply."""
from api.participant_flow_store import ParticipantFlowStore
from models.participant_flow import ParticipantFlowDraft, FlowTrigger
from models.scenario import Step, Edge


def _draft(name="Issuer stub"):
    return ParticipantFlowDraft(
        name=name,
        description="approves 0200",
        trigger=FlowTrigger(connection="issuer_listen"),
        nodes=[
            Step(id="t", kind="trigger"),
            Step(id="r", kind="reply", payload={"mti": "0210", "field39": "00"}),
        ],
        edges=[Edge(source="t", source_port="out", target="r")],
    )


def test_trigger_and_reply_kinds_validate():
    d = _draft()
    assert d.nodes[0].kind == "trigger"
    assert d.nodes[1].kind == "reply"
    assert d.trigger.connection == "issuer_listen"


def test_create_generates_unique_id_independent_of_name():
    s = ParticipantFlowStore(":memory:")
    # a blank/default name no longer produces "untitled-scenario" — the id is a
    # stable unique handle, decoupled from the (mutable) name.
    f = s.create(_draft(""))
    assert f.id.startswith("flow-") and f.id != "untitled-scenario"
    got = s.get(f.id)
    assert got is not None
    assert got.nodes[1].payload["field39"] == "00"
    # an explicit id is still honoured (imports / round-trips)
    f2 = s.create(_draft("X"), fid="my-import-id")
    assert f2.id == "my-import-id"


def test_create_unique_id_on_collision():
    s = ParticipantFlowStore(":memory:")
    a = s.create(_draft("dup"))
    b = s.create(_draft("dup"))
    assert a.id != b.id   # same name, distinct generated ids


def test_reply_payload_may_be_a_whole_string_reference():
    """A reply node echoing a relay/action response is saved as a whole-string
    reference, not a dict — the model must accept it (regression: 422
    "Input should be a valid dictionary")."""
    d = ParticipantFlowDraft(
        name="hsm relay",
        trigger=FlowTrigger(connection="cn_listener_hsm_9001"),
        nodes=[
            Step(id="t", kind="trigger"),
            Step(id="fwd", kind="relay", config={"connection": "cn_hsm_8000"}),
            Step(id="r", kind="reply", payload="${fwd.response}"),
        ],
        edges=[
            Edge(source="t", source_port="out", target="fwd"),
            Edge(source="fwd", source_port="out", target="r"),
        ],
    )
    s = ParticipantFlowStore(":memory:")
    f = s.create(d)
    assert s.get(f.id).nodes[2].payload == "${fwd.response}"


def test_upsert_and_delete():
    s = ParticipantFlowStore(":memory:")
    f = s.create(_draft())
    s.upsert(f.id, _draft("renamed"))
    assert s.get(f.id).name == "renamed"
    s.delete(f.id)
    assert s.get(f.id) is None
