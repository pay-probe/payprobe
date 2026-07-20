"""/load-workers fleet endpoints: list live workers and drain them."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402


@pytest.fixture()
def seeded_bus():
    """Fresh in-memory load bus on the app, pre-seeded with two workers."""
    from worker.engine.load import InMemoryLoadBus

    bus = InMemoryLoadBus()
    m.load_bus = bus
    return bus


async def _seed(bus):
    await bus.worker_heartbeat("w-1", {"run_id": "r1", "host": "h1", "tps": 40, "errors": 0})
    await bus.worker_heartbeat("w-2", {"run_id": "r1", "host": "h2", "tps": 55, "errors": 2})


async def test_list_workers(seeded_bus):
    await _seed(seeded_bus)
    rows = await m.list_load_workers()
    ids = sorted(r["worker_id"] for r in rows)
    assert ids == ["w-1", "w-2"]
    assert all("tps" in r and "draining" in r for r in rows)


async def test_stop_one_worker_marks_draining(seeded_bus):
    await _seed(seeded_bus)
    res = await m.stop_load_worker("w-1")
    assert res == {"worker_id": "w-1", "status": "draining"}
    assert await seeded_bus.is_worker_stopped("w-1") is True
    assert await seeded_bus.is_worker_stopped("w-2") is False

    rows = {r["worker_id"]: r for r in await m.list_load_workers()}
    assert rows["w-1"]["draining"] is True
    assert rows["w-2"]["draining"] is False


async def test_stop_all_workers(seeded_bus):
    await _seed(seeded_bus)
    res = await m.stop_all_load_workers()
    assert res == {"status": "draining", "count": 2}
    assert await seeded_bus.is_worker_stopped("w-1") is True
    assert await seeded_bus.is_worker_stopped("w-2") is True


async def test_list_workers_empty_is_not_an_error(seeded_bus):
    # no workers claiming shards → empty list, not a 404/500
    assert await m.list_load_workers() == []
