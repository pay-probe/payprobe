"""The load driver — one per worker process.

The driver is deliberately decoupled from adapters: it is handed *coroutines*,
not a registry. For TPS-shaped profiles it calls ``run_once()`` and expects
``(ok, latency_ms)``. For soak it calls ``open_client()`` to get a
:class:`SoakClient` whose ``heartbeat()`` it loops. The real worker wires those
to the adapter registry; tests pass fakes. This is what makes the pacing logic
trivially unit-testable without a payment switch.

Pacing uses a token accumulator: every tick we add ``rate * dt`` tokens and
launch ``floor`` of them, so the average send rate tracks
:meth:`Shard.target_tps_at` regardless of tick jitter. In-flight work is capped
by a semaphore (``shard.max_in_flight``) so a slow target applies backpressure
instead of melting the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from .histogram import LatencyHistogram, LoadStats
from .profile import Shard, SOAK

log = logging.getLogger("payprobe.load_driver")

RunOnce = Callable[[], Awaitable[tuple[bool, float]]]


class SoakClient(Protocol):
    async def heartbeat(self) -> tuple[bool, float]: ...
    async def aclose(self) -> None: ...


OpenClient = Callable[[], Awaitable[SoakClient]]
OnSample = Callable[[dict[str, Any]], Awaitable[None]]


class LoadDriver:
    def __init__(
        self,
        shard: Shard,
        *,
        run_once: RunOnce | None = None,
        open_client: OpenClient | None = None,
        on_sample: OnSample | None = None,
        sample_interval_s: float = 1.0,
        tick_s: float = 0.005,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.shard = shard
        self._run_once = run_once
        self._open_client = open_client
        self._on_sample = on_sample
        self._sample_interval = sample_interval_s
        self._tick = tick_s
        self._clock = clock

        self.hist = LatencyHistogram()
        #: transactions the pacer *produced* at the target rate — the offered
        #: load. Each generated txn is either put on the wire (``sent``) or, when
        #: the worker is at its cap, dropped (``throttled``): generated == sent +
        #: throttled.
        self.generated = 0
        #: transactions actually put on the wire (dispatched to the target). NOT
        #: incremented for throttled/never-dispatched attempts.
        self.sent = 0
        self.received = 0
        self.errors = 0
        self.live = 0
        self.dead = 0
        self.reconnects = 0
        #: last failure reason seen — surfaced so a run that errors on every
        #: transaction tells the operator *why* instead of just "0 TPS".
        self.last_error: str | None = None
        #: per-category error counts (e.g. ``{"connect/TimeoutError": 12}``).
        #: Categorised to the failure *type*, not the full message, so they
        #: aggregate across the fleet and across runs — the basis for the
        #: "top error categories" run-vs-run comparison. Bounded to keep an
        #: adversarial adapter from filling memory with unique messages.
        self.error_categories: dict[str, int] = {}

        # -- saturation / backpressure -------------------------------------
        #: max concurrent in-flight permitted on this worker (the semaphore cap).
        self.max_in_flight = max(1, shard.max_in_flight)
        #: current in-flight transactions (holding a permit, not yet done).
        self.in_flight = 0
        #: high-water mark of ``in_flight`` over the run.
        self.peak_in_flight = 0
        #: generated transactions that were NOT put on the wire because the worker
        #: was at its in-flight/backlog cap — i.e. the target, not the worker, is
        #: the bottleneck. Throttled ⇒ *not sent*.
        self.throttled = 0
        #: transactions that WERE sent (on the wire) but cancelled at end-of-run
        #: because they didn't finish within the drain window. Keeps ``pending``
        #: from leaving a phantom backlog — raise ``drain_s`` to reduce it to zero.
        self.aborted = 0

        self._start = 0.0
        self._last_sample_received = 0
        self._last_sample_t = 0.0
        self._cur_tps = 0.0
        self._in_flight = asyncio.Semaphore(self.max_in_flight)
        #: set only once the run is *fully* finished (send window **and** drain).
        #: The sampler keys off this — not the send-loop ``stop`` — so metric
        #: samples keep flowing while outstanding transactions drain, and the
        #: operator/coordinator see ``pending`` fall to zero instead of the
        #: stream freezing the instant the send window closes.
        self._done = asyncio.Event()

    # -- public API ----------------------------------------------------------
    async def run(self, stop: asyncio.Event | None = None) -> LoadStats:
        stop = stop or asyncio.Event()
        self._start = self._clock()
        self._last_sample_t = self._start
        # the sampler runs until the whole run (incl. drain) is done, so the
        # drain is observable; the send loops key off ``stop``.
        sampler = asyncio.create_task(self._sample_loop(self._done))
        try:
            if self.shard.type == SOAK:
                await self._run_soak(stop)
            else:
                await self._run_tps(stop)
        finally:
            self._done.set()
            await sampler
        return self.stats()

    def stats(self) -> LoadStats:
        return LoadStats(
            generated=self.generated,
            sent=self.sent,
            received=self.received,
            errors=self.errors,
            live=self.live,
            dead=self.dead,
            reconnects=self.reconnects,
            elapsed_s=self._clock() - self._start if self._start else 0.0,
            tps=self._cur_tps,
            target_tps=self.shard.target_tps_at(self._elapsed()),
            in_flight=self.in_flight,
            max_in_flight=self.max_in_flight,
            peak_in_flight=self.peak_in_flight,
            throttled=self.throttled,
            aborted=self.aborted,
            hist=self.hist,
        )

    def sample(self) -> dict[str, Any]:
        """Cumulative snapshot for shipping over the bus."""
        return {
            "worker_index": self.shard.worker_index,
            "run_id": self.shard.run_id,
            "generated": self.generated,
            "sent": self.sent,
            "received": self.received,
            "errors": self.errors,
            "live": self.live,
            "dead": self.dead,
            "reconnects": self.reconnects,
            "elapsed_s": self._elapsed(),
            "tps": self._cur_tps,
            "target_tps": self.shard.target_tps_at(self._elapsed()),
            "phase": self.shard.phase_at(self._elapsed()),
            "in_flight": self.in_flight,
            "max_in_flight": self.max_in_flight,
            "peak_in_flight": self.peak_in_flight,
            "throttled": self.throttled,
            "aborted": self.aborted,
            "hist": self.hist.snapshot(),
            "last_error": self.last_error,
            "error_categories": dict(self.error_categories),
        }

    #: cap on distinct error categories tracked per worker; overflow is lumped
    #: into ``"other"`` so a misbehaving adapter can't grow this without bound.
    _MAX_ERROR_CATEGORIES = 50

    @staticmethod
    def categorize_error(error: str) -> str:
        """Normalise a raw error string to a stable, low-cardinality category.

        Failures are keyed by *type* (and connect/reconnect phase) rather than
        their full message, so ``"connect: TimeoutError: [Errno 60] ..."`` and
        ``"connect: TimeoutError: [Errno 60] other host"`` collapse to the same
        ``"connect/TimeoutError"`` bucket. That makes counts addable across
        workers and comparable across runs."""
        e = (error or "").strip()
        if not e:
            return "error"
        parts = [p.strip() for p in e.split(":")]
        head = parts[0]
        if head in ("connect", "reconnect") and len(parts) > 1 and parts[1]:
            return f"{head}/{parts[1][:48]}"
        return (head or "error")[:60]

    def _bump_error_category(self, error: str | None) -> None:
        cat = self.categorize_error(error or "")
        cats = self.error_categories
        if cat not in cats and len(cats) >= self._MAX_ERROR_CATEGORIES:
            cat = "other"
        cats[cat] = cats.get(cat, 0) + 1

    # -- internals -----------------------------------------------------------
    def _elapsed(self) -> float:
        return self._clock() - self._start if self._start else 0.0

    async def _record_result(self, ok: bool, latency_ms: float, error: str | None = None) -> None:
        if ok:
            self.received += 1
            self.hist.record(latency_ms)
        else:
            self.errors += 1
            self._bump_error_category(error)
            if error:
                self.last_error = error[:300]

    @staticmethod
    def _unpack(result: Any) -> tuple[bool, float, str | None]:
        """run_once / heartbeat may return (ok, latency) or (ok, latency, error)."""
        if isinstance(result, tuple) and len(result) >= 3:
            return bool(result[0]), float(result[1]), result[2]
        ok, latency = result
        return bool(ok), float(latency), None

    async def _fire(self) -> None:
        # Acquire an in-flight permit, then dispatch. ``sent`` is incremented here
        # — the moment the transaction actually goes on the wire — not when the
        # task was created, so ``sent`` means *sent* (a task cancelled while still
        # waiting for a permit was never sent and is not counted here).
        async with self._in_flight:
            self.in_flight += 1
            if self.in_flight > self.peak_in_flight:
                self.peak_in_flight = self.in_flight
            self.sent += 1
            try:
                ok, latency, error = self._unpack(await self._run_once())  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 — a thrown adapter call is an error sample
                ok, latency, error = False, 0.0, f"{type(exc).__name__}: {exc}"
            finally:
                self.in_flight -= 1
            await self._record_result(ok, latency, error)

    def _backlog_cap(self) -> int | None:
        """Max outstanding (launched-but-unfinished) transactions, or None for
        unbounded. ``max_backlog`` 0 ⇒ the concurrency limit; >0 ⇒ explicit;
        <0 ⇒ unbounded (legacy open-loop)."""
        mb = self.shard.max_backlog
        if mb < 0:
            return None
        return self.max_in_flight if mb == 0 else mb

    async def _run_tps(self, stop: asyncio.Event) -> None:
        if self._run_once is None:
            raise ValueError("TPS profile requires run_once")
        accum = 0.0
        last = self._clock()
        tasks: set[asyncio.Task] = set()
        cap = self._backlog_cap()
        deadline = self._start + self.shard.duration_s
        while not stop.is_set() and self._clock() < deadline:
            now = self._clock()
            dt = now - last
            last = now
            rate = self.shard.target_tps_at(now - self._start)
            accum += rate * dt
            n = int(accum)
            accum -= n
            for _ in range(n):
                # Every attempt the pacer produces is "generated" offered load.
                self.generated += 1
                # Backpressure: don't launch beyond the outstanding cap. A slow
                # target then shows up as ``throttled`` (generated but NOT sent)
                # instead of an unbounded backlog of phantom work — so ``sent``
                # tracks what's actually on the wire and the run reconciles
                # quickly at drain.
                if cap is not None and len(tasks) >= cap:
                    self.throttled += 1
                    continue
                t = asyncio.create_task(self._fire())
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._tick)
            except asyncio.TimeoutError:
                pass
        # The send window is over (duration reached, or a stop was signalled).
        # Drain outstanding transactions so each one that was launched resolves
        # to a response, an error, or an explicit timeout — rather than being
        # abandoned when run() returns. The window is operator-configurable via
        # ``drain_s``.
        await self._drain(set(tasks))

    async def _drain(self, tasks: set[asyncio.Task]) -> None:
        """Let outstanding transactions finish, per ``shard.drain_s``.

        ``drain_s > 0`` waits up to that many seconds, then times out whatever is
        still outstanding. ``drain_s < 0`` waits until every launched transaction
        resolves (no drain timeout — each still resolves under its own adapter
        timeout, so a dead target can't hang it forever): the "nothing sent is
        lost" option. ``drain_s == 0`` times out the backlog immediately.

        A cancelled transaction that was already on the wire counts as
        :attr:`aborted` (sent but timed out); one still waiting for a permit was
        never sent, so it counts as :attr:`throttled`. Either way the accounting
        closes: ``generated == sent + throttled`` and
        ``sent == received + errors + aborted``."""
        if not tasks:
            return
        window = self.shard.drain_s
        if window < 0:
            await asyncio.wait(tasks)  # wait for all, no drain timeout
            return
        if window > 0:
            _, pending = await asyncio.wait(tasks, timeout=window)
        else:
            pending = set(tasks)
        if pending:
            # snapshot how many are actually on the wire *before* cancelling, so
            # we can split the cancelled set into sent-but-timed-out vs never-sent.
            on_wire = self.in_flight
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            cancelled = sum(1 for t in pending if t.cancelled())
            aborted_now = min(cancelled, on_wire)  # were dispatched → timed out
            self.aborted += aborted_now
            self.throttled += cancelled - aborted_now  # never left the queue

    async def _run_soak(self, stop: asyncio.Event) -> None:
        if self._open_client is None:
            raise ValueError("soak profile requires open_client")
        n = self.shard.connections
        interval = self.shard.heartbeat_interval_s
        open_rate = self.shard.open_rate_per_s
        clients: list[asyncio.Task] = []

        async def client_loop(idx: int) -> None:
            client: SoakClient | None = None
            try:
                client = await self._open_client()  # type: ignore[misc]
                self.live += 1
            except Exception as exc:  # noqa: BLE001
                self.dead += 1
                self.last_error = f"connect: {type(exc).__name__}: {exc}"[:300]
                return
            try:
                while not stop.is_set():
                    # soak has no pacer cap: every heartbeat is generated and sent.
                    self.generated += 1
                    self.sent += 1
                    try:
                        ok, latency, error = self._unpack(await client.heartbeat())
                    except Exception as exc:  # noqa: BLE001
                        ok, latency, error = False, 0.0, f"{type(exc).__name__}: {exc}"
                    await self._record_result(ok, latency, error)
                    if not ok:
                        # try one reconnect before declaring the client dead
                        try:
                            await client.aclose()
                        except Exception as exc:  # noqa: BLE001 — closing a dead client
                            log.debug("soak: error closing client before reconnect: %s", exc)
                        try:
                            client = await self._open_client()  # type: ignore[misc]
                            self.reconnects += 1
                        except Exception as exc:  # noqa: BLE001
                            self.live -= 1
                            self.dead += 1
                            self.last_error = f"reconnect: {type(exc).__name__}: {exc}"[:300]
                            return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception as exc:  # noqa: BLE001 — best-effort teardown
                        log.debug("soak: error closing client on teardown: %s", exc)

        # open connections, optionally paced to avoid a thundering herd
        for i in range(n):
            clients.append(asyncio.create_task(client_loop(i)))
            if open_rate > 0 and i < n - 1:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0 / open_rate)
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                break

        # hold for the duration (0 / negative ⇒ until stopped)
        if self.shard.duration_s > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.shard.duration_s)
            except asyncio.TimeoutError:
                pass
        else:
            await stop.wait()
        stop.set()
        if clients:
            # honour drain_s for the client-loop teardown too (< 0 = wait for all)
            timeout = None if self.shard.drain_s < 0 else max(self.shard.drain_s, 1.0)
            await asyncio.wait(clients, timeout=timeout)

    async def _sample_loop(self, done: asyncio.Event) -> None:
        # ``done`` is set only after the drain, so samples keep flowing while
        # outstanding transactions drain — the operator watches ``pending`` fall.
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=self._sample_interval)
            except asyncio.TimeoutError:
                pass
            self._refresh_tps()
            if self._on_sample is not None:
                try:
                    await self._on_sample(self.sample())
                except Exception as exc:  # noqa: BLE001 — never let reporting kill the run
                    # Non-fatal, but a silently-failing reporter means the run's
                    # metrics vanish — log so "0 TPS" isn't a mystery.
                    log.warning(
                        "load shard %s: sample reporting failed: %s", self.shard.run_id, exc
                    )
        # one final sample so the coordinator sees terminal counters
        self._refresh_tps()
        if self._on_sample is not None:
            try:
                await self._on_sample(self.sample())
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "load shard %s: final sample reporting failed: %s", self.shard.run_id, exc
                )

    def _refresh_tps(self) -> None:
        now = self._clock()
        dt = now - self._last_sample_t
        if dt > 0:
            self._cur_tps = (self.received - self._last_sample_received) / dt
        self._last_sample_t = now
        self._last_sample_received = self.received
