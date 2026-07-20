"""Run-vs-run load comparison: persisted richer history + the compare endpoint.

Covers the TPS-stability roll-up, the like-for-like baseline selection, and the
metric/error-category diff that turns load testing into regression detection.
"""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402
from orchestrator.api.load_coordinator import tps_stability  # noqa: E402


@pytest.fixture()
def store():
    m.run_store = RunStore(":memory:")
    return m.run_store


def _summary(*, p50, p95, p99, tps, received, errors, cv=0.0, cats=None):
    return {
        "sent": received + errors,
        "received": received,
        "errors": errors,
        "tps": tps,
        "latency_ms": {"p50": p50, "p95": p95, "p99": p99, "mean": p50, "max": p99},
        "tps_stability": {"cv": cv, "mean": tps, "stdev": cv * tps,
                          "min": tps, "max": tps, "samples": 10},
        "error_categories": cats or {},
    }


# -- TPS stability roll-up ----------------------------------------------------

def test_tps_stability_flat_series_is_zero_cv():
    out = tps_stability([100.0, 100.0, 100.0, 100.0])
    assert out["mean"] == 100.0 and out["cv"] == 0.0 and out["samples"] == 4


def test_tps_stability_trims_leading_warmup_zeros():
    # ramp-up zeros must not depress the mean / inflate cv
    out = tps_stability([0.0, 0.0, 200.0, 200.0])
    assert out["samples"] == 2 and out["mean"] == 200.0 and out["cv"] == 0.0


def test_tps_stability_interior_dropout_raises_cv():
    steady = tps_stability([100.0] * 6)
    sawtooth = tps_stability([100.0, 0.0, 100.0, 0.0, 100.0, 0.0])
    assert sawtooth["cv"] > steady["cv"]


def test_tps_stability_empty_series():
    out = tps_stability([])
    assert out["samples"] == 0 and out["cv"] == 0.0


# -- baseline selection -------------------------------------------------------

def test_previous_load_prefers_same_label(store):
    store.create("l1", "mock", 1, label="load:steady 20k")
    store.finish("l1", "completed", _summary(p50=1, p95=2, p99=3, tps=100,
                                             received=100, errors=0))
    store.create("o1", "mock", 1, label="load:spike")
    store.finish("o1", "completed", _summary(p50=9, p95=9, p99=9, tps=50,
                                             received=50, errors=0))
    store.create("l2", "mock", 1, label="load:steady 20k")
    store.finish("l2", "completed", _summary(p50=1, p95=2, p99=3, tps=100,
                                             received=100, errors=0))
    # the same-label predecessor wins over the more-recent different-label run
    assert store.previous_load("l2") == "l1"


def test_previous_load_ignores_functional_runs(store):
    store.create("f1", "mock", 3, label="regression")
    store.finish("f1", "passed", {"scenarios": []})
    store.create("l1", "mock", 1, label="load:steady")
    store.finish("l1", "completed", _summary(p50=1, p95=2, p99=3, tps=100,
                                             received=100, errors=0))
    store.create("l2", "mock", 1, label="load:other")
    store.finish("l2", "completed", _summary(p50=1, p95=2, p99=3, tps=100,
                                             received=100, errors=0))
    # falls back to the previous *load* run, never the functional one
    assert store.previous_load("l2") == "l1"


# -- the compare endpoint -----------------------------------------------------

async def test_compare_flags_latency_regression(store):
    store.create("l1", "mock", 1, label="load:steady")
    store.finish("l1", "completed", _summary(p50=10, p95=20, p99=30, tps=1000,
                                             received=1000, errors=0))
    store.create("l2", "mock", 1, label="load:steady")
    store.finish("l2", "completed", _summary(p50=15, p95=40, p99=80, tps=1000,
                                             received=1000, errors=0))
    out = await m.compare_load_run("l2")
    assert out["base_run_id"] == "l1" and out["base_found"] is True
    assert out["deltas"]["latency_p95"]["regressed"] is True
    assert out["deltas"]["latency_p95"]["delta"] == 20
    assert out["summary"]["regressions"] >= 1


async def test_compare_flags_throughput_drop_and_errors(store):
    store.create("l1", "mock", 1, label="load:steady")
    store.finish("l1", "completed", _summary(
        p50=10, p95=20, p99=30, tps=1000, received=1000, errors=0))
    store.create("l2", "mock", 1, label="load:steady")
    store.finish("l2", "completed", _summary(
        p50=10, p95=20, p99=30, tps=600, received=900, errors=100,
        cats={"connect/TimeoutError": 80, "ConnectionRefusedError": 20}))
    out = await m.compare_load_run("l2")
    # throughput fell → tps regressed; error rate rose from 0 → 10%
    assert out["deltas"]["tps"]["regressed"] is True
    assert out["deltas"]["error_rate"]["regressed"] is True
    cats = {c["category"]: c for c in out["error_categories"]}
    assert cats["connect/TimeoutError"]["current"] == 80
    assert cats["connect/TimeoutError"]["delta"] == 80          # new this run


async def test_compare_no_baseline(store):
    store.create("l1", "mock", 1, label="load:steady")
    store.finish("l1", "completed", _summary(p50=10, p95=20, p99=30, tps=1000,
                                             received=1000, errors=0))
    out = await m.compare_load_run("l1")
    assert out["base_found"] is False
    assert out["deltas"]["latency_p95"]["direction"] == "new"
    assert out["deltas"]["latency_p95"]["regressed"] is False


async def test_compare_explicit_target(store):
    store.create("a", "mock", 1, label="load:steady")
    store.finish("a", "completed", _summary(p50=10, p95=20, p99=30, tps=1000,
                                            received=1000, errors=0))
    store.create("b", "mock", 1, label="load:steady")
    store.finish("b", "completed", _summary(p50=5, p95=10, p99=15, tps=1200,
                                            received=1200, errors=0))
    out = await m.compare_load_run("b", to="a")
    assert out["base_run_id"] == "a"
    # faster + higher TPS → improvements, no regressions
    assert out["deltas"]["latency_p95"]["improved"] is True
    assert out["deltas"]["tps"]["improved"] is True
    assert out["summary"]["regressions"] == 0


async def test_compare_unknown_run_404(store):
    with pytest.raises(Exception) as exc:
        await m.compare_load_run("nope")
    assert "404" in str(exc.value) or "not found" in str(exc.value).lower()
