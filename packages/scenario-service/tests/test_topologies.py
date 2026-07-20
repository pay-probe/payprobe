"""TopologyStore CRUD + model."""
from api.topology_store import TopologyStore
from models.topology import TopologyDraft, TopologyParticipant


def _draft(name="Net"):
    return TopologyDraft(
        name=name,
        description="acquirer -> switch -> issuers",
        participants=[
            TopologyParticipant(flow_id="issuer", instances=3),
            TopologyParticipant(flow_id="switch", instances=1),
        ],
    )


def test_create_slugs_id_and_persists():
    s = TopologyStore(":memory:")
    t = s.create(_draft("Payment Net"))
    assert t.id == "payment-net"
    got = s.get(t.id)
    assert got is not None
    assert got.participants[0].flow_id == "issuer"
    assert got.participants[0].instances == 3


def test_unique_id_upsert_delete():
    s = TopologyStore(":memory:")
    a = s.create(_draft("dup"))
    b = s.create(_draft("dup"))
    assert a.id != b.id
    s.upsert(a.id, _draft("renamed"))
    assert s.get(a.id).name == "renamed"
    s.delete(a.id)
    assert s.get(a.id) is None
