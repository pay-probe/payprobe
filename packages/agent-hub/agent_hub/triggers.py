"""Wake sources other than a human (ADR-0010 phase 4): platform events and
schedules.

* **Events.** The orchestrator POSTs run-lifecycle events (``run.completed``,
  ``run.failed``, ``gate.failed``) to ``/events`` with its service token.
  Every active agent whose spec declares an ``event`` trigger with that name
  gets one heartbeat, ``wake="event"``, with the event as its input. The
  principal is the event itself (``sub = "event:<name>"``, no roles): an
  advisor reads fine under it; a write from an event-woken plan/full agent is
  refused downstream by ordinary RBAC, which is the intended fence until a
  workflow with an approval carries a human's authority.
* **Schedules.** A slow tick (the same 30 s cadence as the engine) wakes agents
  whose ``schedule`` trigger is due: ``interval_sec`` since the last scheduled
  wake, or ``daily_at`` (HH:MM UTC) once the wall clock passes it and no
  scheduled wake happened after it today. Nothing is scheduled while agents
  are paused, so a pause never fills the heartbeat log with refusals.

Both go through the one heartbeat code path (``launch_heartbeat``): limits,
budget, coalescing and alerts apply unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .models import AgentSpec
from .store import RegistryStore

log = logging.getLogger(__name__)

HeartbeatLauncher = Callable[..., Awaitable[dict]]


def agents_for_event(
    specs: list[tuple[str, int, dict]], event: str
) -> list[tuple[str, int, AgentSpec]]:
    """Active agents declaring an ``event`` trigger named ``event``. Pure."""
    out: list[tuple[str, int, AgentSpec]] = []
    for name, version, raw in specs:
        spec = AgentSpec.model_validate(raw)
        if any(t.kind == "event" and t.event == event for t in spec.triggers):
            out.append((name, version, spec))
    return out


def schedule_due(spec: AgentSpec, last: datetime | None, now: datetime) -> bool:
    """Is any ``schedule`` trigger of ``spec`` due, given the last scheduled wake? Pure."""
    for t in spec.triggers:
        if t.kind != "schedule":
            continue
        if t.interval_sec and (last is None or (now - last).total_seconds() >= t.interval_sec):
            return True
        if t.daily_at:
            hh, mm = (int(x) for x in t.daily_at.split(":"))
            due_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= due_at and (last is None or last < due_at):
                return True
    return False


class WakeSources:
    def __init__(self, store: RegistryStore, launch: HeartbeatLauncher) -> None:
        self.store = store
        self.launch = launch
        self._ticker: asyncio.Task | None = None
        self.scheduled = 0
        self.evented = 0

    # -- events --------------------------------------------------------------------------

    async def on_event(self, event: str, payload: dict) -> list[dict]:
        """Wake every agent triggered by ``event``; returns one row per agent."""
        woken: list[dict] = []
        for name, version, spec in agents_for_event(await self.store.active_specs("agent"), event):
            try:
                ver = await self.store.get_version("agent", name, version)
                row = await self.launch(
                    name=name,
                    version=version,
                    spec=spec,
                    spec_sha256=ver["spec_sha256"],
                    caller={"sub": f"event:{event}", "roles": []},
                    input_text=json.dumps(payload, indent=2, default=str),
                    wake="event",
                )
            except Exception as exc:  # noqa: BLE001 - one agent's launch never starves the rest
                log.warning("event %s: wake of %s@%s failed: %s", event, name, version, exc)
                woken.append(
                    {"agent": name, "version": version, "status": "error", "error": str(exc)}
                )
                continue
            self.evented += 1
            woken.append(self._row(name, version, row))
        return woken

    # -- schedules -----------------------------------------------------------------------

    async def tick_schedules(self, now: datetime | None = None) -> list[dict]:
        if (await self.store.paused())["paused"]:
            return []
        now = now or datetime.now(UTC)
        woken: list[dict] = []
        for name, version, raw in await self.store.active_specs("agent"):
            spec = AgentSpec.model_validate(raw)
            if not any(t.kind == "schedule" for t in spec.triggers):
                continue
            last = await self.store.last_wake_at(name, "schedule")
            if not schedule_due(spec, last, now):
                continue
            try:
                ver = await self.store.get_version("agent", name, version)
                row = await self.launch(
                    name=name,
                    version=version,
                    spec=spec,
                    spec_sha256=ver["spec_sha256"],
                    caller={"sub": "scheduler", "roles": []},
                    input_text=json.dumps({"wake": "schedule", "at": now.isoformat()}),
                    wake="schedule",
                )
            except Exception as exc:  # noqa: BLE001 - one agent's launch never starves the rest
                log.warning("schedule: wake of %s@%s failed: %s", name, version, exc)
                woken.append(
                    {"agent": name, "version": version, "status": "error", "error": str(exc)}
                )
                continue
            self.scheduled += 1
            woken.append(self._row(name, version, row))
        return woken

    def start_ticker(self, interval_s: float = 30.0) -> None:
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_forever(interval_s))

    async def _tick_forever(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.tick_schedules()
            except Exception:  # noqa: BLE001 - a tick must never kill the ticker
                log.exception("schedule tick failed")

    def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None

    @staticmethod
    def _row(name: str, version: int, row: dict) -> dict:
        return {
            "agent": name,
            "version": version,
            "heartbeat_id": row.get("id"),
            "status": row.get("status"),
            "coalesced": bool(row.get("coalesced")),
        }

    def stats(self) -> dict[str, Any]:
        return {"evented": self.evented, "scheduled": self.scheduled}


__all__ = ["WakeSources", "agents_for_event", "schedule_due"]
