"""PayProbe distributed load-testing subsystem.

The functional engine (``WorkerEngine``) runs each scenario *once* through the
three-phase gate. Load testing is a different shape: drive a transaction at a
sustained rate, or hold many long-lived connections, for a fixed duration, and
report throughput + latency percentiles.

The work is split across *separate* worker processes so a single Python event
loop never has to source 20K TPS / 100K connections on its own. A coordinator
shards a :class:`LoadProfile` into one :class:`Shard` per worker; each worker
runs a :class:`LoadDriver` over the same adapter registry the functional engine
uses, and streams metric samples back. The coordinator merges those samples —
latency histograms add bucket-for-bucket, so global p95/p99 stay exact — into a
live rolled-up snapshot.

Transport between coordinator and workers is the :class:`LoadBus`: an in-memory
implementation for tests/dev and a Redis Streams implementation for real
multi-process fleets (the same split the event backbone already uses).
"""

from .bus import WORKER_TTL_S, InMemoryLoadBus, LoadBus, RedisLoadBus
from .driver import LoadDriver, SoakClient
from .histogram import LatencyHistogram, LoadStats, merge_samples
from .profile import (
    LOAD_COMPLETED,
    LOAD_SAMPLE,
    RAMP,
    SOAK,
    SPIKE,
    STEADY,
    LoadProfile,
    Shard,
)

__all__ = [
    "LOAD_COMPLETED",
    "LOAD_SAMPLE",
    "RAMP",
    "SOAK",
    "SPIKE",
    "STEADY",
    "WORKER_TTL_S",
    "InMemoryLoadBus",
    "LatencyHistogram",
    "LoadBus",
    "LoadDriver",
    "LoadProfile",
    "LoadStats",
    "RedisLoadBus",
    "Shard",
    "SoakClient",
    "merge_samples",
]
