"""StarterFlowStore: builtins + custom CRUD, overrides, and reset."""
import pytest

from api.flow_store import StarterFlowStore
from models.starter_flow import BUILTIN_FLOWS, StarterFlowDraft, FlowStep


def _draft(label="My flow"):
    return StarterFlowDraft(
        label=label, description="demo",
        steps=[FlowStep(ref="a", target="tcp_iso8583", action="send_0200",
                        config={"amount": 100})],
    )


def test_list_includes_builtins_flagged():
    s = StarterFlowStore(":memory:")
    flows = s.list()
    assert {f.id for f in BUILTIN_FLOWS} <= {f.id for f in flows}
    assert all(f.builtin for f in flows if f.id in {b.id for b in BUILTIN_FLOWS})


def test_create_slugs_id_and_persists():
    s = StarterFlowStore(":memory:")
    f = s.create(_draft("ISO 8583 smoke"))
    assert f.id == "iso-8583-smoke" and f.builtin is False
    assert s.get(f.id).steps[0].action == "send_0200"


def test_create_unique_id_on_collision():
    s = StarterFlowStore(":memory:")
    a = s.create(_draft("dup"))
    b = s.create(_draft("dup"))
    assert a.id != b.id


def test_editing_builtin_creates_override_still_flagged_builtin():
    s = StarterFlowStore(":memory:")
    bid = BUILTIN_FLOWS[0].id
    updated = s.update(bid, _draft("changed"))
    assert updated.id == bid and updated.builtin is True
    assert s.get(bid).label == "changed"


def test_delete_custom_then_reset_builtin_override():
    s = StarterFlowStore(":memory:")
    f = s.create(_draft("temp"))
    s.delete(f.id)
    assert s.get(f.id) is None

    # overriding a builtin then deleting resets it to the seed
    bid = BUILTIN_FLOWS[0].id
    s.update(bid, _draft("override"))
    assert s.get(bid).label == "override"
    s.delete(bid)  # reset
    assert s.get(bid).label == BUILTIN_FLOWS[0].label


def test_delete_pristine_builtin_raises():
    s = StarterFlowStore(":memory:")
    with pytest.raises(ValueError, match="built-in"):
        s.delete(BUILTIN_FLOWS[0].id)
