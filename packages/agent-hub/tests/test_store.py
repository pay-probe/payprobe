"""RegistryStore lifecycle against PostgreSQL — the provenance discipline applied to prompts."""

import pytest
from agent_hub.store import Conflict, Guardrail, NotFound, spec_hash
from conftest import agent_spec


async def test_create_makes_version_1_draft(store):
    d = await store.create("agent", "a1", agent_spec(), by="dave")
    assert d["status"] == "draft"
    assert d["active_version"] is None
    assert d["latest_version"] == 1
    assert d["versions"][0]["status"] == "draft"
    assert d["spec"] is None  # nothing active yet


async def test_create_twice_conflicts(store):
    await store.create("agent", "a1", agent_spec())
    with pytest.raises(Conflict):
        await store.create("agent", "a1", agent_spec())


async def test_publish_activates_and_supersedes(store):
    await store.create("agent", "a1", agent_spec(role="v1"))
    v1 = await store.publish("agent", "a1", 1, by="dave")
    assert v1["status"] == "active" and v1["published_by"] == "dave"
    assert (await store.get("agent", "a1"))["status"] == "active"

    await store.add_version("agent", "a1", agent_spec(role="v2"))
    await store.publish("agent", "a1", 2)
    d = await store.get("agent", "a1")
    assert d["active_version"] == 2
    assert [v["status"] for v in d["versions"]] == ["superseded", "active"]
    assert d["spec"]["role"] == "v2"


async def test_published_version_is_immutable(store):
    await store.create("agent", "a1", agent_spec())
    await store.publish("agent", "a1", 1)
    with pytest.raises(Conflict):
        await store.update_draft("agent", "a1", 1, agent_spec(role="edited"))
    with pytest.raises(Conflict):
        await store.publish("agent", "a1", 1)  # already active


async def test_draft_edit_changes_hash(store):
    await store.create("agent", "a1", agent_spec())
    before = (await store.get_version("agent", "a1", 1))["spec_sha256"]
    after = (await store.update_draft("agent", "a1", 1, agent_spec(role="edited")))["spec_sha256"]
    assert before != after
    assert after == spec_hash(agent_spec(role="edited"))


async def test_resolve_bare_name_and_pins(store):
    assert await store.resolve("agent", "a1") is None
    await store.create("agent", "a1", agent_spec(role="one"))
    assert await store.resolve("agent", "a1") is None  # draft is not runnable
    await store.publish("agent", "a1", 1)
    assert (await store.resolve("agent", "a1"))[1] == 1
    await store.add_version("agent", "a1", agent_spec(role="two"))
    await store.publish("agent", "a1", 2)
    _, ver, spec = await store.resolve("agent", "a1")
    assert (ver, spec["role"]) == (2, "two")
    assert (await store.resolve("agent", "a1@1"))[2]["role"] == "one"  # pin survives
    assert await store.resolve("agent", "a1@3") is None
    assert await store.resolve("agent", "a1@x") is None


async def test_retire_stops_resolution_and_new_versions(store):
    await store.create("agent", "a1", agent_spec())
    await store.publish("agent", "a1", 1)
    d = await store.retire("agent", "a1", by="dave")
    assert d["status"] == "retired"
    assert await store.resolve("agent", "a1") is None
    assert await store.resolve("agent", "a1@1") is None
    with pytest.raises(Conflict):
        await store.add_version("agent", "a1", agent_spec())
    with pytest.raises(Conflict):
        await store.publish("agent", "a1", 1)


async def test_builtin_cannot_be_retired(store):
    await store.create("agent", "seeded", agent_spec(), builtin=True)
    with pytest.raises(Guardrail):
        await store.retire("agent", "seeded")


async def test_not_found_paths(store):
    with pytest.raises(NotFound):
        await store.get("agent", "nope")
    await store.create("agent", "a1", agent_spec())
    with pytest.raises(NotFound):
        await store.get_version("agent", "a1", 9)
    with pytest.raises(NotFound):
        await store.add_version("workflow", "a1", {})  # kinds are separate namespaces


async def test_pause_flag_round_trips(store):
    assert await store.paused() == {"paused": False, "by": None, "at": None}
    v = await store.set_paused(True, by="dave")
    assert v["paused"] is True and v["by"] == "dave" and v["at"]
    assert (await store.paused())["paused"] is True
    assert (await store.set_paused(False))["paused"] is False


async def test_migrations_are_recorded_and_idempotent(store):
    assert await store.schema_version() >= 1
    assert await store.migrate() == []  # nothing new to apply
    await store.create("agent", "a1", agent_spec())
    await store.publish("agent", "a1", 1)
    # a second pool over the same database sees the committed state
    from agent_hub.store import RegistryStore
    from conftest import TEST_DSN

    other = await RegistryStore.connect(TEST_DSN, pool_max=1)
    try:
        assert (await other.resolve("agent", "a1"))[1] == 1
    finally:
        await other.close()


async def test_one_active_version_is_enforced_by_the_database(store):
    """Belt and braces: even a buggy code path cannot leave two actives."""
    import asyncpg

    await store.create("agent", "a1", agent_spec())
    await store.publish("agent", "a1", 1)
    await store.add_version("agent", "a1", agent_spec())
    with pytest.raises(asyncpg.UniqueViolationError):
        await store._pool.execute(
            "UPDATE agent_hub_versions SET status='active' WHERE name='a1' AND version=2"
        )
