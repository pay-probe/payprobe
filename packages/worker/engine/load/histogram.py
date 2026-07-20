"""Mergeable latency histogram and rolled-up load statistics.

Why a histogram and not reservoir sampling: percentiles from independent
workers have to be combined into one global figure. Reservoir samples don't
merge cleanly (you'd average percentiles, which is wrong). Fixed-bucket
histograms do — add the workers' bucket counts element-for-element and read the
percentile off the sum. Global p95/p99 stay exact to the bucket width.

Buckets are quasi-logarithmic: fine near zero (sub-millisecond steps) and
coarser as latency grows, covering 0 → ~120 s with ~250 buckets. That keeps the
relative error of any percentile to roughly the bucket's own width.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any


def _build_bounds() -> list[float]:
    """Upper bounds (ms) for each bucket, monotonically increasing.

    Sub-bucketed by powers of two (HdrHistogram-style): within each binade we
    place ``SUB`` evenly spaced buckets, giving constant relative resolution.
    """
    SUB = 16
    bounds: list[float] = []
    # linear floor below 1ms so sub-millisecond latencies aren't all bucket 0
    for i in range(1, SUB + 1):
        bounds.append(i / SUB)  # 0.0625 .. 1.0 ms
    unit = 1.0
    while unit < 120_000:  # up to ~2 minutes
        for s in range(1, SUB + 1):
            bounds.append(unit * (1 + s / SUB))
        unit *= 2
    return bounds


_BOUNDS = _build_bounds()
_N = len(_BOUNDS)


class LatencyHistogram:
    """Bucketed latency counts in milliseconds. Thread-safety is the caller's
    job (the driver only touches it from its own event loop)."""

    __slots__ = ("counts", "_count", "_min", "_max", "_sum")

    def __init__(self) -> None:
        self.counts = [0] * _N
        self._count = 0
        self._min = float("inf")
        self._max = 0.0
        self._sum = 0.0

    def record(self, latency_ms: float) -> None:
        if latency_ms < 0:
            latency_ms = 0.0
        idx = bisect_right(_BOUNDS, latency_ms)
        if idx >= _N:
            idx = _N - 1
        self.counts[idx] += 1
        self._count += 1
        self._sum += latency_ms
        if latency_ms < self._min:
            self._min = latency_ms
        if latency_ms > self._max:
            self._max = latency_ms

    # -- merge ---------------------------------------------------------------
    def merge_counts(
        self, counts: list[int], lo: float, hi: float, total: float, samples: int
    ) -> None:
        """Fold another histogram's raw state into this one."""
        for i, c in enumerate(counts):
            if c:
                self.counts[i] += c
        self._count += samples
        self._sum += total
        if samples and lo < self._min:
            self._min = lo
        if hi > self._max:
            self._max = hi

    # -- reads ---------------------------------------------------------------
    @property
    def count(self) -> int:
        return self._count

    @property
    def min(self) -> float:
        return 0.0 if self._min == float("inf") else self._min

    @property
    def max(self) -> float:
        return self._max

    @property
    def mean(self) -> float:
        return self._sum / self._count if self._count else 0.0

    def percentile(self, p: float) -> float:
        """The ``p``-th percentile latency in ms (p in 0..100)."""
        if self._count == 0:
            return 0.0
        target = p / 100.0 * self._count
        cum = 0
        for i, c in enumerate(self.counts):
            cum += c
            if cum >= target:
                # midpoint of the bucket, clamped to observed [min, max]
                lo = _BOUNDS[i - 1] if i else 0.0
                hi = _BOUNDS[i]
                mid = (lo + hi) / 2.0
                return max(self.min, min(mid, self.max))
        return self.max

    def snapshot(self) -> dict[str, Any]:
        """Compact, JSON-safe state for shipping over the bus (sparse buckets)."""
        sparse = {str(i): c for i, c in enumerate(self.counts) if c}
        return {
            "buckets": sparse,
            "min": self.min,
            "max": self.max,
            "sum": self._sum,
            "samples": self._count,
        }

    def merge_snapshot(self, snap: dict[str, Any]) -> None:
        buckets = snap.get("buckets", {})
        for k, c in buckets.items():
            self.counts[int(k)] += int(c)
        s = int(snap.get("samples", 0))
        self._count += s
        self._sum += float(snap.get("sum", 0.0))
        lo = float(snap.get("min", 0.0))
        hi = float(snap.get("max", 0.0))
        if s and lo < self._min:
            self._min = lo
        if hi > self._max:
            self._max = hi


@dataclass
class LoadStats:
    """A point-in-time rolled-up view of a load run (one worker or the fleet)."""

    generated: int = 0  # offered load produced by the pacer (= sent + throttled)
    sent: int = 0
    received: int = 0
    errors: int = 0
    live: int = 0
    dead: int = 0
    reconnects: int = 0
    elapsed_s: float = 0.0
    tps: float = 0.0  # achieved throughput over the last window
    target_tps: float = 0.0
    last_error: str | None = None
    # -- saturation / backpressure (summed across the fleet) -----------------
    in_flight: int = 0
    max_in_flight: int = 0
    peak_in_flight: int = 0
    throttled: int = 0
    #: transactions launched but cancelled at end-of-run because they didn't
    #: finish within the drain window (summed across the fleet).
    aborted: int = 0
    #: per-category error counts, summed across the fleet (see
    #: ``LoadDriver.categorize_error``). Empty when nothing failed.
    error_categories: dict[str, int] = field(default_factory=dict)
    hist: LatencyHistogram = field(default_factory=LatencyHistogram)

    @property
    def pending(self) -> int:
        # aborted transactions were counted in ``sent`` but cancelled at drain,
        # so they're neither received, errored, nor still outstanding.
        return max(0, self.sent - self.received - self.errors - self.aborted)

    @property
    def saturation(self) -> float:
        """In-flight as a fraction of fleet capacity (0..1); 1.0 ⇒ saturated."""
        return (self.in_flight / self.max_in_flight) if self.max_in_flight else 0.0

    def summary(self) -> dict[str, Any]:
        h = self.hist
        return {
            "generated": self.generated,
            "sent": self.sent,
            "received": self.received,
            "errors": self.errors,
            "pending": self.pending,
            "live": self.live,
            "dead": self.dead,
            "reconnects": self.reconnects,
            "elapsed_s": round(self.elapsed_s, 3),
            "tps": round(self.tps, 1),
            "target_tps": round(self.target_tps, 1),
            "last_error": self.last_error,
            "in_flight": self.in_flight,
            "max_in_flight": self.max_in_flight,
            "peak_in_flight": self.peak_in_flight,
            "throttled": self.throttled,
            "aborted": self.aborted,
            "saturation": round(self.saturation, 3),
            "error_categories": dict(
                sorted(self.error_categories.items(), key=lambda kv: kv[1], reverse=True)
            ),
            "latency_ms": {
                "min": round(h.min, 2),
                "mean": round(h.mean, 2),
                "p50": round(h.percentile(50), 2),
                "p95": round(h.percentile(95), 2),
                "p99": round(h.percentile(99), 2),
                "max": round(h.max, 2),
                "samples": h.count,
            },
        }


def merge_samples(samples: list[dict[str, Any]]) -> LoadStats:
    """Merge the latest per-worker sample dicts into one fleet-wide LoadStats.

    Each sample is what :meth:`LoadDriver.sample` produces: cumulative counters
    plus a histogram snapshot. Counters add; histograms merge bucket-wise.
    """
    out = LoadStats()
    for s in samples:
        out.generated += int(s.get("generated", 0))
        out.sent += int(s.get("sent", 0))
        out.received += int(s.get("received", 0))
        out.errors += int(s.get("errors", 0))
        out.live += int(s.get("live", 0))
        out.dead += int(s.get("dead", 0))
        out.reconnects += int(s.get("reconnects", 0))
        out.target_tps += float(s.get("target_tps", 0.0))
        # fleet throughput is the sum of each worker's current rate
        out.tps += float(s.get("tps", 0.0))
        # saturation/backpressure: in-flight and capacity sum across the fleet
        out.in_flight += int(s.get("in_flight", 0))
        out.max_in_flight += int(s.get("max_in_flight", 0))
        out.peak_in_flight += int(s.get("peak_in_flight", 0))
        out.throttled += int(s.get("throttled", 0))
        out.aborted += int(s.get("aborted", 0))
        out.elapsed_s = max(out.elapsed_s, float(s.get("elapsed_s", 0.0)))
        if s.get("last_error"):
            out.last_error = s["last_error"]
        for cat, n in (s.get("error_categories") or {}).items():
            out.error_categories[cat] = out.error_categories.get(cat, 0) + int(n)
        hsnap = s.get("hist")
        if hsnap:
            out.hist.merge_snapshot(hsnap)
    return out
