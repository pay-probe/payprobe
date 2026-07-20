"""Fleet coordinator for network-flow listeners (ADR-0001).

Places each participant instance of a network run onto the flow-host worker
fleet over the shared load bus, waits for every instance's endpoint
advertisement (the **endpoint registry**), and keeps a periodically refreshed
snapshot of the registry so synchronous consumers (`_run_health`,
`/topology-runs`) can read fleet state without awaiting Redis.

Parallel of ``load_coordinator`` for long-lived listeners instead of
fire-and-forget shards. In-process hosting remains the default; the
orchestrator only routes a run here when ``NETWORK_FLEET=1`` and at least one
``worker.flow_host`` is heartbeating.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("payprobe.flow_fleet")

#: How often the registry snapshot is refreshed while fleet runs are live.
SNAPSHOT_REFRESH_S = 2.0

#: Minimum seconds between re-placement attempts of one lost instance (ADR-0001
#: item 6) — a flapping host must not flood the placement queue.
REPLACE_COOLDOWN_S = 10.0


class PlacementError(RuntimeError):
    """A placement failed (host error or timeout); the run was rolled back."""


class FlowFleet:
    def __init__(self, bus: Any) -> None:
        #: ``bus`` may be the bus itself or a zero-arg provider. main.py passes
        #: ``lambda: load_bus`` so a test (or a future live reconfig) that
        #: rebinds the module-level bus is picked up here too.
        self._bus_provider = bus if callable(bus) else (lambda: bus)
        #: run_id → last seen instance advertisements (sync-readable cache).
        self._snapshot: dict[str, list[dict]] = {}
        self._watched: set[str] = set()
        self._poller: asyncio.Task | None = None
        #: run_id → instance_id → placement doc (kept for re-placement when a
        #: host dies and its advertisements expire; ADR-0001 item 6).
        self._placements: dict[str, dict[str, dict]] = {}
        #: instance_id → monotonic time of the last re-placement attempt.
        self._replaced_at: dict[str, float] = {}

    @property
    def bus(self) -> Any:
        return self._bus_provider()

    # -- fleet presence --------------------------------------------------------

    async def hosts(self) -> list[dict]:
        """Live, non-draining flow-host workers."""
        return [w for w in await self.bus.list_workers()
                if w.get("role") == "flow_host" and not w.get("draining")]

    async def available(self) -> bool:
        return bool(await self.hosts())

    # -- placement --------------------------------------------------------------

    async def place(
        self, run_id: str, placements: list[dict], *, timeout_s: float = 20.0,
    ) -> list[dict]:
        """Push placements and wait until every instance advertises its
        endpoint. Any host-reported start error, or a timeout, stops the run
        network-wide (hosts tear down whatever they already started) and raises
        :class:`PlacementError` — partial networks never linger."""
        expected = {str(p["instance_id"]) for p in placements}
        self._placements.setdefault(run_id, {}).update(
            {str(p["instance_id"]): dict(p) for p in placements})
        await self.bus.push_placements(placements)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            rows = await self.bus.list_instances(run_id)
            by_id = {str(r.get("instance_id")): r for r in rows}
            failed = [r for r in by_id.values() if r.get("status") == "error"]
            if failed:
                await self.stop(run_id)
                first = failed[0]
                raise PlacementError(
                    f"flow host could not start instance "
                    f"'{first.get('instance_id')}' ({first.get('flow_id')}): "
                    f"{first.get('error', 'unknown error')}")
            if expected <= set(by_id):
                self._snapshot[run_id] = list(by_id.values())
                self.watch(run_id)
                return [by_id[i] for i in sorted(expected)]
            if asyncio.get_event_loop().time() >= deadline:
                await self.stop(run_id)
                missing = sorted(expected - set(by_id))
                raise PlacementError(
                    f"fleet placement timed out after {timeout_s:.0f}s — "
                    f"{len(missing)}/{len(expected)} instance(s) never came up "
                    f"({', '.join(missing[:5])}). Are enough flow hosts running?")
            await asyncio.sleep(0.25)

    async def stop(self, run_id: str) -> None:
        """Network-wide stop: every host tears down its instances of the run."""
        await self.bus.signal_network_stop(run_id)
        self._watched.discard(run_id)
        self._snapshot.pop(run_id, None)
        for iid in self._placements.pop(run_id, {}):
            self._replaced_at.pop(iid, None)

    # -- registry snapshot (sync reads for health/endpoints) ---------------------

    def watch(self, run_id: str) -> None:
        self._watched.add(run_id)
        if self._poller is None or self._poller.done():
            self._poller = asyncio.get_event_loop().create_task(self._poll_loop())

    def instances_cached(self, run_id: str) -> list[dict]:
        return list(self._snapshot.get(run_id, []))

    def snapshots(self) -> dict[str, list[dict]]:
        """Every watched run's cached advertisements (for endpoint discovery)."""
        return {rid: list(rows) for rid, rows in self._snapshot.items()}

    async def reconcile(self, run_id: str) -> list[str]:
        """Re-place instances whose advertisements expired (host died) —
        ADR-0001 item 6. A lost instance's original placement is pushed back
        onto the queue so any surviving flow host picks it up; a per-instance
        cooldown keeps a flapping host from flooding the queue. Returns the
        instance ids re-placed."""
        placements = self._placements.get(run_id)
        if not placements or await self.bus.is_network_stopped(run_id):
            return []
        advertised = {str(r.get("instance_id"))
                      for r in await self.bus.list_instances(run_id)}
        now = asyncio.get_event_loop().time()
        replaced: list[str] = []
        for iid, placement in placements.items():
            if iid in advertised:
                continue
            if now - self._replaced_at.get(iid, 0.0) < REPLACE_COOLDOWN_S:
                continue
            self._replaced_at[iid] = now
            await self.bus.push_placements([placement])
            replaced.append(iid)
        if replaced:
            log.warning("run '%s': re-placed lost instance(s) %s",
                        run_id, ", ".join(replaced))
        return replaced

    async def _poll_loop(self) -> None:
        try:
            while self._watched:
                for run_id in list(self._watched):
                    try:
                        self._snapshot[run_id] = await self.bus.list_instances(run_id)
                        await self.reconcile(run_id)
                    except Exception as exc:  # noqa: BLE001 — keep polling
                        log.debug("registry poll for %s failed: %s", run_id, exc)
                await asyncio.sleep(SNAPSHOT_REFRESH_S)
        except asyncio.CancelledError:
            raise
