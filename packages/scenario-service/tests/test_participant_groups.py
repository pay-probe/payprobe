"""ParticipantGroupStore CRUD + model."""
from api.participant_group_store import ParticipantGroupStore
from models.participant_group import ParticipantGroupDraft, GroupMember


def _draft(name="Issuers"):
    return ParticipantGroupDraft(
        name=name,
        description="fleet of issuer stand-ins",
        adapter_type="tcp",
        members=[GroupMember(connection="issuer_a", weight=1),
                 GroupMember(connection="issuer_b", weight=3)],
        selection={"policy": "weighted"},
    )


def test_model_defaults_to_round_robin():
    d = ParticipantGroupDraft(name="g")
    assert d.selection == {"policy": "round_robin"}
    assert d.members == []


def test_create_slugs_id_and_persists():
    s = ParticipantGroupStore(":memory:")
    g = s.create(_draft("Issuer Fleet"))
    assert g.id == "issuer-fleet"
    got = s.get(g.id)
    assert got is not None
    assert got.members[1].connection == "issuer_b"
    assert got.members[1].weight == 3
    assert got.selection["policy"] == "weighted"


def test_unique_id_and_upsert_and_delete():
    s = ParticipantGroupStore(":memory:")
    a = s.create(_draft("dup"))
    b = s.create(_draft("dup"))
    assert a.id != b.id
    s.upsert(a.id, _draft("renamed"))
    assert s.get(a.id).name == "renamed"
    s.delete(a.id)
    assert s.get(a.id) is None
