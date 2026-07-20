"""Coordinator <-> worker transport for a load run.

Three primitives, mirroring the event-backbone's in-memory / Redis split:

* **shard claim** — the coordinator pushes one shard per worker onto a queue;
  each separate worker pops exactly one (atomic, so two workers never grab the
  same shard).
* **metric samples** — workers append cumulative snapshots to a per-run stream;
  the coordinator tails it and merges.
* **stop** — the coordinator raises a per-run stop flag; workers poll it.

``InMemoryLoadBus`` runs everything in one process (dev, tests, and the
single-node fallback). ``RedisLoadBus`` uses a Redis list for the claim queue
(``LPOP``/``RPUSH``), a Redis Stream for samples (``XADD``/``XREAD``), and a key
for the stop flag — so real, separate worker processes coordinate with nothing
but a shared Redis.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Protocol


class LoadBus(Protocol):
    async def push_shards(self, run_id: str, shards: list[dict[str, Any]]) -> None: ...
    async def claim_shard(self, run_id: str, timeout_s: float = 10.0) -> dict[str, Any] | None: ...
    async def pending_shards(self, run_id: str) -> int: ...
    async def report_sample(self, run_id: str, sample: dict[str, Any]) -> None: ...
    def samples(self, run_id: str) -> AsyncIterator[dict[str, Any]]: ...
    async def signal_stop(self, run_id: str) -> None: ...
    async def is_stopped(self, run_id: str) -> bool: ...
    async def mark_done(self, run_id: str) -> None: ...

    # -- hot re-tune (adjust target TPS on a running run) --------------------
    async def signal_retune(self, run_id: str, params: dict[str, Any]) -> None: ...
    async def get_retune(self, run_id: str) -> dict[str, Any] | None: ...

    # -- worker presence + per-worker drain ----------------------------------
    async def worker_heartbeat(self, worker_id: str, info: dict[str, Any]) -> None: ...
    async def list_workers(self) -> list[dict[str, Any]]: ...
    async def drop_worker(self, worker_id: str) -> None: ...
    async def signal_worker_stop(self, worker_id: str) -> None: ...
    async def is_worker_stopped(self, worker_id: str) -> bool: ...

    # -- participant-flow hosting (ADR-0001: networks on the worker fleet) ----
    # The coordinator pushes one *placement* per listener instance onto a global
    # queue; any flow-host worker claims it, starts the listener, and
    # *advertises* the bound endpoint into the run's instance registry. A
    # network-wide stop flag tears the run down across every host.
    async def push_placements(self, placements: list[dict[str, Any]]) -> None: ...
    async def claim_placement(self, timeout_s: float = 5.0) -> dict[str, Any] | None: ...
    async def advertise_instance(self, run_id: str, instance: dict[str, Any]) -> None: ...
    async def retract_instance(self, run_id: str, instance_id: str) -> None: ...
    async def list_instances(self, run_id: str) -> list[dict[str, Any]]: ...
    async def signal_network_stop(self, run_id: str) -> None: ...
    async def is_network_stopped(self, run_id: str) -> bool: ...

    # fleet trace aggregation: hosts ship new hop records with their heartbeat;
    # the orchestrator merges them into the Network Trace views. Capture is a
    # single fleet-wide gate mirroring the orchestrator's global toggle.
    async def report_flow_traces(self, run_id: str, records: list[dict[str, Any]]) -> None: ...
    async def flow_traces(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]: ...
    async def clear_flow_traces(self, run_id: str) -> None: ...
    async def set_flow_capture(self, enabled: bool) -> None: ...
    async def get_flow_capture(self) -> bool | None: ...


#: A worker is considered live if it heartbeat within this many seconds. The
#: worker refreshes well inside the window (see WORKER_HEARTBEAT_S) so a missed
#: beat — process gone — drops it from the fleet view quickly.
WORKER_TTL_S = 15

#: A hosted-listener advertisement is considered live within this window; flow
#: hosts refresh their instances' advertisements on every heartbeat, so a dead
#: host's listeners vanish from the registry (and from run health) quickly.
INSTANCE_TTL_S = 15

#: Retained fleet trace-hop records per network run (oldest dropped first).
FLOW_TRACES_MAX = 5000


class InMemoryLoadBus:
    """Single-process bus. Same instance shared by coordinator + in-proc workers."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[dict]] = {}
        self._samples: dict[str, list[dict]] = {}
        self._sample_evt: dict[str, asyncio.Event] = {}
        self._stop: set[str] = set()
        self._done: set[str] = set()
        #: run_id -> latest retune params (target_tps / base_tps / spike_tps)
        self._retune: dict[str, dict] = {}
        #: worker_id -> (last_seen_monotonic, info)
        self._workers: dict[str, tuple[float, dict]] = {}
        self._worker_stop: set[str] = set()
        # -- participant-flow hosting ----------------------------------------
        # A plain list + polling claim (not asyncio.Queue): the bus is a
        # process-wide singleton and a Queue binds to the first event loop that
        # touches it — under pytest each test runs its own loop.
        self._placements: list[dict] = []
        #: run_id -> instance_id -> (last_seen_monotonic, info)
        self._instances: dict[str, dict[str, tuple[float, dict]]] = {}
        self._net_stop: set[str] = set()
        #: run_id -> shipped trace-hop records (capped at FLOW_TRACES_MAX)
        self._flow_traces: dict[str, list[dict]] = {}
        self._flow_capture: bool | None = None

    def _q(self, run_id: str) -> asyncio.Queue:
        return self._queues.setdefault(run_id, asyncio.Queue())

    def _evt(self, run_id: str) -> asyncio.Event:
        return self._sample_evt.setdefault(run_id, asyncio.Event())

    async def push_shards(self, run_id: str, shards: list[dict[str, Any]]) -> None:
        q = self._q(run_id)
        for s in shards:
            await q.put(s)

    async def claim_shard(self, run_id: str, timeout_s: float = 10.0) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._q(run_id).get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    async def pending_shards(self, run_id: str) -> int:
        return self._q(run_id).qsize()

    async def report_sample(self, run_id: str, sample: dict[str, Any]) -> None:
        self._samples.setdefault(run_id, []).append(sample)
        self._evt(run_id).set()

    async def samples(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        idx = 0
        while True:
            buf = self._samples.get(run_id, [])
            while idx < len(buf):
                yield buf[idx]
                idx += 1
            if run_id in self._done:
                # flush any late arrivals then stop
                buf = self._samples.get(run_id, [])
                while idx < len(buf):
                    yield buf[idx]
                    idx += 1
                return
            evt = self._evt(run_id)
            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def signal_stop(self, run_id: str) -> None:
        self._stop.add(run_id)

    async def is_stopped(self, run_id: str) -> bool:
        return run_id in self._stop

    async def mark_done(self, run_id: str) -> None:
        self._done.add(run_id)
        self._evt(run_id).set()

    async def signal_retune(self, run_id: str, params: dict[str, Any]) -> None:
        self._retune[run_id] = dict(params)

    async def get_retune(self, run_id: str) -> dict[str, Any] | None:
        return self._retune.get(run_id)

    # -- worker presence -----------------------------------------------------
    async def worker_heartbeat(self, worker_id: str, info: dict[str, Any]) -> None:
        import time as _time

        self._workers[worker_id] = (_time.monotonic(), dict(info))

    async def list_workers(self) -> list[dict[str, Any]]:
        import time as _time

        now = _time.monotonic()
        out: list[dict[str, Any]] = []
        for wid, (seen, info) in list(self._workers.items()):
            age = now - seen
            if age > WORKER_TTL_S:
                self._workers.pop(wid, None)  # expired → self-clean
                continue
            row = dict(info)
            row["worker_id"] = wid
            row["age_s"] = round(age, 1)
            row["draining"] = wid in self._worker_stop
            out.append(row)
        return out

    async def drop_worker(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)
        self._worker_stop.discard(worker_id)

    async def signal_worker_stop(self, worker_id: str) -> None:
        self._worker_stop.add(worker_id)

    async def is_worker_stopped(self, worker_id: str) -> bool:
        return worker_id in self._worker_stop

    # -- participant-flow hosting ---------------------------------------------
    async def push_placements(self, placements: list[dict[str, Any]]) -> None:
        self._placements.extend(dict(p) for p in placements)

    async def claim_placement(self, timeout_s: float = 5.0) -> dict[str, Any] | None:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            if self._placements:
                return self._placements.pop(0)
            if asyncio.get_event_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def advertise_instance(self, run_id: str, instance: dict[str, Any]) -> None:
        import time as _time

        iid = str(instance.get("instance_id"))
        self._instances.setdefault(run_id, {})[iid] = (_time.monotonic(), dict(instance))

    async def retract_instance(self, run_id: str, instance_id: str) -> None:
        self._instances.get(run_id, {}).pop(instance_id, None)

    async def list_instances(self, run_id: str) -> list[dict[str, Any]]:
        import time as _time

        now = _time.monotonic()
        out: list[dict[str, Any]] = []
        rows = self._instances.get(run_id, {})
        for iid, (seen, info) in list(rows.items()):
            if now - seen > INSTANCE_TTL_S:
                rows.pop(iid, None)  # expired → self-clean (host gone)
                continue
            out.append(dict(info))
        return out

    async def signal_network_stop(self, run_id: str) -> None:
        self._net_stop.add(run_id)

    async def is_network_stopped(self, run_id: str) -> bool:
        return run_id in self._net_stop

    async def report_flow_traces(self, run_id: str, records: list[dict[str, Any]]) -> None:
        buf = self._flow_traces.setdefault(run_id, [])
        buf.extend(dict(r) for r in records)
        del buf[:-FLOW_TRACES_MAX]

    async def flow_traces(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        return [dict(r) for r in self._flow_traces.get(run_id, [])[-limit:]]

    async def clear_flow_traces(self, run_id: str) -> None:
        self._flow_traces.pop(run_id, None)

    async def set_flow_capture(self, enabled: bool) -> None:
        self._flow_capture = bool(enabled)

    async def get_flow_capture(self) -> bool | None:
        return self._flow_capture


class RedisLoadBus:
    """Redis-backed bus for real multi-process worker fleets."""

    def __init__(self, redis: Any, *, ttl_s: int = 86_400) -> None:
        self.redis = redis  # redis.asyncio.Redis, decode_responses=True
        self.ttl = ttl_s

    @staticmethod
    def _shard_key(run_id: str) -> str:
        return f"loadrun:{run_id}:shards"

    @staticmethod
    def _sample_key(run_id: str) -> str:
        return f"loadrun:{run_id}:samples"

    @staticmethod
    def _stop_key(run_id: str) -> str:
        return f"loadrun:{run_id}:stop"

    @staticmethod
    def _done_key(run_id: str) -> str:
        return f"loadrun:{run_id}:done"

    @staticmethod
    def _retune_key(run_id: str) -> str:
        return f"loadrun:{run_id}:retune"

    # presence: a self-expiring key per worker, plus an index set we prune lazily
    _WORKERS_INDEX = "loadworkers"

    @staticmethod
    def _worker_key(worker_id: str) -> str:
        return f"loadworker:{worker_id}"

    @staticmethod
    def _worker_stop_key(worker_id: str) -> str:
        return f"loadworker:{worker_id}:stop"

    async def push_shards(self, run_id: str, shards: list[dict[str, Any]]) -> None:
        key = self._shard_key(run_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            for s in shards:
                pipe.rpush(key, json.dumps(s))
            pipe.expire(key, self.ttl)
            await pipe.execute()

    async def claim_shard(self, run_id: str, timeout_s: float = 10.0) -> dict[str, Any] | None:
        # BLPOP blocks across processes until a shard is available or timeout.
        res = await self.redis.blpop(self._shard_key(run_id), timeout=int(max(1, timeout_s)))
        if not res:
            return None
        _key, payload = res
        return json.loads(payload)

    async def pending_shards(self, run_id: str) -> int:
        return int(await self.redis.llen(self._shard_key(run_id)))

    async def report_sample(self, run_id: str, sample: dict[str, Any]) -> None:
        key = self._sample_key(run_id)
        await self.redis.xadd(key, {"data": json.dumps(sample)}, maxlen=200_000, approximate=True)
        await self.redis.expire(key, self.ttl)

    async def samples(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        key = self._sample_key(run_id)
        last = "0"
        while True:
            resp = await self.redis.xread({key: last}, block=500, count=256)
            if resp:
                _k, entries = resp[0]
                for entry_id, fields in entries:
                    last = entry_id
                    yield json.loads(fields["data"])
            elif await self.redis.get(self._done_key(run_id)):
                # final non-blocking drain
                tail = await self.redis.xread({key: last}, count=512)
                if tail:
                    for entry_id, fields in tail[0][1]:
                        yield json.loads(fields["data"])
                return

    async def signal_stop(self, run_id: str) -> None:
        await self.redis.set(self._stop_key(run_id), "1", ex=self.ttl)

    async def is_stopped(self, run_id: str) -> bool:
        return bool(await self.redis.get(self._stop_key(run_id)))

    async def mark_done(self, run_id: str) -> None:
        await self.redis.set(self._done_key(run_id), "1", ex=self.ttl)

    async def signal_retune(self, run_id: str, params: dict[str, Any]) -> None:
        await self.redis.set(self._retune_key(run_id), json.dumps(params), ex=self.ttl)

    async def get_retune(self, run_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._retune_key(run_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    # -- worker presence -----------------------------------------------------
    async def worker_heartbeat(self, worker_id: str, info: dict[str, Any]) -> None:
        key = self._worker_key(worker_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            # TTL key — if the worker dies, it disappears within WORKER_TTL_S.
            pipe.set(key, json.dumps(info), ex=WORKER_TTL_S)
            pipe.sadd(self._WORKERS_INDEX, worker_id)
            await pipe.execute()

    async def list_workers(self) -> list[dict[str, Any]]:
        ids = await self.redis.smembers(self._WORKERS_INDEX)
        if not ids:
            return []
        ids = list(ids)
        async with self.redis.pipeline(transaction=False) as pipe:
            for wid in ids:
                pipe.get(self._worker_key(wid))
            for wid in ids:
                pipe.exists(self._worker_stop_key(wid))
            results = await pipe.execute()
        infos = results[: len(ids)]
        stops = results[len(ids) :]
        out: list[dict[str, Any]] = []
        stale: list[str] = []
        for wid, raw, draining in zip(ids, infos, stops):
            if not raw:  # key expired → prune the index
                stale.append(wid)
                continue
            try:
                row = json.loads(raw)
            except (TypeError, ValueError):
                continue
            row["worker_id"] = wid
            row["draining"] = bool(draining)
            out.append(row)
        if stale:
            await self.redis.srem(self._WORKERS_INDEX, *stale)
        return out

    async def drop_worker(self, worker_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._worker_key(worker_id))
            pipe.srem(self._WORKERS_INDEX, worker_id)
            await pipe.execute()

    async def signal_worker_stop(self, worker_id: str) -> None:
        await self.redis.set(self._worker_stop_key(worker_id), "1", ex=self.ttl)

    async def is_worker_stopped(self, worker_id: str) -> bool:
        return bool(await self.redis.get(self._worker_stop_key(worker_id)))

    # -- participant-flow hosting ---------------------------------------------
    #: global placement queue — any flow host may claim any placement.
    _PLACEMENTS_KEY = "flowhost:placements"

    @staticmethod
    def _instances_key(run_id: str) -> str:
        return f"flownet:{run_id}:instances"

    @staticmethod
    def _net_stop_key(run_id: str) -> str:
        return f"flownet:{run_id}:stop"

    async def push_placements(self, placements: list[dict[str, Any]]) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            for p in placements:
                pipe.rpush(self._PLACEMENTS_KEY, json.dumps(p))
            pipe.expire(self._PLACEMENTS_KEY, self.ttl)
            await pipe.execute()

    async def claim_placement(self, timeout_s: float = 5.0) -> dict[str, Any] | None:
        res = await self.redis.blpop(self._PLACEMENTS_KEY, timeout=int(max(1, timeout_s)))
        if not res:
            return None
        _key, payload = res
        return json.loads(payload)

    async def advertise_instance(self, run_id: str, instance: dict[str, Any]) -> None:
        import time as _time

        row = dict(instance)
        row["ts"] = _time.time()
        key = self._instances_key(run_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, str(instance.get("instance_id")), json.dumps(row))
            pipe.expire(key, self.ttl)
            await pipe.execute()

    async def retract_instance(self, run_id: str, instance_id: str) -> None:
        await self.redis.hdel(self._instances_key(run_id), instance_id)

    async def list_instances(self, run_id: str) -> list[dict[str, Any]]:
        import time as _time

        rows = await self.redis.hgetall(self._instances_key(run_id))
        now = _time.time()
        out: list[dict[str, Any]] = []
        stale: list[str] = []
        for iid, raw in (rows or {}).items():
            try:
                info = json.loads(raw)
            except (TypeError, ValueError):
                continue
            # advertisements are refreshed each host heartbeat — an old ts
            # means the host died; prune it so run health reflects reality.
            if now - float(info.get("ts", 0)) > INSTANCE_TTL_S:
                stale.append(iid)
                continue
            out.append(info)
        if stale:
            await self.redis.hdel(self._instances_key(run_id), *stale)
        return out

    async def signal_network_stop(self, run_id: str) -> None:
        await self.redis.set(self._net_stop_key(run_id), "1", ex=self.ttl)

    async def is_network_stopped(self, run_id: str) -> bool:
        return bool(await self.redis.get(self._net_stop_key(run_id)))

    @staticmethod
    def _flow_traces_key(run_id: str) -> str:
        return f"flownet:{run_id}:traces"

    _FLOW_CAPTURE_KEY = "flowhost:capture"

    async def report_flow_traces(self, run_id: str, records: list[dict[str, Any]]) -> None:
        key = self._flow_traces_key(run_id)
        async with self.redis.pipeline(transaction=False) as pipe:
            for r in records:
                pipe.xadd(key, {"data": json.dumps(r)}, maxlen=FLOW_TRACES_MAX, approximate=True)
            pipe.expire(key, self.ttl)
            await pipe.execute()

    async def flow_traces(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        entries = await self.redis.xrevrange(self._flow_traces_key(run_id), count=limit)
        out: list[dict[str, Any]] = []
        for _entry_id, fields in reversed(entries or []):
            try:
                out.append(json.loads(fields["data"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def clear_flow_traces(self, run_id: str) -> None:
        await self.redis.delete(self._flow_traces_key(run_id))

    async def set_flow_capture(self, enabled: bool) -> None:
        await self.redis.set(self._FLOW_CAPTURE_KEY, "1" if enabled else "0", ex=self.ttl)

    async def get_flow_capture(self) -> bool | None:
        raw = await self.redis.get(self._FLOW_CAPTURE_KEY)
        return None if raw is None else raw == "1"
