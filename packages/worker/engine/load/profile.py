"""Load profile definition and per-worker sharding.

A :class:`LoadProfile` is the whole test ("drive ``payment`` at 20 000 TPS for
10 minutes across 10 workers"). :meth:`LoadProfile.shard` slices it into one
:class:`Shard` per worker — dividing TPS / connections evenly with the
remainder spread across the low-index workers so the fleet sums back to the
requested target exactly.

The shape of the load over time is a pure function of elapsed seconds,
:meth:`Shard.target_tps_at`, so steady / ramp / spike all fall out of the same
pacing loop in the driver with no separate controller threads.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

# -- profile types -----------------------------------------------------------
STEADY = "steady"  # hold a constant target TPS
SOAK = "soak"  # hold N long-lived connections sending heartbeats
SPIKE = "spike"  # steady base TPS with periodic bursts
RAMP = "ramp"  # linearly increase TPS over a window

_TPS_TYPES = {STEADY, SPIKE, RAMP}

# -- event types emitted on the run stream for the portal --------------------
LOAD_SAMPLE = "load.sample"  # a rolled-up snapshot (every ~1s)
LOAD_COMPLETED = "load.completed"  # terminal, carries the final summary


def _split(total: float, parts: int, index: int) -> float:
    """This worker's slice of ``total`` split ``parts`` ways (remainder to low
    indices so the slices sum back to ``total``)."""
    if parts <= 0:
        return 0.0
    base = total / parts
    # spread the rounding remainder for integer-ish quantities deterministically
    whole = int(total) // parts
    rem = int(total) % parts
    if float(total).is_integer():
        return float(whole + (1 if index < rem else 0))
    return base


@dataclass
class Shard:
    """One worker's portion of a load run."""

    run_id: str
    worker_index: int
    worker_count: int
    type: str
    duration_s: float

    # TPS-shaped profiles (steady / ramp / spike) — values already per-worker
    target_tps: float = 0.0  # steady
    start_tps: float = 0.0  # ramp
    end_tps: float = 0.0  # ramp
    ramp_s: float = 0.0  # ramp window
    base_tps: float = 0.0  # spike baseline
    spike_tps: float = 0.0  # spike peak
    spike_every_s: float = 0.0  # cycle length (start of spike to next)
    spike_duration_s: float = 0.0  # how long each spike holds

    # soak profile — values already per-worker
    connections: int = 0
    heartbeat_interval_s: float = 30.0
    open_rate_per_s: float = 0.0  # 0 = open all at once

    # safety valve: max concurrent in-flight transactions on this worker
    max_in_flight: int = 2000

    #: cap on *outstanding* (launched-but-unfinished) transactions on this worker
    #: — the backpressure that stops a slow target from accumulating a huge
    #: phantom backlog of "sent" work that can never be dispatched in time.
    #: ``0`` (default) ⇒ cap at ``max_in_flight`` (closed-loop: never queue more
    #: than the concurrency limit, so ``sent`` reflects what was actually
    #: dispatched and a run reconciles in seconds). ``> 0`` ⇒ that explicit cap.
    #: ``< 0`` ⇒ unbounded (legacy open-loop: the pacer creates at the target
    #: rate regardless, and the shortfall queues — measures coordinated omission
    #: but inflates ``sent``/``pending`` under saturation).
    max_backlog: int = 0

    #: grace period (seconds) after the send window closes during which no new
    #: transactions are launched but outstanding ones are allowed to finish.
    #: ``> 0`` waits up to that long then aborts the remainder; ``< 0`` waits
    #: until every outstanding transaction completes (no timeout); ``0`` cuts
    #: immediately. See :meth:`LoadDriver._drain`.
    drain_s: float = 30.0

    scenario_id: str | None = None

    def target_tps_at(self, elapsed: float) -> float:
        """The instantaneous per-worker target TPS at ``elapsed`` seconds."""
        if self.type == STEADY:
            return self.target_tps
        if self.type == RAMP:
            if self.ramp_s <= 0:
                return self.end_tps
            frac = min(1.0, max(0.0, elapsed / self.ramp_s))
            return self.start_tps + (self.end_tps - self.start_tps) * frac
        if self.type == SPIKE:
            if self.spike_every_s <= 0:
                return self.base_tps
            pos = elapsed % self.spike_every_s
            in_spike = pos >= (self.spike_every_s - self.spike_duration_s)
            return self.spike_tps if in_spike else self.base_tps
        return 0.0

    def apply_retune(self, fleet_params: dict[str, Any]) -> None:
        """Adjust this shard's per-worker targets from fleet-wide totals.

        The operator re-tunes the whole run (e.g. "now drive 5000 TPS"); each
        worker recomputes its own slice with the same even split used at shard
        time, so the fleet still sums back to the requested total. Only the
        knobs relevant to this shard's profile type are touched.

        ``workers`` (when present) is the new fleet size after a live re-scale:
        this shard adopts it before re-splitting, so a worker sharded as 1-of-2
        becomes 1-of-4 and old + new shards sum to the fleet total instead of
        overdriving the target."""
        if fleet_params.get("workers"):
            self.worker_count = max(1, int(fleet_params["workers"]))
        n, i = self.worker_count, self.worker_index
        if self.type == STEADY and fleet_params.get("target_tps") is not None:
            self.target_tps = _split(float(fleet_params["target_tps"]), n, i)
        if self.type == SPIKE:
            if fleet_params.get("base_tps") is not None:
                self.base_tps = _split(float(fleet_params["base_tps"]), n, i)
            if fleet_params.get("spike_tps") is not None:
                self.spike_tps = _split(float(fleet_params["spike_tps"]), n, i)
        if self.type == RAMP and fleet_params.get("end_tps") is not None:
            self.end_tps = _split(float(fleet_params["end_tps"]), n, i)

    def phase_at(self, elapsed: float) -> str:
        if self.type == SPIKE:
            pos = elapsed % self.spike_every_s if self.spike_every_s else 0
            return "spike" if pos >= (self.spike_every_s - self.spike_duration_s) else "base"
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Shard":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class LoadProfile:
    """A complete load test, fanned across ``workers`` processes."""

    type: str
    duration_s: float
    workers: int = 1
    scenario_id: str | None = None
    name: str = ""

    # TPS-shaped profiles (fleet-wide totals)
    target_tps: float = 0.0
    start_tps: float = 0.0
    end_tps: float = 0.0
    ramp_s: float = 0.0
    base_tps: float = 0.0
    spike_tps: float = 0.0
    spike_every_s: float = 0.0
    spike_duration_s: float = 0.0

    # soak profile (fleet-wide totals)
    connections: int = 0
    heartbeat_interval_s: float = 30.0
    open_rate_per_s: float = 0.0

    max_in_flight_per_worker: int = 2000

    #: fleet-wide outstanding-transaction cap (same value per worker — see
    #: :attr:`Shard.max_backlog`). ``0`` ⇒ cap at the concurrency limit.
    max_backlog_per_worker: int = 0

    #: fleet-wide grace period for outstanding transactions when the send window
    #: closes (same value on every worker — see :attr:`Shard.drain_s`).
    drain_s: float = 30.0

    def validate(self) -> None:
        if self.type not in (_TPS_TYPES | {SOAK}):
            raise ValueError(f"unknown load profile type: {self.type!r}")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.duration_s <= 0 and self.type != SOAK:
            raise ValueError("duration_s must be > 0")
        if self.type == STEADY and self.target_tps <= 0:
            raise ValueError("steady profile needs target_tps > 0")
        if self.type == RAMP and self.end_tps <= 0:
            raise ValueError("ramp profile needs end_tps > 0")
        if self.type == SPIKE and (self.base_tps <= 0 or self.spike_tps <= 0):
            raise ValueError("spike profile needs base_tps and spike_tps > 0")
        if self.type == SOAK and self.connections <= 0:
            raise ValueError("soak profile needs connections > 0")

    def shard(self, worker_index: int) -> Shard:
        """Build the Shard for worker ``worker_index`` (0-based)."""
        i, n = worker_index, self.workers
        return Shard(
            run_id="",  # filled in by the coordinator
            worker_index=i,
            worker_count=n,
            type=self.type,
            duration_s=self.duration_s,
            target_tps=_split(self.target_tps, n, i),
            start_tps=_split(self.start_tps, n, i),
            end_tps=_split(self.end_tps, n, i),
            ramp_s=self.ramp_s,
            base_tps=_split(self.base_tps, n, i),
            spike_tps=_split(self.spike_tps, n, i),
            spike_every_s=self.spike_every_s,
            spike_duration_s=self.spike_duration_s,
            connections=int(_split(self.connections, n, i)),
            heartbeat_interval_s=self.heartbeat_interval_s,
            open_rate_per_s=_split(self.open_rate_per_s, n, i) if self.open_rate_per_s else 0.0,
            max_in_flight=self.max_in_flight_per_worker,
            max_backlog=self.max_backlog_per_worker,
            drain_s=self.drain_s,
            scenario_id=self.scenario_id,
        )

    def shards(self, run_id: str) -> list[Shard]:
        out = []
        for i in range(self.workers):
            s = self.shard(i)
            s.run_id = run_id
            out.append(s)
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LoadProfile":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in fields})
