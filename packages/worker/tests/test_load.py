"""Tests for the distributed load-testing subsystem.

Covered:
* histogram percentiles + exact bucket-wise merge across "workers"
* profile sharding math (TPS + connections sum back to the fleet total)
* the steady-rate driver hits its target transaction count within tolerance
* the soak driver opens the right number of clients and tracks reconnects
* the coordinator aggregates in-process workers into one merged snapshot
"""

from __future__ import annotations

import asyncio

import pytest

from worker.engine.load import (
    LoadProfile,
    LatencyHistogram,
    LoadDriver,
    InMemoryLoadBus,
    merge_samples,
    STEADY,
    SOAK,
)

# -- histogram ---------------------------------------------------------------


def test_histogram_percentiles_uniform():
    h = LatencyHistogram()
    for v in range(1, 1001):  # 1..1000 ms
        h.record(v)
    assert h.count == 1000
    # bucket midpoints, so allow a relative tolerance ~ bucket width
    assert h.percentile(50) == pytest.approx(500, rel=0.05)
    assert h.percentile(95) == pytest.approx(950, rel=0.05)
    assert h.percentile(99) == pytest.approx(990, rel=0.05)
    assert h.min == 1
    assert h.max == 1000


def test_histogram_merge_is_exact_bucketwise():
    full = LatencyHistogram()
    a = LatencyHistogram()
    b = LatencyHistogram()
    for i in range(10_000):
        v = (i % 500) + 1
        full.record(v)
        (a if i % 2 == 0 else b).record(v)
    merged = LatencyHistogram()
    merged.merge_snapshot(a.snapshot())
    merged.merge_snapshot(b.snapshot())
    assert merged.count == full.count
    # merged buckets equal the single-histogram buckets exactly
    assert merged.counts == full.counts
    for p in (50, 95, 99):
        assert merged.percentile(p) == full.percentile(p)


def test_histogram_clamps_to_observed_range():
    h = LatencyHistogram()
    h.record(5)
    h.record(7)
    assert h.min <= h.percentile(50) <= h.max


def test_merge_samples_sums_fleet_tps():
    # regression: the merged snapshot must carry throughput, not drop it —
    # fleet TPS is the sum of each worker's current rate.
    samples = [
        {"sent": 100, "received": 95, "tps": 47.5, "target_tps": 50, "elapsed_s": 2.0},
        {"sent": 100, "received": 96, "tps": 48.0, "target_tps": 50, "elapsed_s": 2.0},
    ]
    merged = merge_samples(samples)
    assert merged.tps == pytest.approx(95.5)
    assert merged.target_tps == pytest.approx(100)
    assert merged.summary()["tps"] == pytest.approx(95.5)


def test_categorize_error_keys_by_type_not_message():
    # the same failure type with different messages must collapse to one bucket
    c = LoadDriver.categorize_error
    assert c("connect: TimeoutError: [Errno 60] host a") == "connect/TimeoutError"
    assert c("connect: TimeoutError: [Errno 60] host b") == "connect/TimeoutError"
    assert c("reconnect: ConnectionRefusedError: x") == "reconnect/ConnectionRefusedError"
    assert c("ValueError: bad field") == "ValueError"
    assert c("") == "error"


def test_merge_samples_sums_error_categories():
    samples = [
        {"errors": 3, "error_categories": {"connect/TimeoutError": 2, "ValueError": 1}},
        {"errors": 2, "error_categories": {"connect/TimeoutError": 2}},
    ]
    merged = merge_samples(samples)
    assert merged.error_categories == {"connect/TimeoutError": 4, "ValueError": 1}
    # summary surfaces them sorted by count (highest first)
    cats = merged.summary()["error_categories"]
    assert list(cats) == ["connect/TimeoutError", "ValueError"]


async def test_driver_records_error_categories():
    shard = LoadProfile(type=STEADY, duration_s=0.5, workers=1, target_tps=100).shard(0)
    shard.run_id = "r"
    n = {"i": 0}

    async def run_once():
        n["i"] += 1
        # every other call fails with a categorisable error
        if n["i"] % 2 == 0:
            return False, 0.0, "connect: TimeoutError: simulated"
        return True, 2.0

    driver = LoadDriver(shard, run_once=run_once, sample_interval_s=0.2)
    stats = await driver.run()
    assert stats.errors > 0
    assert driver.error_categories.get("connect/TimeoutError", 0) == stats.errors
    assert driver.sample()["error_categories"]["connect/TimeoutError"] == stats.errors


# -- sharding ----------------------------------------------------------------


def test_steady_sharding_sums_to_fleet_target():
    p = LoadProfile(type=STEADY, duration_s=10, workers=7, target_tps=20_000)
    shards = p.shards("r1")
    assert len(shards) == 7
    assert sum(s.target_tps for s in shards) == 20_000
    # evenly spread: max-min <= 1 transaction/sec
    tps = [s.target_tps for s in shards]
    assert max(tps) - min(tps) <= 1


def test_soak_sharding_sums_to_fleet_connections():
    p = LoadProfile(
        type=SOAK, duration_s=0, workers=9, connections=100_000, heartbeat_interval_s=30
    )
    shards = p.shards("r1")
    assert sum(s.connections for s in shards) == 100_000


def test_ramp_and_spike_rate_function():
    ramp = LoadProfile(
        type="ramp", duration_s=10, workers=1, start_tps=0, end_tps=100, ramp_s=10
    ).shard(0)
    assert ramp.target_tps_at(0) == pytest.approx(0)
    assert ramp.target_tps_at(5) == pytest.approx(50)
    assert ramp.target_tps_at(10) == pytest.approx(100)

    spike = LoadProfile(
        type="spike",
        duration_s=20,
        workers=1,
        base_tps=10,
        spike_tps=100,
        spike_every_s=10,
        spike_duration_s=2,
    ).shard(0)
    assert spike.target_tps_at(0) == 10  # base
    assert spike.target_tps_at(9) == 100  # last 2s of the cycle
    assert spike.target_tps_at(11) == 10  # back to base in next cycle


# -- driver ------------------------------------------------------------------


async def test_steady_driver_hits_target_count():
    shard = LoadProfile(type=STEADY, duration_s=1.0, workers=1, target_tps=200).shard(0)
    shard.run_id = "r"

    async def run_once():
        return True, 1.0

    driver = LoadDriver(shard, run_once=run_once, sample_interval_s=0.2)
    stats = await driver.run()
    # ~200 over 1s; pacing + drain tolerance
    assert 150 <= stats.sent <= 260
    assert stats.received == stats.sent
    assert stats.errors == 0


async def test_steady_driver_counts_errors():
    shard = LoadProfile(type=STEADY, duration_s=0.5, workers=1, target_tps=100).shard(0)
    shard.run_id = "r"
    n = {"i": 0}

    async def run_once():
        n["i"] += 1
        return (n["i"] % 2 == 0), 2.0

    driver = LoadDriver(shard, run_once=run_once, sample_interval_s=0.2)
    stats = await driver.run()
    assert stats.errors > 0
    assert stats.received + stats.errors == stats.sent


async def test_soak_driver_opens_clients_and_reconnects():
    shard = LoadProfile(
        type=SOAK, duration_s=0.4, workers=1, connections=5, heartbeat_interval_s=0.05
    ).shard(0)
    shard.run_id = "r"
    opened = {"n": 0}

    class Client:
        def __init__(self):
            self.beats = 0

        async def heartbeat(self):
            self.beats += 1
            # fail the 2nd beat once to force a reconnect path
            return (self.beats != 2), 1.0

        async def aclose(self):
            return None

    async def open_client():
        opened["n"] += 1
        return Client()

    driver = LoadDriver(shard, open_client=open_client, sample_interval_s=0.1)
    stats = await driver.run()
    assert opened["n"] >= 5  # at least one client per connection
    assert stats.reconnects >= 1
    assert stats.sent > 0


# -- coordinator end-to-end (in-process workers) -----------------------------


async def test_coordinator_aggregates_inproc_workers():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()
    backbone = InMemoryStreamBackbone()

    # a fake in-process worker: claim a shard, drive a trivial run_once, report
    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)

        async def run_once():
            return True, 3.0

        async def on_sample(s):
            s["worker_id"] = f"w{shard.worker_index}"
            await b.report_sample(run_id, s)

        driver = LoadDriver(shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.1)
        return await driver.run()

    coord = LoadCoordinator(
        bus, backbone, run_store=None, in_proc_worker=fake_worker, emit_interval_s=0.1, grace_s=0.5
    )
    profile = LoadProfile(type=STEADY, duration_s=0.6, workers=3, target_tps=150)
    run = await coord.start("run-x", profile, env={}, scenario={"id": "s"})
    # wait for the aggregation/lifecycle tasks to finish
    await asyncio.gather(*run._tasks, return_exceptions=True)

    assert run.status == "completed"
    assert run.summary is not None
    # 3 workers × ~50 tps × 0.6s ≈ 90 sent; tolerate pacing/drain
    assert run.summary["sent"] >= 40
    assert run.summary["latency_ms"]["samples"] == run.summary["received"]
    assert run.summary["workers"] == 3


def test_read_rss_bytes_is_positive():
    """The dependency-free RSS reader returns this process's resident memory."""
    from worker.load_worker import read_rss_bytes

    rss = read_rss_bytes()
    assert isinstance(rss, int)
    assert rss > 0  # the test process itself is using memory


@pytest.mark.asyncio
async def test_coordinator_exports_worker_rss_gauge():
    """Workers ship rss_bytes in their samples; the coordinator surfaces it as a
    per-worker Prometheus gauge (the soak/leak-trend series) while the run is
    live, then drops the series when the run finishes."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from api.observability import LOAD_WORKER_RSS
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()
    backbone = InMemoryStreamBackbone()
    rss_value = 222_333_444

    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)

        async def run_once():
            return True, 3.0

        async def on_sample(s):
            s["worker_id"] = f"w{shard.worker_index}"
            s["rss_bytes"] = rss_value  # what load_worker stamps in real runs
            await b.report_sample(run_id, s)

        driver = LoadDriver(shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.1)
        return await driver.run()

    coord = LoadCoordinator(
        bus,
        backbone,
        run_store=None,
        in_proc_worker=fake_worker,
        emit_interval_s=0.1,
        grace_s=0.5,
    )
    profile = LoadProfile(type=STEADY, duration_s=2.0, workers=1, target_tps=50)
    run = await coord.start("run-rss", profile, env={}, scenario={"id": "s"})

    # while the run is live the gauge carries the worker's RSS
    seen = False
    for _ in range(40):
        await asyncio.sleep(0.1)
        text = LOAD_WORKER_RSS.render()
        if 'run_id="run-rss"' in text and str(rss_value) in text:
            seen = True
            break
    assert seen, "expected payprobe_load_worker_rss_bytes to carry the worker RSS"

    await coord.stop("run-rss")
    await asyncio.gather(*run._tasks, return_exceptions=True)

    # once finished, the per-worker series is cleaned up
    assert 'run_id="run-rss"' not in LOAD_WORKER_RSS.render()


async def test_coordinator_registers_and_unregisters_with_run_control():
    """A load run must be registered with run_control so any replica can stop
    it, and unregistered when it finishes."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    events: list[tuple[str, str]] = []

    class FakeControl:
        async def register(self, run_id, kind="run"):
            events.append(("register", run_id, kind))

        async def unregister(self, run_id):
            events.append(("unregister", run_id))

    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)

        async def run_once():
            return True, 2.0

        async def on_sample(s):
            s["worker_id"] = f"w{shard.worker_index}"
            await b.report_sample(run_id, s)

        return await LoadDriver(
            shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.1
        ).run()

    coord = LoadCoordinator(
        InMemoryLoadBus(),
        InMemoryStreamBackbone(),
        run_store=None,
        in_proc_worker=fake_worker,
        emit_interval_s=0.1,
        grace_s=0.3,
        run_control=FakeControl(),
    )
    run = await coord.start(
        "run-rc",
        LoadProfile(type=STEADY, duration_s=0.4, workers=2, target_tps=80),
        env={},
        scenario={"id": "s"},
    )
    # registered immediately on start
    assert ("register", "run-rc", "load") in events
    await asyncio.gather(*run._tasks, return_exceptions=True)
    # unregistered once finished
    assert ("unregister", "run-rc") in events


async def test_coordinator_waits_for_drain_before_finalizing():
    """A duration-elapsed, saturated run must let the fleet drain its in-flight
    backlog before it's finalized — the final summary reconciles (pending == 0,
    everything sent is received), not a truncated roll-up that drops outstanding
    transactions the moment the send window closes."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()

    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)

        async def run_once():
            await asyncio.sleep(0.05)  # slow target ⇒ a backlog builds behind the cap
            return True, 50.0

        async def on_sample(s):
            s["worker_id"] = "w0"
            await b.report_sample(run_id, s)

        return await LoadDriver(
            shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.1
        ).run()

    backbone = InMemoryStreamBackbone()
    coord = LoadCoordinator(
        bus,
        backbone,
        in_proc_worker=fake_worker,
        emit_interval_s=0.1,
        grace_s=0.3,
    )
    # unbounded backlog + a small concurrency cap ⇒ a real queue forms that takes
    # measurable time to drain; drain_s < 0 ⇒ wait for every launched txn.
    profile = LoadProfile(
        type=STEADY,
        duration_s=0.4,
        workers=1,
        target_tps=200,
        max_in_flight_per_worker=5,
        max_backlog_per_worker=-1,
        drain_s=-1.0,
    )
    run = await coord.start("run-drain", profile, env={}, scenario={"id": "s"})
    await asyncio.gather(*run._tasks, return_exceptions=True)

    summ = run.summary
    assert summ["status"] == "completed"
    assert summ["pending"] == 0  # nothing left outstanding
    assert summ["received"] == summ["sent"]  # every send resolved
    assert summ["sent"] == summ["received"] + summ["errors"] + summ.get("aborted", 0)

    # the drain phase was surfaced live: at least one emitted sample carried the
    # ``draining`` flag so the portal can show the drain in progress.
    drain_samples = [
        e
        for _, e in backbone.history("run-drain")
        if e.get("type") == "load.sample" and (e.get("data") or {}).get("draining")
    ]
    assert drain_samples, "expected at least one load.sample flagged draining"
    assert drain_samples[0]["data"].get("drain_s") == -1.0

    # elapsed is split into the send window and the drain, not lumped together.
    assert summ["drain_elapsed_s"] > 0  # the drain took real time
    assert summ["send_elapsed_s"] >= 0
    assert summ["elapsed_s"] == pytest.approx(
        summ["send_elapsed_s"] + summ["drain_elapsed_s"], abs=0.3
    )


async def test_drain_budget_is_a_max_not_a_fixed_wait():
    """``drain_s`` is a maximum: a healthy run with nothing outstanding finalizes
    immediately after the send window, it does NOT sit for the whole budget."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()

    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)

        async def run_once():
            return True, 1.0  # instant ⇒ no backlog, nothing to drain

        async def on_sample(s):
            s["worker_id"] = "w0"
            await b.report_sample(run_id, s)

        return await LoadDriver(
            shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.05
        ).run()

    coord = LoadCoordinator(
        bus,
        InMemoryStreamBackbone(),
        in_proc_worker=fake_worker,
        emit_interval_s=0.05,
        grace_s=0.2,
    )
    # a generous 30s drain budget, but nothing is in flight at the end.
    profile = LoadProfile(type=STEADY, duration_s=0.3, workers=1, target_tps=100, drain_s=30.0)
    t0 = asyncio.get_event_loop().time()
    run = await coord.start("run-fastdrain", profile, env={}, scenario={"id": "s"})
    await asyncio.gather(*run._tasks, return_exceptions=True)
    wall = asyncio.get_event_loop().time() - t0

    assert run.summary["status"] == "completed"
    assert wall < 5.0  # ended early, didn't wait out 30s
    assert run.summary.get("drain_elapsed_s", 0) < 5.0


async def test_duration_clock_starts_when_fleet_is_driving():
    """Worker startup latency must not eat the run's send window. A worker that
    takes a while to provision/warm up should still drive for the FULL duration —
    the coordinator starts the duration clock at first sample, not run creation."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()
    STARTUP, DURATION = 0.5, 0.4

    async def fake_worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        shard = Shard.from_dict(payload)
        await asyncio.sleep(STARTUP)  # simulate container provisioning + warmup
        stop = asyncio.Event()

        async def poller():
            while not stop.is_set():
                if await b.is_stopped(run_id):
                    stop.set()
                    return
                await asyncio.sleep(0.05)

        async def run_once():
            return True, 1.0

        async def on_sample(s):
            s["worker_id"] = "w0"
            await b.report_sample(run_id, s)

        p = asyncio.create_task(poller())
        try:
            return await LoadDriver(
                shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.05
            ).run(stop)
        finally:
            p.cancel()

    coord = LoadCoordinator(
        bus,
        InMemoryStreamBackbone(),
        in_proc_worker=fake_worker,
        emit_interval_s=0.05,
        grace_s=0.2,
    )
    profile = LoadProfile(type=STEADY, duration_s=DURATION, workers=1, target_tps=200)
    run = await coord.start("run-ready", profile, env={}, scenario={"id": "s"})
    await asyncio.gather(*run._tasks, return_exceptions=True)

    # Full duration of driving ⇒ ~200*0.4 = 80 sent. Without the readiness gate the
    # coordinator would have stopped the worker the instant it started (≈0 sent).
    assert run.summary["sent"] >= 40
    # and elapsed reflects driving time (~duration), not startup + duration.
    assert run.summary["elapsed_s"] <= DURATION + 1.0


async def test_await_ready_waits_through_transient_notice():
    """A transient 'fleet short' notice must NOT start the duration clock — the
    gate waits for a real sample. Only a terminal ``startup_failed`` bails early.
    (Regression: a slow provision that briefly reports 'short' used to reset the
    clock and cut the run to a few seconds.)"""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator, LoadRun
    from worker.engine import InMemoryStreamBackbone

    coord = LoadCoordinator(
        InMemoryLoadBus(),
        InMemoryStreamBackbone(),
        startup_timeout_s=5.0,
    )
    run = LoadRun("r", LoadProfile(type=STEADY, duration_s=1, workers=1, target_tps=10))

    # a notice alone (workers still coming up) must not end the wait
    run.notice = "Fleet short: 0/1 worker(s) running"

    async def report_late():
        await asyncio.sleep(0.4)
        run.latest["w0"] = {"sent": 1, "received": 1}

    t0 = asyncio.get_event_loop().time()
    await asyncio.gather(coord._await_ready(run), report_late())
    waited = asyncio.get_event_loop().time() - t0
    assert 0.3 < waited < 2.0  # returned on the sample, not the 5s timeout
    assert run.drive_started_at > run.started_at  # clock stamped at first sample


async def test_await_ready_bails_on_terminal_failure():
    """When no worker will ever start (``startup_failed``), the gate returns at
    once instead of blocking the whole startup timeout."""
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator, LoadRun
    from worker.engine import InMemoryStreamBackbone

    coord = LoadCoordinator(
        InMemoryLoadBus(),
        InMemoryStreamBackbone(),
        startup_timeout_s=30.0,
    )
    run = LoadRun("r", LoadProfile(type=STEADY, duration_s=1, workers=1, target_tps=10))
    run.startup_failed = True

    t0 = asyncio.get_event_loop().time()
    await coord._await_ready(run)
    assert asyncio.get_event_loop().time() - t0 < 1.0  # did not wait out 30s


class _NonInMemBus:
    """A bus that is NOT InMemoryLoadBus, so the coordinator treats it as a
    'Redis fleet' and takes the external-or-fallback path."""

    def __init__(self):
        from worker.engine.load import InMemoryLoadBus as _M

        self._m = _M()

    async def push_shards(self, r, s):
        await self._m.push_shards(r, s)

    async def claim_shard(self, r, timeout_s=10.0):
        return await self._m.claim_shard(r, timeout_s=timeout_s)

    async def pending_shards(self, r):
        return await self._m.pending_shards(r)

    async def report_sample(self, r, s):
        await self._m.report_sample(r, s)

    def samples(self, r):
        return self._m.samples(r)

    async def signal_stop(self, r):
        await self._m.signal_stop(r)

    async def is_stopped(self, r):
        return await self._m.is_stopped(r)

    async def mark_done(self, r):
        await self._m.mark_done(r)


def _coord(bus, **kw):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator
    from worker.engine import InMemoryStreamBackbone

    async def worker(b, run_id):
        from worker.engine.load import Shard

        payload = await b.claim_shard(run_id, timeout_s=5)
        if payload is None:
            return None
        shard = Shard.from_dict(payload)

        async def run_once():
            return True, 1.0

        async def on_sample(s):
            s["worker_id"] = f"w{shard.worker_index}"
            await b.report_sample(run_id, s)

        return await LoadDriver(
            shard, run_once=run_once, on_sample=on_sample, sample_interval_s=0.1
        ).run()

    return LoadCoordinator(
        bus,
        InMemoryStreamBackbone(),
        run_store=None,
        in_proc_worker=worker,
        emit_interval_s=0.1,
        grace_s=0.3,
        worker_grace_s=0.3,
        **kw,
    )


async def test_inproc_fallback_refused_above_cap():
    """No external workers + demand over the cap ⇒ refuse in-process, set a
    notice, drive nothing (protects the orchestrator loop)."""
    coord = _coord(_NonInMemBus(), inproc_max_tps=1000)
    run = await coord.start(
        "big",
        LoadProfile(type=STEADY, duration_s=0.6, workers=2, target_tps=20_000),
        env={},
        scenario={"id": "s"},
    )
    await asyncio.gather(*run._tasks, return_exceptions=True)
    assert run.notice is not None and "external workers" in run.notice
    assert run.summary["sent"] == 0  # nothing driven in-process


async def test_inproc_fallback_runs_below_cap():
    """Demand under the cap ⇒ fallback drives the load in-process."""
    coord = _coord(_NonInMemBus(), inproc_max_tps=1000)
    run = await coord.start(
        "small",
        LoadProfile(type=STEADY, duration_s=0.6, workers=2, target_tps=100),
        env={},
        scenario={"id": "s"},
    )
    await asyncio.gather(*run._tasks, return_exceptions=True)
    assert run.notice is None
    assert run.summary["sent"] > 0


# -- worker fleet presence ----------------------------------------------------


async def test_bus_worker_presence_list_and_drain():
    """Heartbeats show up in list_workers; a per-worker stop flag marks the row
    draining and is observable by the worker; drop removes it."""
    bus = InMemoryLoadBus()
    await bus.worker_heartbeat("w-1", {"run_id": "r1", "tps": 40, "errors": 0})
    await bus.worker_heartbeat("w-2", {"run_id": "r1", "tps": 55, "errors": 3})

    rows = {w["worker_id"]: w for w in await bus.list_workers()}
    assert set(rows) == {"w-1", "w-2"}
    assert rows["w-1"]["tps"] == 40
    assert rows["w-1"]["draining"] is False

    # drain one — flag is set, the row reflects it, and the worker can see it
    await bus.signal_worker_stop("w-1")
    assert await bus.is_worker_stopped("w-1") is True
    assert await bus.is_worker_stopped("w-2") is False
    rows = {w["worker_id"]: w for w in await bus.list_workers()}
    assert rows["w-1"]["draining"] is True

    # deregister removes it from the fleet view
    await bus.drop_worker("w-2")
    assert {w["worker_id"] for w in await bus.list_workers()} == {"w-1"}


async def test_bus_worker_presence_expires():
    """A worker that stops heartbeating drops out of the view past the TTL."""
    from worker.engine.load.bus import WORKER_TTL_S

    bus = InMemoryLoadBus()
    await bus.worker_heartbeat("ghost", {"run_id": "r1"})
    assert len(await bus.list_workers()) == 1

    # backdate the last-seen beyond the TTL → pruned on next list
    seen, info = bus._workers["ghost"]
    bus._workers["ghost"] = (seen - WORKER_TTL_S - 1, info)
    assert await bus.list_workers() == []
    assert "ghost" not in bus._workers  # self-cleaned


# -- weighted traffic mix ----------------------------------------------------


def test_weighted_picker_respects_weights():
    from worker.load_worker import WeightedPicker

    picker = WeightedPicker([80, 15, 5], seed=42)
    counts = [0, 0, 0]
    for _ in range(10_000):
        counts[picker.pick()] += 1
    # roughly the requested proportions (generous tolerance for sampling noise)
    assert counts[0] / 10_000 == pytest.approx(0.80, abs=0.03)
    assert counts[1] / 10_000 == pytest.approx(0.15, abs=0.03)
    assert counts[2] / 10_000 == pytest.approx(0.05, abs=0.02)


def test_weighted_picker_zero_weights_fall_back_to_uniform():
    from worker.load_worker import WeightedPicker

    picker = WeightedPicker([0, 0], seed=1)
    picks = [picker.pick() for _ in range(1000)]
    assert set(picks) == {0, 1}  # both reachable


async def test_weighted_run_once_single_scenario_is_passthrough():
    from worker.engine import WorkerEngine
    from worker.load_worker import make_weighted_run_once

    eng = WorkerEngine({})
    sc = {"id": "noop", "steps": []}
    fn = make_weighted_run_once(eng, [sc], [1.0])
    ok, latency, err = await fn()
    assert ok and err is None


# -- saturation / backpressure ----------------------------------------------


async def test_backpressure_saturates_and_throttles():
    """A slow target with a tiny in-flight cap saturates → throttled counts rise,
    and the offered load splits cleanly: generated == sent + throttled, with
    throttled attempts NEVER counted as sent."""
    shard = LoadProfile(
        type=STEADY,
        duration_s=0.3,
        workers=1,
        target_tps=300,
        max_in_flight_per_worker=2,
    ).shard(0)
    shard.run_id = "r"

    async def run_once():
        await asyncio.sleep(0.02)  # slow target — dispatch can't keep up
        return True, 20.0

    driver = LoadDriver(shard, run_once=run_once, sample_interval_s=0.1)
    stats = await driver.run()
    assert stats.max_in_flight == 2
    assert stats.peak_in_flight <= 2  # never exceeds the cap
    assert stats.throttled > 0  # target is the bottleneck
    assert stats.sent < stats.generated  # not everything generated was sent
    assert stats.generated == stats.sent + stats.throttled  # offered load splits cleanly
    snap = driver.sample()
    assert snap["generated"] == snap["sent"] + snap["throttled"]


def test_merge_samples_sums_saturation_fields():
    a = {"in_flight": 3, "max_in_flight": 10, "peak_in_flight": 7, "throttled": 5}
    b = {"in_flight": 4, "max_in_flight": 10, "peak_in_flight": 9, "throttled": 2}
    out = merge_samples([a, b])
    assert out.in_flight == 7 and out.max_in_flight == 20
    assert out.peak_in_flight == 16 and out.throttled == 7
    assert out.saturation == pytest.approx(7 / 20)
    assert out.summary()["saturation"] == pytest.approx(0.35)


# -- drain / end-of-run in-flight handling -----------------------------------


def _saturating_shard(drain_s: float):
    """A shard whose target far outpaces a slow, in-flight-capped worker, so a
    backlog of launched-but-unfinished transactions builds up by end-of-run.

    ``max_backlog_per_worker=-1`` keeps the legacy *unbounded* pacing so the
    backlog actually forms — this exercises the drain in isolation. (The default
    backlog cap is tested separately in ``test_backlog_cap_bounds_outstanding``.)
    """
    shard = LoadProfile(
        type=STEADY,
        duration_s=0.2,
        workers=1,
        target_tps=500,
        max_in_flight_per_worker=2,
        max_backlog_per_worker=-1,
        drain_s=drain_s,
    ).shard(0)
    shard.run_id = "r"
    return shard


async def test_drain_zero_aborts_backlog_but_accounts_it():
    """drain_s=0 cuts immediately: the backlog is aborted, not left as phantom
    'pending'. Accounting stays exact: sent = received + errors + aborted."""
    shard = _saturating_shard(drain_s=0.0)

    async def run_once():
        await asyncio.sleep(0.05)  # slow target — a backlog piles up
        return True, 50.0

    stats = await LoadDriver(shard, run_once=run_once, sample_interval_s=0.1).run()
    assert stats.aborted > 0  # backlog was cancelled
    assert stats.pending == 0  # nothing left unaccounted
    assert stats.sent == stats.received + stats.errors + stats.aborted


async def test_drain_negative_waits_for_all_in_flight():
    """drain_s < 0 waits until every launched transaction finishes — no aborts,
    no pending, and every send is eventually received."""
    shard = LoadProfile(
        type=STEADY,
        duration_s=0.2,
        workers=1,
        target_tps=100,
        max_in_flight_per_worker=4,
        max_backlog_per_worker=-1,
        drain_s=-1.0,
    ).shard(0)
    shard.run_id = "r"

    async def run_once():
        await asyncio.sleep(0.01)
        return True, 10.0

    stats = await LoadDriver(shard, run_once=run_once, sample_interval_s=0.1).run()
    assert stats.aborted == 0
    assert stats.pending == 0
    assert stats.received == stats.sent  # all in-flight work drained


async def test_drain_positive_bounded_then_aborts_remainder():
    """A short positive drain lets some outstanding work finish, then aborts the
    rest — still fully accounted."""
    shard = _saturating_shard(drain_s=0.05)

    async def run_once():
        await asyncio.sleep(0.05)
        return True, 50.0

    stats = await LoadDriver(shard, run_once=run_once, sample_interval_s=0.1).run()
    assert stats.aborted > 0
    assert stats.pending == 0
    assert stats.sent == stats.received + stats.errors + stats.aborted
    assert stats.aborted == stats.summary()["aborted"]


def test_merge_samples_sums_aborted():
    out = merge_samples([{"aborted": 4}, {"aborted": 7}])
    assert out.aborted == 11
    assert out.summary()["aborted"] == 11


async def test_backlog_cap_bounds_outstanding():
    """With the default backlog cap (0 ⇒ cap at the concurrency limit), a slow
    target does NOT accumulate an unbounded backlog: outstanding stays within the
    cap, the shortfall is counted as ``throttled``, and ``sent`` reflects what was
    actually dispatched (far below target*duration) — so the run reconciles in a
    short drain instead of leaving thousands 'pending'."""
    cap = 5
    shard = LoadProfile(
        type=STEADY,
        duration_s=0.3,
        workers=1,
        target_tps=1000,  # far outruns the target
        max_in_flight_per_worker=cap,  # max_backlog defaults to 0 ⇒ cap at `cap`
        drain_s=2.0,
    ).shard(0)
    shard.run_id = "r"

    async def run_once():
        await asyncio.sleep(0.02)  # slow target
        return True, 20.0

    stats = await LoadDriver(shard, run_once=run_once, sample_interval_s=0.1).run()
    # outstanding never exceeded the cap …
    assert stats.peak_in_flight <= cap
    # … the target couldn't keep up, so the shortfall shows as throttled …
    assert stats.throttled > 0
    # … sent tracks real dispatch, nowhere near the uncapped 1000*0.3 = 300 …
    assert stats.sent < 100
    # … the offered load (generated) is much larger and splits cleanly …
    assert stats.generated > stats.sent
    assert stats.generated == stats.sent + stats.throttled
    # … and everything reconciles: nothing left pending.
    assert stats.pending == 0
    assert stats.sent == stats.received + stats.errors + stats.aborted


def test_unbounded_backlog_opt_out_inflates_sent():
    """A shard with ``max_backlog < 0`` keeps the legacy open-loop pacing — the
    cap is disabled (regression guard for the opt-out)."""
    sh = LoadProfile(
        type=STEADY,
        duration_s=1,
        workers=1,
        target_tps=100,
        max_in_flight_per_worker=2,
        max_backlog_per_worker=-1,
    ).shard(0)
    d = LoadDriver(sh, run_once=lambda: None)
    assert d._backlog_cap() is None
    sh2 = LoadProfile(
        type=STEADY,
        duration_s=1,
        workers=1,
        target_tps=100,
        max_in_flight_per_worker=7,  # default max_backlog 0 ⇒ cap at 7
    ).shard(0)
    assert LoadDriver(sh2, run_once=lambda: None)._backlog_cap() == 7


# -- hot re-tune -------------------------------------------------------------


def test_shard_apply_retune_resplits_fleet_target():
    p = LoadProfile(type=STEADY, duration_s=10, workers=4, target_tps=400)
    shards = p.shards("r")
    for sh in shards:
        sh.apply_retune({"target_tps": 800})
    assert sum(sh.target_tps for sh in shards) == 800  # re-split across the fleet
    assert max(sh.target_tps for sh in shards) - min(sh.target_tps for sh in shards) <= 1


def test_shard_apply_retune_ignores_irrelevant_knobs():
    sh = LoadProfile(type=STEADY, duration_s=10, workers=1, target_tps=100).shard(0)
    sh.apply_retune({"base_tps": 999})  # spike knob — no effect on a steady shard
    assert sh.target_tps == 100


async def test_bus_retune_round_trip():
    bus = InMemoryLoadBus()
    assert await bus.get_retune("r1") is None
    await bus.signal_retune("r1", {"target_tps": 1234})
    assert (await bus.get_retune("r1"))["target_tps"] == 1234


async def test_coordinator_retune_signals_bus_and_updates_profile():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "orchestrator"))
    from api.load_coordinator import LoadCoordinator, LoadRun
    from worker.engine import InMemoryStreamBackbone

    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, InMemoryStreamBackbone(), run_store=None)
    # inject a live run without spawning workers
    profile = LoadProfile(type=STEADY, duration_s=10, workers=2, target_tps=100)
    coord.runs["r1"] = LoadRun("r1", profile)

    assert await coord.retune("r1", {"target_tps": 500}) is True
    assert (await bus.get_retune("r1"))["target_tps"] == 500
    assert coord.runs["r1"].profile.target_tps == 500  # snapshot reflects the new target
    # unknown run → False
    assert await coord.retune("ghost", {"target_tps": 1}) is False
    # nothing valid to tune → False
    assert await coord.retune("r1", {"nonsense": 1}) is False
