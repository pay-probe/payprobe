"""A run must reach a verdict (and emit run.completed) even if an adapter can't
connect — an unreachable host is a phase-1 failure, never a hung run."""

from worker.adapters.registry import AdapterRegistry
from worker.engine import RUN_COMPLETED, InMemorySink, WorkerEngine

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


# -- phase 1 checks only what the run dispatches to (2026-11-13) --------------

#: a reachable-by-construction mock target beside the dead TCP one
MIXED_ENV = {
    **REAL_ENV,
    "adapters": {
        **REAL_ENV["adapters"],
        "pos_ok": {"mock": True, "latency_ms": 0},
    },
}


def _one_step(target: str, sid: str = "sc") -> dict:
    return {
        "id": sid,
        "name": sid,
        "test_class": "e2e",
        "steps": [{"id": "s1", "target": target, "action": "send_0800", "payload": {}}],
    }


async def test_unused_unreachable_adapter_does_not_fail_the_run():
    """The environment declares a dead adapter no step targets: phase 1 neither
    dials nor checks it, and a run whose only case passed is passed."""
    engine = WorkerEngine(MIXED_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([_one_step("pos_ok")], run_id="r-unused")
    p1 = summary["phases"]["phase_1"]
    assert p1 == {"status": "passed", "components": 1, "failed": []}
    assert summary["scenarios"][0]["status"] == "passed"
    assert summary["status"] == "passed"


async def test_dead_adapter_reached_only_through_a_call_subflow_is_checked():
    """A target used only inside a ``call`` sub-scenario still counts as used."""
    parent = {
        "id": "parent",
        "name": "parent",
        "test_class": "e2e",
        "steps": [
            {"id": "s1", "target": "pos_ok", "action": "send_0800", "payload": {}},
            {"id": "c1", "kind": "call", "config": {"scenario_id": "child"}},
        ],
        "subflows": {"child": _one_step("switch_dead", "child")},
    }
    engine = WorkerEngine(MIXED_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([parent], run_id="r-subflow")
    assert summary["phases"]["phase_1"]["failed"] == ["switch_dead"]
    # blocking still reads only the scenario's own steps (pre-existing), so the
    # parent runs and its call fails; the run is failed either way
    assert summary["status"] == "failed"


async def test_dead_adapter_used_only_by_a_fixture_is_checked():
    engine = WorkerEngine(MIXED_ENV, InMemorySink())
    summary = await engine.run_scenario_batch(
        [_one_step("pos_ok")],
        run_id="r-fixture",
        fixtures={"after": [_one_step("switch_dead", "teardown")]},
    )
    assert summary["phases"]["phase_1"]["failed"] == ["switch_dead"]
    assert summary["status"] == "failed"


async def test_registry_warmup_with_targets_dials_only_those():
    reg = AdapterRegistry(MIXED_ENV)
    await reg.warmup({"pos_ok"})
    health = await reg.health_check_all()
    assert health == {"pos_ok": True}
    await reg.disconnect_all()
