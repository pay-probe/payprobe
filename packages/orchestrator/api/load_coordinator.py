"""Load-run coordinator.

Shards a :class:`LoadProfile` across ``workers`` separate processes, dispatches
the shards over the :class:`LoadBus`, then tails every worker's metric stream and
merges it into one fleet-wide snapshot — latency histograms add bucket-for-bucket
so the global p95/p99 are exact, not an average of per-worker percentiles. The
merged snapshots are published as ``load.sample`` events on the same run-event
backbone the portal already tails, and the final roll-up is persisted to the run
store so it shows up in history and the trend.

With a Redis bus the workers are external (``python -m worker.load_worker``,
scaled by your orchestrator/k8s). With the in-memory bus the coordinator can run
the workers in-process — the single-node fallback used by dev and tests.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from worker.engine import RunEvent, RUN_STARTED, RUN_COMPLETED
from worker.engine.load import (
    LoadProfile, InMemoryLoadBus, merge_samples, LOAD_SAMPLE, LOAD_COMPLETED, SOAK,
)
from .observability import (
    LOAD_RUNS_INFLIGHT, LOAD_RUN_TPS, LOAD_RUN_ERRORS, LOAD_RUN_WORKERS,
    LOAD_WORKER_RSS,
)

log = logging.getLogger("payprobe.load_coordinator")

# optional callback to run a worker in-process (the single-node fallback)
InProcWorker = Callable[[Any, str], Awaitable[Any]]


def tps_stability(series: list[float]) -> dict[str, Any]:
    """Roll a per-second TPS timeline up into a stability summary.

    ``cv`` is the coefficient of variation (stdev / mean): unit-free, so two
    runs at different target rates compare directly. Lower is steadier — a
    perfectly flat run is 0.0. ``min``/``max`` bound the timeline so a single
    dropout is visible even when the mean looks fine. Leading zero samples (the
    ramp/warm-up before traffic flows) are trimmed so they don't inflate cv."""
    pts = list(series or [])
    # trim only the leading warm-up zeros; an interior dropout is real signal.
    while pts and pts[0] == 0.0:
        pts.pop(0)
    n = len(pts)
    if n == 0:
        return {"mean": 0.0, "stdev": 0.0, "cv": 0.0, "min": 0.0, "max": 0.0, "samples": 0}
    mean = sum(pts) / n
    var = sum((x - mean) ** 2 for x in pts) / n
    stdev = var ** 0.5
    cv = (stdev / mean) if mean else 0.0
    return {
        "mean": round(mean, 1),
        "stdev": round(stdev, 1),
        "cv": round(cv, 3),
        "min": round(min(pts), 1),
        "max": round(max(pts), 1),
        "samples": n,
    }


class LoadRun:
    def __init__(self, run_id: str, profile: LoadProfile) -> None:
        self.run_id = run_id
        self.profile = profile
        self.status = "pending"
        self.started_at = time.time()
        #: when the fleet actually began driving (first sample). The duration
        #: clock and the reported ``elapsed_s`` measure from here, not from run
        #: creation, so worker startup/provisioning latency isn't charged against
        #: the run's send window. Defaults to ``started_at`` until a worker reports.
        self.drive_started_at = self.started_at
        self.latest: dict[str, dict] = {}      # worker_id -> latest cumulative sample
        self.summary: dict | None = None
        #: operator-facing notice (e.g. "start external workers") surfaced in the
        #: run stream + portal without being an error.
        self.notice: str | None = None
        #: set only when the fleet will *never* start driving (in-process cap
        #: refused, or provisioning failed with no fallback) — distinct from a
        #: transient "fleet short" notice where workers are still coming up. The
        #: readiness gate bails on this, but never on a mere notice, so a slow
        #: provision doesn't restart the duration clock prematurely.
        self.startup_failed = False
        #: everything (besides the Shard itself) a shard payload embeds —
        #: env/scenario/mix/weights. Stashed at start so a live re-scale can
        #: mint additional shards for freshly added workers.
        self._shard_ctx: dict[str, Any] | None = None
        #: per-second fleet throughput, one point per emit tick. Recorded live so
        #: the persisted run carries a TPS timeline (not just a final average) —
        #: the basis for the run-vs-run "TPS stability" comparison.
        self.tps_series: list[float] = []
        self._stop = asyncio.Event()
        #: set only by an explicit operator Stop (not by the duration elapsing),
        #: so an in-progress drain can be cut short when the operator wants out.
        self._abort = asyncio.Event()
        #: True while the send window is over but outstanding transactions are
        #: still being drained — surfaced on the snapshot so the portal can show a
        #: "draining" phase (otherwise the drain is invisible).
        self.draining = False
        #: phase boundaries (wall clock), so ``elapsed`` can be split into the
        #: send window and the drain instead of reporting one lumped figure.
        #: ``send_ended_at`` = when the send window closed; ``drain_started_at`` /
        #: ``drain_ended_at`` bracket the drain. 0 ⇒ not reached yet.
        self.send_ended_at = 0.0
        self.drain_started_at = 0.0
        self.drain_ended_at = 0.0
        self._tasks: list[asyncio.Task] = []

    def _elapsed_breakdown(self) -> dict[str, float]:
        """Split wall-clock into the send window and the drain.

        ``send_elapsed_s`` = driving time (fleet-ready → send window closed, or
        → now if still sending). ``drain_elapsed_s`` = drain start → drain end (or
        → now while draining). ``elapsed_s`` = their sum (excludes startup +
        finalize grace). Coordinator-owned so the split is consistent live and at
        finalize, overriding the workers' lumped elapsed."""
        now = time.time()
        send_end = self.send_ended_at or now
        send = max(0.0, send_end - self.drive_started_at)
        if self.drain_started_at:
            drain_end = self.drain_ended_at or now
            drain = max(0.0, drain_end - self.drain_started_at)
        else:
            drain = 0.0
        return {
            "send_elapsed_s": round(send, 1),
            "drain_elapsed_s": round(drain, 1),
            "elapsed_s": round(send + drain, 1),
        }

    def snapshot(self) -> dict[str, Any]:
        stats = merge_samples(list(self.latest.values()))
        s = stats.summary()
        s["status"] = self.status
        s["workers_reporting"] = len(self.latest)
        s["workers"] = self.profile.workers
        s["profile"] = self.profile.type
        if self.notice:
            s["notice"] = self.notice
        s["draining"] = self.draining
        # coordinator-owned elapsed split (send window vs drain), replacing the
        # workers' single lumped ``elapsed_s``.
        s.update(self._elapsed_breakdown())
        if self.draining:
            # the configured budget, so the portal can show "draining 4s / 30s".
            s["drain_s"] = self.profile.drain_s
        return s


class LoadCoordinator:
    def __init__(
        self,
        bus,
        backbone,
        run_store=None,
        *,
        emit_interval_s: float = 1.0,
        grace_s: float = 5.0,
        worker_grace_s: float = 4.0,
        drain_ceiling_s: float = 600.0,
        startup_timeout_s: float = 120.0,
        in_proc_worker: InProcWorker | None = None,
        run_control=None,
        provisioner=None,
        inproc_max_tps: float = 2000.0,
        inproc_max_connections: int = 5000,
    ) -> None:
        self.bus = bus
        self.backbone = backbone
        self.run_store = run_store
        self.emit_interval = emit_interval_s
        self.grace = grace_s
        self.worker_grace_s = worker_grace_s
        #: hard ceiling (s) on how long a duration-elapsed run waits for its
        #: workers to drain in-flight transactions when ``drain_s < 0``
        #: (wait-for-all). Bounds an unbounded drain against a wedged target so a
        #: run can't hang forever; positive ``drain_s`` bounds itself.
        self.drain_ceiling_s = drain_ceiling_s
        #: max time to wait for the fleet to *start driving* before the duration
        #: clock begins. Provisioning a worker container + warming up its adapters
        #: can take several seconds; counting that against a short duration would
        #: eat the run's real send time (a 10s run that only drove 3s). Bounded so
        #: a fleet that never starts still finalizes.
        self.startup_timeout_s = startup_timeout_s
        self.in_proc_worker = in_proc_worker
        #: optional worker provisioner (see api/worker_provisioner.py). When set
        #: and the bus is distributed, a launched run brings its own fleet up and
        #: tears it down on finalize; the portal can also scale it live. None ⇒
        #: the custom path only (operator starts `load_worker` themselves).
        self.provisioner = provisioner
        #: Safety ceiling for the in-process fallback. The fallback runs workers
        #: inside the orchestrator event loop; above this it would contend with
        #: the API/WebSocket loop, so we refuse and tell the operator to start
        #: external `load_worker` processes instead.
        self.inproc_max_tps = inproc_max_tps
        self.inproc_max_connections = inproc_max_connections
        #: Cross-replica registry so any orchestrator can stop a load run it
        #: doesn't own (see api/run_control.py). Optional — None ⇒ single-node.
        self.run_control = run_control
        self.runs: dict[str, LoadRun] = {}

    # -- lifecycle -----------------------------------------------------------
    async def start(self, run_id: str, profile: LoadProfile, env: dict,
                    scenario: dict, *, heartbeat_action: str | None = None,
                    scenarios: list[dict] | None = None,
                    weights: list[float] | None = None) -> LoadRun:
        profile.validate()
        run = LoadRun(run_id, profile)
        self.runs[run_id] = run
        LOAD_RUNS_INFLIGHT.inc()
        # make the run stoppable from any replica (routed back to us)
        if self.run_control is not None:
            await self.run_control.register(run_id, "load")

        # weighted traffic mix: ``scenarios`` + ``weights`` drive a per-transaction
        # blend (e.g. 80% payment / 15% refund / 5% reversal). ``scenario`` stays
        # the primary (back-compat + soak heartbeat source).
        mix = scenarios or [scenario]
        run._shard_ctx = {
            "env": env, "scenario": scenario, "scenarios": mix,
            "weights": weights or [1.0] * len(mix),
            "heartbeat_action": heartbeat_action,
        }
        shards = profile.shards(run_id)
        payloads = [{**sh.to_dict(), **run._shard_ctx} for sh in shards]
        await self.bus.push_shards(run_id, payloads)

        await self._emit(run, RUN_STARTED, {
            "kind": "load", "profile": profile.to_dict(), "workers": profile.workers,
        })
        run.status = "running"

        if isinstance(self.bus, InMemoryLoadBus):
            # single process: run the workers in-process right away
            if self.in_proc_worker is not None:
                self._spawn_inproc_workers(run)
        else:
            # Redis fleet: external `python -m worker.load_worker` processes claim
            # the shards. If a provisioner is configured, bring the fleet up now
            # (dynamic path) — this must NOT depend on the in-process fallback
            # being enabled, since the fully distributed setup
            # (PAYPROBE_LOAD_EXTERNAL_WORKERS=1 ⇒ in_proc_worker=None) is exactly
            # where auto-provisioning matters most. Otherwise the operator starts
            # workers themselves (custom path).
            if self.provisioner is not None and getattr(self.provisioner, "available", False):
                run._tasks.append(asyncio.create_task(self._provision(run)))
            # Safety net: if nothing claims the shards within a grace window and an
            # in-process worker is available, drive the load in-process so a run
            # never silently produces 0 TPS.
            if self.in_proc_worker is not None:
                run._tasks.append(asyncio.create_task(self._external_or_fallback(run)))

        run._tasks.append(asyncio.create_task(self._aggregate(run)))
        run._tasks.append(asyncio.create_task(self._lifecycle(run)))
        run._tasks.append(asyncio.create_task(self._watch_external_stop(run)))
        return run

    def _spawn_inproc_workers(self, run: LoadRun) -> None:
        for _ in range(run.profile.workers):
            run._tasks.append(
                asyncio.create_task(self.in_proc_worker(self.bus, run.run_id))
            )

    async def _external_or_fallback(self, run: LoadRun) -> None:
        """Wait briefly for an external fleet to claim shards; if the shard queue
        is still full and nothing has reported, drive the load in-process."""
        await asyncio.sleep(self.worker_grace_s)
        if run._stop.is_set() or run.status != "running":
            return
        try:
            pending = await self.bus.pending_shards(run.run_id)
        except Exception:  # noqa: BLE001
            pending = 0
        claimed_any = pending < run.profile.workers
        if claimed_any or run.latest:
            return  # an external fleet is handling it

        # The fallback runs workers in the orchestrator's own event loop. That's
        # fine for modest load, but driving 20K TPS / 100K conns in-process would
        # starve the API/WebSocket loop — so refuse above a safe cap and tell the
        # operator to bring up real workers.
        kind, demand, cap = self._inproc_demand(run.profile)
        if demand > cap:
            # Emit a command that actually works: a standalone worker REQUIRES
            # REDIS_URL (it exits immediately without it), so reuse the
            # provisioner's manual_command which fills REDIS_URL from the
            # orchestrator's own env. Also point at the one-click Load-page scale.
            from .worker_provisioner import manual_command

            cmd = manual_command(run.run_id, None)
            run.notice = (
                f"No external workers joined and the requested {kind} ({demand:g}) "
                f"exceeds the in-process safe cap ({cap:g}). Scale the fleet from "
                f"the Load page, or start a worker yourself — `{cmd}` — or lower "
                f"the load. Running this in-process would risk starving the "
                f"orchestrator."
            )
            log.warning("load run %s: %s", run.run_id, run.notice)
            run.startup_failed = True  # no worker will drive — don't wait on readiness
            try:
                await self._emit(run, LOAD_SAMPLE, run.snapshot())
            except Exception:  # noqa: BLE001
                pass
            return

        log.warning(
            "load run %s: no external workers claimed shards within %.0fs — "
            "falling back to %d in-process worker(s)",
            run.run_id, self.worker_grace_s, run.profile.workers,
        )
        self._spawn_inproc_workers(run)

    def _inproc_demand(self, p: LoadProfile) -> tuple[str, float, float]:
        """(kind, fleet demand, cap) for the in-process fallback decision."""
        if p.type == SOAK:
            return "connections", float(p.connections), float(self.inproc_max_connections)
        peak = max(p.target_tps, p.start_tps, p.end_tps, p.base_tps, p.spike_tps)
        return "TPS", float(peak), float(self.inproc_max_tps)

    async def _provision(self, run: LoadRun) -> None:
        """Dynamic path: ask the provisioner to bring up the run's fleet."""
        try:
            res = await self.provisioner.scale(run.run_id, run.profile.workers)
            log.info("load run %s: provisioned fleet %s", run.run_id, res)
            # A short fleet was previously only visible in this log line, so the
            # portal showed "1/4 workers" with no reason. Surface the
            # provisioner's own diagnosis (docker error / crashed-worker logs)
            # as the run notice the Load page already renders.
            running = int(res.get("running", 0) or 0)
            notice = str(res.get("notice") or "")
            if notice or running < run.profile.workers:
                run.notice = (
                    f"Fleet short: {running}/{run.profile.workers} worker(s) "
                    f"running{' — ' + notice if notice else ''}"
                )
                try:
                    await self._emit(run, LOAD_SAMPLE, run.snapshot())
                except Exception:  # noqa: BLE001 — notice still lands in snapshot
                    pass
        except Exception as exc:  # noqa: BLE001 — fall back to manual/in-process
            log.warning("load run %s: provisioning failed: %s", run.run_id, exc)
            run.notice = f"Provisioning failed: {type(exc).__name__}: {exc}"

    async def scale(self, run_id: str, desired: int) -> dict:
        """Scale a live run's worker fleet to ``desired`` (dynamic path).

        Reshards the run so the new workers have shards to claim, then drives the
        provisioner. Returns the provisioner status (or a notice when no
        provisioner is configured — the operator scales manually then)."""
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        desired = max(1, int(desired))
        if self.provisioner is None or not getattr(self.provisioner, "available", False):
            from .worker_provisioner import manual_command
            return {
                "kind": "none", "available": False, "requested": desired,
                "running": run.snapshot().get("workers_reporting", 0),
                "notice": "No worker provisioner configured. Scale manually: "
                          f"{manual_command(run_id, None)}",
            }
        # Re-publish shards for the new fleet size so freshly added workers have
        # a shard to claim (push_shards is additive on the queue; workers that
        # already hold a shard keep theirs). This is the step that was long
        # promised but missing: without it, scaled-up workers found an empty
        # queue, idled out and exited — "the fleet is 4 but the run uses 2".
        old = max(1, int(run.profile.workers))
        run.profile.workers = desired
        if desired > old and run._shard_ctx is not None:
            extra = []
            for i in range(old, desired):
                sh = run.profile.shard(i)
                sh.run_id = run_id
                extra.append({**sh.to_dict(), **run._shard_ctx})
            await self.bus.push_shards(run_id, extra)
        if desired != old:
            # Make the whole fleet re-slice to the new size: existing workers
            # observe ``workers`` via the retune key and recompute their share
            # of the same fleet totals, so old + new shards sum back to the
            # target instead of overdriving it.
            retune: dict[str, Any] = {"workers": desired}
            for knob in ("target_tps", "base_tps", "spike_tps", "end_tps"):
                v = float(getattr(run.profile, knob, 0.0) or 0.0)
                if v > 0:
                    retune[knob] = v
            await self.bus.signal_retune(run_id, retune)
        try:
            res = await self.provisioner.scale(run_id, desired)
        except Exception as exc:  # noqa: BLE001
            log.warning("load run %s: scale failed: %s", run_id, exc)
            return {"kind": getattr(self.provisioner, "kind", "?"),
                    "available": True, "requested": desired,
                    "running": await self._provisioned_count(run_id),
                    "notice": str(exc)}
        return res

    async def _provisioned_count(self, run_id: str) -> int:
        try:
            return await self.provisioner.count(run_id)
        except Exception:  # noqa: BLE001
            return 0

    async def _teardown_fleet(self, run_id: str) -> None:
        """Reap any provisioner-managed workers for a run that's stopping."""
        if self.provisioner is None or not getattr(self.provisioner, "available", False):
            return
        try:
            await self.provisioner.teardown(run_id)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            log.debug("load run %s: fleet teardown failed: %s", run_id, exc)

    async def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run:
            run._abort.set()  # operator wants out — cut any in-progress drain short
            run._stop.set()
            await self.bus.signal_stop(run_id)
            await self._teardown_fleet(run_id)
            return True
        # Not owned by this replica. If the run is live in the shared store, push
        # a stop over the bus — the owning replica's watcher and the worker fleet
        # both observe it, so any replica can stop any load run.
        if self.run_store is not None:
            rec = self.run_store.get(run_id)
            if rec is not None and rec.get("status") == "running":
                await self.bus.signal_stop(run_id)
                return True
        return False

    async def retune(self, run_id: str, params: dict) -> bool:
        """Adjust the live target TPS of a running load run.

        Pushes the new fleet-wide targets over the bus; every worker observes
        the key and recomputes its own slice, and the driver's pacing picks up
        the new ``target_tps_at`` within a tick — no restart. Works for runs
        owned by this replica *and* runs on another replica (the bus key is
        shared), as long as the run is still live in the store."""
        owned = self.runs.get(run_id)
        if owned is None and self.run_store is not None:
            rec = self.run_store.get(run_id)
            if rec is None or rec.get("status") != "running":
                return False
        elif owned is None:
            return False
        clean = {k: v for k, v in params.items()
                 if k in ("target_tps", "base_tps", "spike_tps", "end_tps") and v is not None}
        if not clean:
            return False
        await self.bus.signal_retune(run_id, clean)
        # reflect the new target on the in-memory profile so the snapshot/history
        # reports what's actually being driven.
        if owned is not None:
            for k, v in clean.items():
                setattr(owned.profile, k, float(v))
        return True

    def get(self, run_id: str) -> LoadRun | None:
        return self.runs.get(run_id)

    # -- internals -----------------------------------------------------------
    async def _watch_external_stop(self, run: LoadRun) -> None:
        """Finalize when a stop is signalled from another replica.

        ``stop()`` on a replica that doesn't own the run writes the bus stop key
        instead of setting the local event. The owning replica polls that key so
        it drains and rolls up the run, rather than waiting out the full
        duration. No-op cost where stop comes in locally (the event is already
        set)."""
        is_stopped = getattr(self.bus, "is_stopped", None)
        if is_stopped is None:
            return
        while not run._stop.is_set():
            try:
                if await is_stopped(run.run_id):
                    run._stop.set()
                    return
            except Exception as exc:  # noqa: BLE001 — bus hiccup; keep polling
                log.debug("load run %s: stop-key poll failed: %s", run.run_id, exc)
            await asyncio.sleep(1.0)

    async def _await_ready(self, run: LoadRun) -> None:
        """Wait for the fleet to start driving before the duration clock begins.

        A load worker has to be provisioned (a container spin-up on the dynamic
        path) and warm up its adapters before it drives a single transaction —
        several seconds that must NOT be charged against a short duration, or a
        10s run only actually sends for 3s. We wait for the first sample (a worker
        is driving) and stamp ``drive_started_at``, bounded by
        ``startup_timeout_s`` and short-circuited if the run is stopped or the
        fleet gave up (a notice was raised)."""
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline and not run._stop.is_set():
            if run.latest:  # a worker has reported ⇒ the fleet is driving
                run.drive_started_at = time.time()
                return
            if run.startup_failed:  # no worker will ever start — nothing to time
                return
            await asyncio.sleep(0.2)

    async def _lifecycle(self, run: LoadRun) -> None:
        """Hold for the profile duration, then stop, drain, and finalize."""
        # Start the duration clock only once the fleet is actually driving, so
        # provisioning + warmup latency doesn't shorten the run's send window.
        await self._await_ready(run)
        duration = run.profile.duration_s
        natural_end = False
        if duration and duration > 0:
            try:
                await asyncio.wait_for(run._stop.wait(), timeout=duration)
            except asyncio.TimeoutError:
                natural_end = True  # duration elapsed rather than an operator Stop
        else:  # soak with no duration → run until explicitly stopped
            await run._stop.wait()

        run._stop.set()
        run.send_ended_at = time.time()  # the send window closes here
        # Signal the fleet to stop *sending new* traffic. Workers then drain their
        # outstanding transactions per ``drain_s`` so everything launched resolves
        # to a response or a timeout instead of being dropped.
        await self.bus.signal_stop(run.run_id)
        if natural_end and run.profile.drain_s != 0:
            # Enter the visible drain phase: flag it and emit a snapshot right away
            # so the portal flips to "draining" without waiting for the next tick.
            run.draining = True
            run.drain_started_at = time.time()
            try:
                await self._emit(run, LOAD_SAMPLE, run.snapshot())
            except Exception:  # noqa: BLE001 — the emit loop will catch up regardless
                pass
            await self._await_drain(run)
            run.drain_ended_at = time.time()
            run.draining = False
        # give workers a moment to flush their terminal samples
        await asyncio.sleep(self.grace)
        await self.bus.mark_done(run.run_id)

    async def _await_drain(self, run: LoadRun) -> None:
        """Wait for the fleet to drain outstanding transactions before finalizing.

        Without this the coordinator marked the run done ``grace`` seconds after
        the duration elapsed — long before a saturated fleet finished draining —
        so the operator's ``drain_s`` never really applied.

        ``drain_s`` is a *maximum*, not a fixed wait: we return as soon as there
        is nothing left to drain. Two independent "done" signals, whichever fires
        first — so a slow start on either doesn't hold the run open:
          * the merged snapshot shows nothing outstanding (``pending`` +
            ``in_flight`` → 0), or
          * every worker that was driving has finished and dropped from the fleet
            presence (covers a worker that stops emitting samples mid-drain).
        Bounded by ``drain_s`` (``< 0`` ⇒ wait for all, capped at
        ``drain_ceiling_s`` so a wedged target can't hang the run forever)."""
        drain = float(getattr(run.profile, "drain_s", 0.0) or 0.0)
        if drain == 0:
            return
        bound = self.drain_ceiling_s if drain < 0 else drain
        deadline = time.monotonic() + bound
        list_workers = getattr(self.bus, "list_workers", None)
        seen_workers = False
        while time.monotonic() < deadline and not run._abort.is_set():
            snap = run.snapshot()
            # nothing ever joined (e.g. the fallback was refused, or no worker
            # claimed a shard) ⇒ there is nothing to drain.
            if not snap.get("workers_reporting", 0):
                return
            outstanding = int(snap.get("pending", 0) or 0) + int(snap.get("in_flight", 0) or 0)
            if outstanding <= 0:
                return
            # Presence signal: once we've seen this run's workers in the fleet,
            # their disappearance means they finished driving + draining and
            # dropped — end the drain even if their last sample was stale.
            if list_workers is not None:
                try:
                    live = [w for w in await list_workers() if w.get("run_id") == run.run_id]
                except Exception:  # noqa: BLE001 — presence read is best-effort
                    live = None
                if live:
                    seen_workers = True
                elif live is not None and seen_workers:
                    return
            await asyncio.sleep(0.5)

    async def _aggregate(self, run: LoadRun) -> None:
        emitter = asyncio.create_task(self._emit_loop(run))
        try:
            async for sample in self.bus.samples(run.run_id):
                wid = sample.get("worker_id") or f"w{sample.get('worker_index', 0)}"
                run.latest[wid] = sample
        finally:
            emitter.cancel()
        # final roll-up
        run.status = "completed"
        run.summary = run.snapshot()
        run.summary["status"] = "completed"
        # snapshot() already split elapsed into send_elapsed_s + drain_elapsed_s
        # (measured from fleet-ready, so startup latency isn't counted). Report
        # achieved throughput over the SEND window — the drain only finishes
        # already-sent work, so dividing by the total would understate the rate.
        send_elapsed = run.summary.get("send_elapsed_s", 0) or run.summary.get("elapsed_s", 0)
        if send_elapsed > 0:
            run.summary["tps"] = round(run.summary.get("received", 0) / send_elapsed, 1)
        # persist the throughput timeline + a stability roll-up (coefficient of
        # variation): how steady the achieved TPS was over the run. Low cv ⇒
        # flat/healthy; a high cv flags a run that sawtoothed or fell off.
        run.summary["tps_series"] = list(run.tps_series)
        run.summary["tps_stability"] = tps_stability(run.tps_series)
        await self._emit(run, LOAD_COMPLETED, run.summary)
        # terminal event so the existing run-stream/WebSocket machinery closes
        await self._emit(run, RUN_COMPLETED, {"status": "completed", "kind": "load"})
        # reap any provisioner-managed fleet now the run is done (covers the
        # duration-elapsed and external-stop paths, not just explicit stop()).
        await self._teardown_fleet(run.run_id)
        # drop the per-run gauges and the inflight counter
        LOAD_RUNS_INFLIGHT.dec()
        for g in (LOAD_RUN_TPS, LOAD_RUN_ERRORS, LOAD_RUN_WORKERS):
            try:
                g.remove(run_id=run.run_id)
            except Exception:  # noqa: BLE001
                pass
        # drop the per-worker RSS series for this run so finished runs don't
        # linger in the metric output (the gauge is labelled per worker_id).
        for wid in list(run.latest):
            try:
                LOAD_WORKER_RSS.remove(run_id=run.run_id, worker_id=wid)
            except Exception:  # noqa: BLE001
                pass
        if self.run_store is not None:
            try:
                self.run_store.finish(run.run_id, "completed", run.summary)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist load run %s", run.run_id)
        # no longer in-flight — drop from the cross-replica registry
        if self.run_control is not None:
            try:
                await self.run_control.unregister(run.run_id)
            except Exception:  # noqa: BLE001
                log.debug("run_control unregister failed for %s", run.run_id)

    async def _emit_loop(self, run: LoadRun) -> None:
        warned = False
        while True:
            await asyncio.sleep(self.emit_interval)
            try:
                snap = run.snapshot()
                # capture the per-second fleet throughput for the persisted TPS
                # timeline (stability is computed off this at finalize). Only
                # while the run is actively driving — drain ticks tail to ~0 and
                # would otherwise depress the stability figure.
                if run.status == "running":
                    run.tps_series.append(round(float(snap.get("tps", 0.0) or 0.0), 1))
                # surface live load health to Prometheus (per active run)
                LOAD_RUN_TPS.set(snap.get("tps", 0) or 0, run_id=run.run_id)
                LOAD_RUN_ERRORS.set(snap.get("errors", 0) or 0, run_id=run.run_id)
                LOAD_RUN_WORKERS.set(
                    snap.get("workers_reporting", 0) or 0, run_id=run.run_id
                )
                # per-worker resident memory, for the soak/leak trend. Workers
                # that have stopped reporting keep their last value until the run
                # finishes (cleaned up in _aggregate), so a worker that died mid
                # soak is still visible.
                for wid, ws in run.latest.items():
                    rss = ws.get("rss_bytes")
                    if rss:
                        LOAD_WORKER_RSS.set(rss, run_id=run.run_id, worker_id=wid)
                await self._emit(run, LOAD_SAMPLE, snap)
                # if a run is erroring on every transaction, say why in the log
                # once — so the cause is visible without a portal rebuild.
                if not warned and snap.get("errors", 0) > 0 and snap.get("received", 0) == 0:
                    reason = snap.get("last_error") or "unknown (no error detail)"
                    log.warning(
                        "load run %s: every transaction is failing (%d errors, 0 ok). "
                        "Reason: %s", run.run_id, snap["errors"], reason,
                    )
                    warned = True
            except Exception as exc:  # noqa: BLE001 — emit must not kill the loop
                # A persistent failure here drops live samples (the portal/Grafana
                # go blank); log it so the gap is explained, not silent.
                log.warning("load run %s: sample emit failed: %s", run.run_id, exc)

    async def _emit(self, run: LoadRun, type_: str, data: dict) -> None:
        evt = RunEvent(type=type_, run_id=run.run_id, data=data)
        await self.backbone.append(run.run_id, evt.to_dict())
