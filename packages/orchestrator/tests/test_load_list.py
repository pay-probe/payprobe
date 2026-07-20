"""Load-run list endpoint: only load runs, newest first, reopenable."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


async def test_list_filters_to_load_runs(store):
    store.create("f1", "mock", 3, label="regression suite")  # functional
    store.create("l1", "mock", 1, label="load:steady 20k")    # load
    store.finish("l1", "completed", {"sent": 10, "received": 10, "errors": 0,
                                     "tps": 9, "latency_ms": {"p95": 1}})
    out = await m.list_load_runs()
    ids = [r["run_id"] for r in out]
    assert ids == ["l1"]                       # functional run excluded
    assert out[0]["label"] == "steady 20k"      # 'load:' prefix stripped
    assert out[0]["status"] == "completed"
