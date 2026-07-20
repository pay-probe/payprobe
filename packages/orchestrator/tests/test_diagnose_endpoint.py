"""Diagnose endpoint: classify a finished run's failure."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


async def test_diagnose_functional_failure(store):
    store.create("rF", "prod", 1)
    store.finish("rF", "failed", {"scenarios": [{
        "name": "auth", "steps": [
            {"step_id": "s1", "target": "http", "action": "send_auth_request",
             "status": "error", "error": "ConnectionRefusedError: refused"},
        ]}]})
    out = await m.diagnose_run_endpoint("rF")
    assert out["ok"] is False
    assert out["findings"][0]["code"].startswith("unreachable:http")


async def test_diagnose_load_all_failing(store):
    store.create("rL", "prod", 1, label="load:demo")
    store.finish("rL", "completed", {
        "sent": 500, "received": 0, "errors": 500, "tps": 0, "target_tps": 1000,
        "workers": 4, "workers_reporting": 4, "latency_ms": {"p95": 0},
        "last_error": "tcp_iso8583.send_0200: read timed out",
    })
    out = await m.diagnose_run_endpoint("rL")
    assert out["findings"][0]["code"] == "all_failing_timeout"


async def test_diagnose_404(store):
    with pytest.raises(Exception):
        await m.diagnose_run_endpoint("nope")


async def test_connectivity_failure_attaches_live_doctor_pass(store):
    """A connectivity-class failure triggers a scoped platform-doctor pass and
    its live problems ride along under ``doctor`` (here: the connection
    registry is unreachable in the test env, which is itself a problem)."""
    store.create("rC", "prod", 1)
    store.finish("rC", "failed", {"scenarios": [{
        "name": "auth", "steps": [
            {"step_id": "s1", "target": "visa", "action": "send_0200",
             "status": "error", "error": "ConnectionRefusedError: refused"},
        ]}]})
    out = await m.diagnose_run_endpoint("rC")
    assert "doctor" in out
    assert out["doctor"]["status"] in ("ok", "degraded", "failing")
    assert isinstance(out["doctor"]["problems"], list)


async def test_assertion_failure_skips_doctor_pass(store):
    """Functional (assertion) failures are the scenario's business — no live
    doctor probing, no ``doctor`` key."""
    store.create("rA", "prod", 1)
    store.finish("rA", "failed", {"scenarios": [{
        "name": "auth", "steps": [
            {"step_id": "s1", "target": "visa", "action": "send_0200",
             "status": "failed",
             "error": "assertion de39 equals '00' (got '05')"},
        ]}]})
    out = await m.diagnose_run_endpoint("rA")
    assert "doctor" not in out
