"""A run must reach a verdict (and emit run.completed) even if an adapter can't
connect — an unreachable host is a phase-1 failure, never a hung run."""

from worker.engine import WorkerEngine, InMemorySink, RUN_COMPLETED
from worker.adapters.registry import AdapterRegistry

# A real (non-mock) env naming a TCP connection to an unreachable port.
REAL_ENV = {
    "mode": "real",
    "phase1_retry_delay_sec": 0,  # keep the one phase-1 retry instant in tests
    "adapters": {
        "switch_dead": {
            "adapter": "tcp",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": 1,
            "connect_timeout_sec": 0.3,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
        },
    },
}


async def test_warmup_failure_is_recorded_not_raised():
    reg = AdapterRegistry(REAL_ENV)
    await reg.warmup()  # must not raise
    health = await reg.health_check_all()
    assert health.get("switch_dead") is False
    await reg.disconnect_all()


async def test_run_completes_despite_unreachable_adapter():
    sink = InMemorySink()
    engine = WorkerEngine(REAL_ENV, sink)
    scenario = {
        "id": "sc",
        "name": "dead",
        "test_class": "e2e",
        "steps": [
            {
                "id": "s1",
                "target": "switch_dead",
                "action": "send_0800",
                "payload": {},
                "assertions": [],
            }
        ],
    }
    summary = await engine.run_scenario_batch([scenario], run_id="r-dead")
    # produced a definite verdict (not a hang / not an unhandled raise)
    assert summary["status"] in ("failed", "blocked", "error", "passed")
    # phase 1 flagged the dead component
    assert summary["phases"]["phase_1"]["status"] != "passed"
    # the terminal event was emitted
    assert any(e.type == RUN_COMPLETED for e in sink.events)


async def test_phase1_retries_once_and_records_recovery():
    """A component that fails its first health check but heals on the retry
    (e.g. a relay with a stale upstream) must NOT block the run — phase 1
    passes and the flake stays visible under ``recovered``."""
    sink = InMemorySink()
    engine = WorkerEngine(REAL_ENV, sink)

    async def _first_check():
        return {"switch_dead": False}

    async def _retry(targets):
        assert targets == {"switch_dead"}
        return {"switch_dead": True}

    engine.registry.health_check_all = _first_check  # type: ignore[method-assign]
    engine.registry.retry_unhealthy = _retry  # type: ignore[method-assign]

    summary = await engine.run_scenario_batch([], run_id="r-flaky")
    p1 = summary["phases"]["phase_1"]
    assert p1["status"] == "passed"
    assert p1["failed"] == []
    assert p1["recovered"] == ["switch_dead"]


async def test_phase1_retry_does_not_save_genuinely_dead_target():
    reg = AdapterRegistry(REAL_ENV)
    await reg.warmup()
    retried = await reg.retry_unhealthy({"switch_dead"})
    assert retried == {"switch_dead": False}
    await reg.disconnect_all()
