"""Standalone load worker — run many of these to make up a fleet.

    REDIS_URL=redis://... LOAD_RUN_ID=<id> python -m worker.load_worker

Each process claims exactly one shard of the run from the shared bus, builds the
transaction coroutine from the same adapter registry the functional engine uses,
drives it with :class:`LoadDriver`, and streams cumulative metric samples back.
N processes (containers, pods, hosts) coordinating through one Redis is how the
fleet reaches 20K TPS / 100K connections without any single event loop having
to.

The coordinator embeds the run's ``env`` + ``scenario`` in each shard payload,
so a worker needs nothing but the bus to start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import time
import uuid
from typing import Any, Awaitable, Callable

try:  # uvloop is a ~2x event-loop win; optional for dev
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:  # noqa: BLE001
    pass

from .engine import WorkerEngine, NullSink, ScenarioRunner
from .engine.generators import GeneratorContext
from .engine.load import LoadDriver, Shard, RedisLoadBus, SOAK

log = logging.getLogger("payprobe.load_worker")

#: How often a worker refreshes its presence. Comfortably inside WORKER_TTL_S
#: (engine.load.bus) so the fleet panel drops a dead worker within a few seconds.
WORKER_HEARTBEAT_S = 5.0


def read_rss_bytes() -> int:
    """This process's resident set size in bytes, dependency-free.

    Soak/leak hunting needs each worker's memory trended over time. We avoid a
    ``psutil`` dependency: on Linux ``/proc/self/statm`` gives RSS in pages, which
    is exact and cheap; everywhere else (and if ``/proc`` is unavailable) we fall
    back to ``resource.getrusage`` whose ``ru_maxrss`` is KiB on Linux but bytes
    on macOS/BSD. Returns 0 if nothing is readable rather than raising — a missing
    sample must never break the metric stream."""
    try:
        with open("/proc/self/statm", "r") as fh:
            rss_pages = int(fh.readline().split()[1])
        return rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        import sys

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kibibytes on Linux/BSD.
        return int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
    except Exception:  # noqa: BLE001 — best-effort; absence is not an error
        return 0


# -- building the transaction coroutine from a scenario ----------------------


def make_run_once(engine: WorkerEngine, scenario: dict, generators: GeneratorContext | None = None):
    """A coroutine that runs ``scenario`` once and returns (ok, latency_ms, error).

    Per-step events are dropped (NullSink) — at load rates the event stream is
    the metric samples, not per-transaction traces. On failure a concise reason
    is returned so the operator can see *why* every transaction is erroring.

    ``generators`` is shared across every transaction so ``${seq.*}`` counters
    advance and ``${rand.*}`` / ``${pool.*}`` vary per transaction instead of
    every message carrying the same PAN/STAN/RRN.
    """

    async def run_once() -> tuple[bool, float, str | None]:
        runner = ScenarioRunner(
            NullSink(),
            run_id="load",
            secrets=scenario.get("__secrets__"),
            generators=generators,
        )
        start = time.monotonic()
        result = await runner.run(scenario, engine._execute_step, phase=2)
        latency_ms = (time.monotonic() - start) * 1000.0
        if result.passed:
            return True, latency_ms, None
        return False, latency_ms, _failure_reason(result)

    return run_once


def _failure_reason(result) -> str:
    """A short human reason from the first failed step of a scenario result."""
    for step in getattr(result, "steps", []) or []:
        if getattr(step, "status", "") in ("failed", "error"):
            if step.error:
                return f"{step.target}.{step.action}: {step.error}"
            for a in getattr(step, "assertions", []) or []:
                if not getattr(a, "passed", True):
                    return (
                        f"{step.target}.{step.action}: assertion {a.field} "
                        f"{a.operator} {a.expected!r} (got {a.actual!r})"
                    )
            return f"{step.target}.{step.action}: {step.status}"
    return "scenario failed"


class WeightedPicker:
    """Reproducible weighted chooser over indices ``0..n-1``.

    A real switch sees a *mix* of transaction types (e.g. 80% payment, 15%
    refund, 5% reversal), not one type. This picks the scenario to run for each
    transaction by weight, seeded so a run is repeatable per worker."""

    def __init__(self, weights: list[float], seed: Any = None) -> None:
        self._w = [max(0.0, float(x)) for x in weights]
        self._total = sum(self._w)
        if self._total <= 0:  # degenerate → uniform
            self._w = [1.0] * len(weights)
            self._total = float(len(weights)) or 1.0
        self._rng = random.Random(seed)

    def pick(self) -> int:
        r = self._rng.random() * self._total
        upto = 0.0
        for i, w in enumerate(self._w):
            upto += w
            if r < upto:
                return i
        return len(self._w) - 1  # float rounding fallback


def make_weighted_run_once(
    engine: WorkerEngine,
    scenarios: list[dict],
    weights: list[float],
    generators: GeneratorContext | None = None,
    *,
    seed: Any = None,
) -> Callable[[], Awaitable[tuple[bool, float, str | None]]]:
    """A run_once that picks one of several scenarios per transaction by weight.

    Falls back to the single-scenario path when only one scenario is supplied.
    All scenarios share the one generator context so ``${seq.*}`` counters and
    the card rotation advance across the whole mix, not per type."""
    runners = [make_run_once(engine, sc, generators) for sc in scenarios]
    if len(runners) == 1:
        return runners[0]
    picker = WeightedPicker(weights, seed=seed)

    async def run_once() -> tuple[bool, float, str | None]:
        return await runners[picker.pick()]()

    return run_once


def make_open_client(engine: WorkerEngine, scenario: dict, heartbeat_action: str | None):
    """Open a soak client whose ``heartbeat()`` exercises one action.

    A heartbeat is a single adapter action (default: the scenario's first step's
    target/action). True per-socket fan-out depends on the adapter holding a
    connection per client; pooled adapters share the pool, in which case
    ``connections`` measures concurrent virtual clients rather than sockets.
    """
    steps = scenario.get("steps") or []
    target = scenario.get("target") or (steps[0].get("target") if steps else None)
    action = heartbeat_action or (steps[0].get("action") if steps else "heartbeat")
    payload = scenario.get("heartbeat_payload") or (steps[0].get("payload") if steps else {}) or {}

    class _Client:
        async def heartbeat(self) -> tuple[bool, float]:
            adapter = await engine.registry.get(target)
            start = time.monotonic()
            sr = await adapter.execute(action, dict(payload))
            latency = sr.duration_ms or (time.monotonic() - start) * 1000.0
            return (not sr.failed), float(latency)

        async def aclose(self) -> None:
            return None

    async def open_client():
        return _Client()

    return open_client


# -- the worker loop ---------------------------------------------------------


async def run_worker(
    bus, run_id: str, *, claim_timeout_s: float = 30.0, worker_id: str | None = None
) -> dict[str, Any] | None:
    worker_id = worker_id or f"w-{uuid.uuid4().hex[:8]}"
    started_at = time.time()
    payload = await bus.claim_shard(run_id, timeout_s=claim_timeout_s)
    if payload is None:
        log.warning("[%s] no shard to claim for run %s", worker_id, run_id)
        return None

    shard = Shard.from_dict(payload)
    env = payload.get("env") or {}
    scenario = payload.get("scenario") or {}
    # weighted traffic mix: the coordinator may embed several scenarios + weights
    # (e.g. 80% payment / 15% refund / 5% reversal). Fall back to the single
    # ``scenario`` when no mix is supplied.
    scenarios = payload.get("scenarios") or ([scenario] if scenario else [])
    weights = payload.get("weights") or [1.0] * len(scenarios)
    heartbeat_action = payload.get("heartbeat_action")
    log.info(
        "[%s] claimed shard %d/%d type=%s",
        worker_id,
        shard.worker_index,
        shard.worker_count,
        shard.type,
    )

    engine = WorkerEngine(env)
    await engine.warmup_adapters()

    stop = asyncio.Event()
    #: most recent metric sample, shared with the heartbeat so the fleet view
    #: shows live per-worker TPS/errors without a second metrics path.
    last_sample: dict[str, Any] = {}

    async def stop_poller() -> None:
        # Stop on either the run-wide stop (whole run cancelled) or a stop aimed
        # at just this worker (operator drained it from the fleet panel). Also
        # pick up hot re-tunes and apply them to the shard — the driver reads
        # ``shard.target_tps_at`` each tick, so a new target takes effect within
        # one pacing tick without restarting the run.
        get_retune = getattr(bus, "get_retune", None)
        while not stop.is_set():
            if await bus.is_stopped(run_id) or await bus.is_worker_stopped(worker_id):
                stop.set()
                return
            if get_retune is not None:
                try:
                    params = await get_retune(run_id)
                    if params:
                        shard.apply_retune(params)
                except Exception as exc:  # noqa: BLE001 — re-tune is best-effort
                    log.debug("[%s] retune poll failed: %s", worker_id, exc)
            await asyncio.sleep(0.5)

    def _presence(status: str) -> dict[str, Any]:
        return {
            "worker_id": worker_id,
            "run_id": run_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "shard_index": shard.worker_index,
            "shard_count": shard.worker_count,
            "type": shard.type,
            "status": status,
            "started_at": started_at,
            "tps": last_sample.get("tps", 0),
            "sent": last_sample.get("sent", 0),
            "received": last_sample.get("received", 0),
            "errors": last_sample.get("errors", 0),
            "connections": last_sample.get("live", 0),
            "rss_bytes": read_rss_bytes(),
        }

    async def heartbeat() -> None:
        # Refresh well inside WORKER_TTL_S so a missed beat = process gone.
        try:
            while not stop.is_set():
                status = "draining" if await bus.is_worker_stopped(worker_id) else "running"
                await bus.worker_heartbeat(worker_id, _presence(status))
                await asyncio.sleep(WORKER_HEARTBEAT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — presence is best-effort
            log.debug("[%s] heartbeat failed: %s", worker_id, exc)

    async def on_sample(sample: dict[str, Any]) -> None:
        sample["worker_id"] = worker_id
        # Stamp resident memory so the coordinator can trend per-worker RSS for
        # soak/leak detection (Grafana "worker RSS" panel + leak alert).
        sample["rss_bytes"] = read_rss_bytes()
        last_sample.update(sample)
        await bus.report_sample(run_id, sample)

    if shard.type == SOAK:
        driver = LoadDriver(
            shard,
            open_client=make_open_client(engine, scenario, heartbeat_action),
            on_sample=on_sample,
        )
    else:
        # One generator context per shard, seeded from run + worker so each
        # worker draws an independent-but-reproducible stream of card data. The
        # whole mix shares it (so ${seq.*}/card rotation advance across types);
        # cards/terminals come from the primary scenario's resolved pools.
        primary = scenarios[0] if scenarios else scenario
        generators = GeneratorContext(
            seed=f"{run_id}:{shard.worker_index}",
            terminals=primary.get("terminals"),
            cards=primary.get("cards"),
        )
        driver = LoadDriver(
            shard,
            run_once=make_weighted_run_once(
                engine,
                scenarios,
                weights,
                generators,
                seed=f"{run_id}:{shard.worker_index}:mix",
            ),
            on_sample=on_sample,
        )

    # publish presence immediately so the worker shows up the instant it claims
    # a shard, not only after the first heartbeat tick.
    await bus.worker_heartbeat(worker_id, _presence("running"))
    poller = asyncio.create_task(stop_poller())
    beat = asyncio.create_task(heartbeat())
    try:
        stats = await driver.run(stop)
    finally:
        poller.cancel()
        beat.cancel()
        await engine.teardown()
        # leave the fleet view cleanly rather than waiting out the TTL
        try:
            await bus.drop_worker(worker_id)
        except Exception as exc:  # noqa: BLE001 — best-effort deregister
            log.debug("[%s] deregister failed: %s", worker_id, exc)
    summary = stats.summary()
    log.info("[%s] done: %s", worker_id, summary)
    return summary


def _make_bus():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis.asyncio as aioredis

        return RedisLoadBus(aioredis.from_url(url, decode_responses=True))
    raise SystemExit("REDIS_URL is required for a standalone load worker")


async def _main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run_id = os.environ.get("LOAD_RUN_ID")
    if not run_id:
        raise SystemExit("LOAD_RUN_ID is required")
    bus = _make_bus()
    await run_worker(bus, run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
