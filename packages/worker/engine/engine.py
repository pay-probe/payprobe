"""WorkerEngine — the three-phase test execution core.

Phase model (spec §6.3 / §9):
  Phase 1  Components   health_check() on every adapter in the environment.
  Phase 2  Integration  run ``component`` + ``integration`` scenarios.
  Phase 3  End-to-End   run ``e2e`` scenarios, diffed against baseline downstream.

Blast-radius gating: a scenario is BLOCKED (not failed) when it depends on a
target that failed an earlier gate. Phase 1 failures block any scenario touching
the unhealthy target; phase 2 failures block e2e scenarios touching a target
whose integration scenario failed.

Concurrency: two semaphores cap the envelope — total outbound connections and
simultaneously executing steps — matching the spec's 50K/10K ceilings, sized
per-run from the environment's connection budget.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..adapters.base.base_adapter import StepResult
from ..adapters.registry import AdapterRegistry
from .events import (
    RunEvent,
    EventSink,
    NullSink,
    FanoutSink,
    QueueSink,
    RUN_STARTED,
    PHASE_UPDATE,
    SCENARIO_RESULT,
    RUN_COMPLETED,
)
from .runner import (
    ScenarioRunner,
    ScenarioResult,
    scenario_targets,
    PASSED,
    FAILED,
    BLOCKED,
)

log = logging.getLogger(__name__)

# scenario test_class -> phase bucket
PHASE_2_CLASSES = {"component", "integration"}
PHASE_3_CLASSES = {"e2e"}


class WorkerEngine:
    def __init__(
        self,
        env_config: dict,
        sink: EventSink | None = None,
        *,
        max_connections: int | None = None,
        max_ops: int | None = None,
    ) -> None:
        self.env_config = env_config
        self.sink = sink or NullSink()
        self.registry = AdapterRegistry(env_config)

        budget = env_config.get("connection_budget", {})
        conn = max_connections or budget.get("total", 50_000)
        ops = max_ops or min(conn, 10_000)
        self.connection_semaphore = asyncio.Semaphore(conn)
        self.ops_limiter = asyncio.Semaphore(ops)
        #: pause before the one phase-1 retry of unhealthy components — long
        #: enough for a relay's stale-upstream reconnect, short enough not to
        #: drag every genuinely-dead-target run (tests set it to 0).
        self.phase1_retry_delay_sec: float = float(env_config.get("phase1_retry_delay_sec", 1.0))

    # -- lifecycle -----------------------------------------------------------

    async def warmup_adapters(self) -> None:
        await self.registry.warmup()

    async def teardown(self) -> None:
        await self.registry.disconnect_all()

    # -- streaming API -------------------------------------------------------

    async def iter_run(self, scenarios: list[dict], run_id: str):
        """Run a batch and yield each event live as it happens.

        ``async for event in engine.iter_run(scenarios, run_id): ...``

        The base sink (Redis stream, etc.) still receives every event; this
        adds a private in-memory queue fanned out alongside it. The loop ends
        after the terminal ``run.completed`` event.
        """
        queue = QueueSink()
        base = self.sink
        self.sink = FanoutSink([base, queue])
        task = asyncio.create_task(self.run_scenario_batch(scenarios, run_id))
        try:
            async for event in queue.drain():
                yield event
            await task
        finally:
            self.sink = base
            if not task.done():
                task.cancel()

    # -- step execution (owns the concurrency envelope) ----------------------

    async def _execute_step(
        self,
        target: str,
        action: str,
        payload: dict,
        assertions: list,
    ) -> StepResult:
        async with self.connection_semaphore:
            async with self.ops_limiter:
                adapter = await self.registry.get(target)
                start = time.monotonic()
                sr = await adapter.execute(action, payload)
                if not sr.duration_ms:
                    sr.duration_ms = int((time.monotonic() - start) * 1000)
                if assertions:
                    # the step's measured timing is an assertable metric
                    # (spec §10 tracks duration_ms per step); expose it
                    # alongside the response without clobbering a real field.
                    target_obj = dict(sr.response_payload or {})
                    target_obj.setdefault("duration_ms", sr.duration_ms)
                    sr.assertions = adapter.assert_response(target_obj, assertions)
                return sr

    # -- the run -------------------------------------------------------------

    async def run_scenario_batch(
        self,
        scenarios: list[dict],
        run_id: str,
        debug_hook=None,
    ) -> dict:
        """Execute a batch of scenarios through the three-phase pipeline.

        ``debug_hook`` (if given) is an async ``(node_id, context)`` callback the
        runner awaits before each node — the orchestrator uses it for
        breakpoints / step-through.

        Returns a run summary dict suitable for persistence / reporting.
        """
        secrets = sorted({s for sc in scenarios for s in (sc.get("__secrets__") or [])})
        runner = ScenarioRunner(self.sink, run_id, debug_hook, secrets)
        await self.sink.publish(
            RunEvent(
                RUN_STARTED,
                run_id,
                {
                    "scenario_count": len(scenarios),
                },
            )
        )

        phase2 = [s for s in scenarios if s.get("test_class", "e2e") in PHASE_2_CLASSES]
        phase3 = [s for s in scenarios if s.get("test_class", "e2e") in PHASE_3_CLASSES]

        summary: dict = {"run_id": run_id, "phases": {}, "scenarios": []}

        # ---- Phase 1: component health checks ----
        await self.warmup_adapters()
        health = await self.registry.health_check_all()
        failed_targets = {t for t, ok in health.items() if not ok}
        recovered: list[str] = []
        if failed_targets:
            # Second chance before blocking the run: a relay/proxy with a
            # stale upstream heals itself on the first attempt (its reconnect
            # can outlive the probe's timeout), so one re-probe after a beat
            # turns a transient warmup fault into a passing phase 1.
            await asyncio.sleep(self.phase1_retry_delay_sec)
            retried = await self.registry.retry_unhealthy(failed_targets)
            recovered = sorted(t for t, ok in retried.items() if ok)
            failed_targets -= set(recovered)
        phase1_status = PASSED if not failed_targets else FAILED
        summary["phases"]["phase_1"] = {
            "status": phase1_status,
            "components": len(health),
            "failed": sorted(failed_targets),
        }
        if recovered:
            # surfaced in the report so a flaky component is visible even
            # when the retry saved the run
            summary["phases"]["phase_1"]["recovered"] = recovered
        await self._emit_phase(run_id, 1, phase1_status, summary["phases"]["phase_1"])

        # ---- Phase 2: integration / component scenarios ----
        p2_results, p2_failed_targets = await self._run_phase(
            runner,
            phase2,
            phase=2,
            blocked_targets=failed_targets,
        )
        summary["phases"]["phase_2"] = self._phase_summary(p2_results)
        await self._emit_phase(
            run_id, 2, summary["phases"]["phase_2"]["status"], summary["phases"]["phase_2"]
        )

        # ---- Phase 3: end-to-end scenarios ----
        p3_blocked = failed_targets | p2_failed_targets
        p3_results, _ = await self._run_phase(
            runner,
            phase3,
            phase=3,
            blocked_targets=p3_blocked,
        )
        summary["phases"]["phase_3"] = self._phase_summary(p3_results)
        await self._emit_phase(
            run_id, 3, summary["phases"]["phase_3"]["status"], summary["phases"]["phase_3"]
        )

        for r in (*p2_results, *p3_results):
            summary["scenarios"].append(
                {
                    "scenario_id": r.scenario_id,
                    "name": r.name,
                    "status": r.status,
                    "steps": [o.__dict__ for o in r.steps],
                }
            )

        statuses = {s["status"] for s in summary["scenarios"]}
        summary["status"] = (
            FAILED
            if FAILED in statuses or phase1_status == FAILED
            else (PASSED if statuses <= {PASSED, BLOCKED} else FAILED)
        )
        await self.sink.publish(
            RunEvent(
                RUN_COMPLETED,
                run_id,
                {
                    "status": summary["status"],
                    "phases": summary["phases"],
                },
            )
        )
        await self.teardown()
        return summary

    # -- helpers -------------------------------------------------------------

    async def _run_phase(
        self,
        runner: ScenarioRunner,
        scenarios: list[dict],
        *,
        phase: int,
        blocked_targets: set[str],
    ) -> tuple[list[ScenarioResult], set[str]]:
        results: list[ScenarioResult] = []
        newly_failed_targets: set[str] = set()
        runnable: list[dict] = []

        for sc in scenarios:
            if scenario_targets(sc) & blocked_targets:
                blocked = ScenarioResult(
                    scenario_id=sc.get("id", sc.get("name", "?")),
                    name=sc.get("name", "?"),
                    status=BLOCKED,
                )
                results.append(blocked)
                await self.sink.publish(
                    RunEvent(
                        SCENARIO_RESULT,
                        runner.run_id,
                        {
                            "scenario": blocked.scenario_id,
                            "phase": phase,
                            "status": BLOCKED,
                        },
                    )
                )
            else:
                runnable.append(sc)

        # run the unblocked scenarios concurrently — the semaphores cap actual
        # parallelism, so this scales without unbounded task explosion.
        coros = [runner.run(sc, self._execute_step, phase) for sc in runnable]
        for r in await asyncio.gather(*coros):
            results.append(r)
            if r.status == FAILED:
                newly_failed_targets |= {s.target for s in r.steps if s.status != PASSED}
            await self.sink.publish(
                RunEvent(
                    SCENARIO_RESULT,
                    runner.run_id,
                    {
                        "scenario": r.scenario_id,
                        "phase": phase,
                        "status": r.status,
                    },
                )
            )
        return results, newly_failed_targets

    @staticmethod
    def _phase_summary(results: list[ScenarioResult]) -> dict:
        total = len(results)
        passed = sum(r.status == PASSED for r in results)
        failed = sum(r.status == FAILED for r in results)
        blocked = sum(r.status == BLOCKED for r in results)
        if failed:
            status = FAILED
        elif total and passed == total:
            status = PASSED
        elif total == 0:
            status = PASSED
        else:
            status = "partial"
        return {
            "status": status,
            "total": total,
            "passed": passed,
            "failed": failed,
            "blocked": blocked,
        }

    async def _emit_phase(self, run_id: str, phase: int, status: str, data: dict) -> None:
        await self.sink.publish(
            RunEvent(
                PHASE_UPDATE,
                run_id,
                {
                    "phase": phase,
                    "status": status,
                    **data,
                },
            )
        )
