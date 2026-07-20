"""Tiny, dependency-free Prometheus metrics.

A self-contained implementation of the three metric types we need — Counter,
Gauge, Histogram — plus text exposition (Prometheus format 0.0.4). We avoid the
``prometheus_client`` dependency so every service can vendor this one file
without pulling a library into its image; the API is a deliberate subset of the
official client's so it's familiar and swappable later.

Usage::

    from worker.engine.metrics import Counter, Gauge, Histogram, render

    REQS = Counter("http_requests_total", "HTTP requests", ["method", "code"])
    REQS.inc(method="GET", code="200")

    INFLIGHT = Gauge("runs_inflight", "Runs currently executing")
    INFLIGHT.inc(); INFLIGHT.dec()

    LAT = Histogram("http_request_seconds", "Request latency")
    LAT.observe(0.012, method="GET")

    text = render()  # hand to a /metrics endpoint

Everything is process-local and thread-safe. Series are created lazily per label
combination; use ``.remove(**labels)`` to drop a transient series (e.g. a load
run that finished) so its labels don't linger.
"""

from __future__ import annotations

import math
import threading
from typing import Iterable

_REGISTRY: list["_Metric"] = []
_REG_LOCK = threading.Lock()

DEFAULT_BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


def _escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Metric:
    typ = "untyped"

    def __init__(self, name: str, documentation: str, labelnames: Iterable[str] = ()):
        self.name = name
        self.doc = documentation
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()
        with _REG_LOCK:
            _REGISTRY.append(self)

    def _key(self, labels: dict) -> tuple:
        if set(labels) != set(self.labelnames):
            missing = set(self.labelnames) - set(labels)
            extra = set(labels) - set(self.labelnames)
            raise ValueError(f"{self.name}: label mismatch (missing={missing}, extra={extra})")
        return tuple(str(labels[n]) for n in self.labelnames)

    def _label_str(self, key: tuple, extra: tuple | None = None) -> str:
        pairs = [f'{n}="{_escape(v)}"' for n, v in zip(self.labelnames, key)]
        if extra:
            pairs.append(f'{extra[0]}="{_escape(extra[1])}"')
        return "{" + ",".join(pairs) + "}" if pairs else ""

    def remove(self, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._series.pop(key, None)  # type: ignore[attr-defined]

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {self.doc}", f"# TYPE {self.name} {self.typ}"]


class Counter(_Metric):
    typ = "counter"

    def __init__(self, name, documentation, labelnames=()):
        super().__init__(name, documentation, labelnames)
        self._series: dict[tuple, float] = {}

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._series[key] = self._series.get(key, 0.0) + amount

    def render(self) -> str:
        lines = self._header()
        with self._lock:
            items = list(self._series.items())
        if not items and not self.labelnames:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(f"{self.name}{self._label_str(key)} {_num(val)}")
        return "\n".join(lines)


class Gauge(_Metric):
    typ = "gauge"

    def __init__(self, name, documentation, labelnames=()):
        super().__init__(name, documentation, labelnames)
        self._series: dict[tuple, float] = {}

    def set(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._series[key] = float(value)

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._series[key] = self._series.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels) -> None:
        self.inc(-amount, **labels)

    def render(self) -> str:
        lines = self._header()
        with self._lock:
            items = list(self._series.items())
        if not items and not self.labelnames:
            items = [((), 0.0)]
        for key, val in items:
            lines.append(f"{self.name}{self._label_str(key)} {_num(val)}")
        return "\n".join(lines)


class Histogram(_Metric):
    typ = "histogram"

    def __init__(self, name, documentation, labelnames=(), buckets=DEFAULT_BUCKETS):
        super().__init__(name, documentation, labelnames)
        self.buckets = tuple(buckets)
        # key -> {"counts": [..per bucket..], "sum": float, "count": int}
        self._series: dict[tuple, dict] = {}

    def observe(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            s = self._series.get(key)
            if s is None:
                s = {"counts": [0] * len(self.buckets), "sum": 0.0, "count": 0}
                self._series[key] = s
            for i, b in enumerate(self.buckets):
                if value <= b:
                    s["counts"][i] += 1
            s["sum"] += value
            s["count"] += 1

    def render(self) -> str:
        lines = self._header()
        with self._lock:
            items = list(self._series.items())
        for key, s in items:
            cumulative = 0
            for i, b in enumerate(self.buckets):
                cumulative = s["counts"][i]
                le = "+Inf" if math.isinf(b) else _num(b)
                lines.append(f"{self.name}_bucket{self._label_str(key, ('le', le))} {cumulative}")
            lines.append(f"{self.name}_bucket{self._label_str(key, ('le', '+Inf'))} {s['count']}")
            lines.append(f"{self.name}_sum{self._label_str(key)} {_num(s['sum'])}")
            lines.append(f"{self.name}_count{self._label_str(key)} {s['count']}")
        return "\n".join(lines)


def _num(v: float) -> str:
    """Render a number the way Prometheus expects (ints without trailing .0)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v)


def render() -> str:
    """Full text exposition for all registered metrics."""
    with _REG_LOCK:
        metrics = list(_REGISTRY)
    return "\n".join(m.render() for m in metrics) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
