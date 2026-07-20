"""Resilience scorer: pure scoring over per-second samples + endpoint wiring."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api.resilience import (  # noqa: E402
    STAGE_BASELINE,
    STAGE_RECOVERY,
    STAGE_STORM,
    aggregate_stages,
    score_resilience,
)


def _s(t, stage, cr, ce, sf, sr, p95=10.0):
    """A cumulative sample dict."""
    return {
        "t": t, "stage": stage,
        "client_received": cr, "client_errors": ce,
        "sim_faults": sf, "sim_received": sr, "p95_ms": p95,
    }


# -- stage aggregation (deltas from cumulative counters) ----------------------


def test_aggregate_stages_diffs_first_and_last():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 100, 0, 0, 100),
        _s(2, STAGE_STORM, 100, 0, 0, 100),
        _s(3, STAGE_STORM, 180, 20, 25, 100),
    ]
    stages = aggregate_stages(samples)
    base = stages[STAGE_BASELINE]
    storm = stages[STAGE_STORM]
    assert base.client_received == 100 and base.client_errors == 0
    assert base.success_rate == 1.0
    # storm delta: +80 received, +20 errors, +25 faults over +0 sim? sim went 100->100=0
    assert storm.client_received == 80 and storm.client_errors == 20
    assert storm.sim_faults == 25
    assert round(storm.error_rate, 2) == 0.20


# -- a resilient client: faults injected, but retries keep success high -------


def test_resilient_client_scores_high_and_passes():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200),
        # storm: sim faults 30% of replies, but client only loses ~2%
        _s(2, STAGE_STORM, 200, 0, 0, 200),
        _s(3, STAGE_STORM, 390, 4, 60, 200),
        # recovery: back to clean
        _s(4, STAGE_RECOVERY, 390, 4, 60, 200),
        _s(5, STAGE_RECOVERY, 590, 4, 60, 200),
    ]
    rep = score_resilience(samples)
    assert rep["verdict"] == "PASS"
    assert rep["score"] >= 80
    assert rep["grade"] in ("A+", "A", "B")
    assert rep["blocking"] == []
    # absorption is high: most injected faults never reached the client
    assert rep["components"]["absorption"] >= 80


# -- a fragile client: every injected fault becomes a client error -----------


def test_fragile_client_fails_availability_gate():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200),
        # storm: half the replies faulted AND the client errored on all of them
        _s(2, STAGE_STORM, 200, 0, 0, 200),
        _s(3, STAGE_STORM, 300, 100, 100, 200),
        _s(4, STAGE_RECOVERY, 300, 100, 100, 200),
        _s(5, STAGE_RECOVERY, 500, 100, 100, 200),
    ]
    rep = score_resilience(samples)
    assert rep["verdict"] == "FAIL"
    assert "availability" in rep["blocking"]
    assert rep["components"]["availability"] < 90


# -- a wedged client: sim still answers some, client gets nothing through ----


def test_wedged_client_trips_no_wedge_gate():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200),
        # 50% fault rate (sim sees 200 more, faults 100), but the client gets
        # nothing through — success collapses to zero → wedged
        _s(2, STAGE_STORM, 200, 0, 0, 200),
        _s(3, STAGE_STORM, 200, 80, 100, 400),
        _s(4, STAGE_RECOVERY, 200, 80, 100, 400),
        _s(5, STAGE_RECOVERY, 200, 80, 100, 400),  # never recovers either
    ]
    rep = score_resilience(samples)
    assert rep["verdict"] == "FAIL"
    assert "no_wedge" in rep["blocking"]


# -- no recovery: client stays broken after the storm lifts ------------------


def test_no_recovery_fails_recovery_gate():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200),
        _s(2, STAGE_STORM, 200, 0, 0, 200),
        _s(3, STAGE_STORM, 380, 10, 30, 200),
        # recovery stage but success rate stays degraded (client didn't heal)
        _s(4, STAGE_RECOVERY, 380, 10, 30, 200),
        _s(5, STAGE_RECOVERY, 480, 100, 30, 200),
    ]
    rep = score_resilience(samples)
    assert "recovery" in rep["blocking"]
    assert rep["verdict"] == "FAIL"


# -- latency blowout under chaos ---------------------------------------------


def test_latency_ceiling_gate():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0, p95=20.0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200, p95=20.0),
        # p95 explodes to 200ms = 10× baseline (ceiling is 4× by default)
        _s(2, STAGE_STORM, 200, 0, 0, 200, p95=200.0),
        _s(3, STAGE_STORM, 400, 0, 40, 200, p95=200.0),
        _s(4, STAGE_RECOVERY, 400, 0, 40, 200, p95=20.0),
        _s(5, STAGE_RECOVERY, 600, 0, 40, 200, p95=20.0),
    ]
    rep = score_resilience(samples)
    assert "latency" in rep["blocking"]
    assert rep["components"]["latency"] == 0.0


# -- thresholds are overridable ----------------------------------------------


def test_threshold_override_relaxes_pass_bar():
    samples = [
        _s(0, STAGE_BASELINE, 0, 0, 0, 0),
        _s(1, STAGE_BASELINE, 200, 0, 0, 200),
        _s(2, STAGE_STORM, 200, 0, 0, 200),
        _s(3, STAGE_STORM, 340, 30, 40, 200),
        _s(4, STAGE_RECOVERY, 340, 30, 40, 200),
        _s(5, STAGE_RECOVERY, 540, 30, 40, 200),
    ]
    strict = score_resilience(samples, {"min_availability_ratio": 0.98})
    relaxed = score_resilience(samples, {"min_availability_ratio": 0.5, "pass_score": 50})
    assert strict["verdict"] == "FAIL"
    assert relaxed["verdict"] == "PASS"


# -- endpoint lifecycle (real simulator + a fake load run) --------------------


class _FakeLoadRun:
    """Minimal stand-in for a running LoadRun: a snapshot whose client
    counters climb each poll, so the resilience sampler sees live traffic."""

    def __init__(self):
        self.status = "running"
        self._n = 0

    def snapshot(self):
        self._n += 1
        received = self._n * 100
        return {
            "status": "running",
            "received": received,
            "errors": 0,
            "tps": 100.0,
            "latency_ms": {"p95": 12.0},
        }


async def test_resilience_run_endpoint_lifecycle():
    from orchestrator.api import main as m

    m.SIMULATORS.clear()
    m.CHAOS_STORMS.clear()
    m.RESILIENCE_RUNS.clear()

    started = await m.start_simulator(m.SimulatorDraft(
        label="target-sim",
        config={"protocol": "iso8583", "default": {"echo": ["11"], "set": {"39": "00"}}},
    ))
    sid = started["id"]

    # register a fake running load run for the sampler to observe
    m.load_coordinator.runs["load-x"] = _FakeLoadRun()

    try:
        created = await m.start_resilience_run(m.ResilienceRunRequest(
            target_sim_id=sid,
            load_run_id="load-x",
            storm=m.ChaosStormRequest(
                phases=[m.ChaosPhase(duration_s=0.1, chaos={"drop_pct": 100}, label="outage")],
            ),
            baseline_s=0.1,
            recovery_s=0.1,
        ))
        rid = created["id"]
        assert created["status"] == "running"

        # let the run walk baseline → storm → recovery and finish
        task = m.RESILIENCE_RUNS[rid]["task"]
        import asyncio
        await asyncio.wait_for(task, timeout=5)

        view = await m.get_resilience_run(rid)
        assert view["status"] == "completed"
        rep = view["report"]
        assert rep is not None
        assert rep["verdict"] in ("PASS", "FAIL")
        # all three stages were sampled
        assert set(rep["stages"]) == {"baseline", "storm", "recovery"}
        # the storm was cleaned up off the simulator
        assert sid not in m.CHAOS_STORMS
        assert m.SIMULATORS[sid].global_chaos == {}

        # it shows up in history with its verdict
        listed = await m.list_resilience_runs()
        assert any(r["id"] == rid and r["verdict"] == rep["verdict"] for r in listed)
    finally:
        m.load_coordinator.runs.pop("load-x", None)
        await m.stop_simulator(sid)


async def test_resilience_run_requires_running_load():
    import pytest
    from fastapi import HTTPException
    from orchestrator.api import main as m

    m.SIMULATORS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="target", config={"protocol": "iso8583", "default": {"set": {"39": "00"}}}))
    sid = started["id"]
    try:
        with pytest.raises(HTTPException) as exc:
            await m.start_resilience_run(m.ResilienceRunRequest(
                target_sim_id=sid,
                load_run_id="does-not-exist",
                storm=m.ChaosStormRequest(phases=[m.ChaosPhase(duration_s=0.1, chaos={"drop_pct": 50})]),
            ))
        assert exc.value.status_code == 400
    finally:
        await m.stop_simulator(sid)
