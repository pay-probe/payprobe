"""Standalone participant-flow host — the fleet role behind ADR-0001.

    REDIS_URL=redis://... python -m worker.flow_host

A flow host claims listener *placements* from the shared bus (one placement =
one instance of a participant flow in a network run), starts the corresponding
responder locally, and advertises the bound ``host:port`` into the run's
instance registry so the orchestrator's health/gate and the callers can find
it. Placements embed everything the responder needs (flow doc + listen config +
resolved downstream adapters), so — like ``load_worker`` — a host needs nothing
but the bus.

Lifecycle: a network-wide stop flag tears down every instance of a run across
all hosts; a per-worker drain (fleet panel) empties one host; advertisements
are refreshed with the heartbeat, so a killed host's instances disappear from
the registry within ``INSTANCE_TTL_S`` and the run shows degraded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Any

from .engine.load import RedisLoadBus
from .engine.load.bus import INSTANCE_TTL_S  # noqa: F401  (re-export for callers)

log = logging.getLogger("payprobe.flow_host")

#: Presence/advertisement refresh cadence — comfortably inside both TTLs.
HOST_HEARTBEAT_S = 5.0


def _build_responder(placement: dict[str, Any]):
    """Construct the right listener for a placement (mirrors the orchestrator's
    in-process ``_build_flow_responder`` / relay-proxy split — the decision was
    already made by the coordinator, we only instantiate)."""
    kind = str(placement.get("kind") or "responder")
    config = placement.get("config") or {}
    flow = placement.get("flow") or {}
    downstream = placement.get("downstream") or {}
    flow_id = placement.get("flow_id") or "flow"
    if kind == "proxy":
        from .adapters.tcp.proxy import TcpProxy

        return TcpProxy(placement.get("proxy_config") or config)
    adapter = str(config.get("adapter") or "").lower()
    proto = str(config.get("protocol") or "").lower()
    if adapter == "http" or proto == "http":
        from .adapters.http.flow_responder import HttpFlowResponder

        return HttpFlowResponder(config, flow, downstream=downstream, run_id=flow_id)
    from .adapters.tcp.flow_responder import FlowResponder

    return FlowResponder(config, flow, downstream=downstream, run_id=flow_id)


class _Hosted:
    """One locally hosted listener instance."""

    def __init__(self, run_id: str, placement: dict[str, Any], responder: Any) -> None:
        self.run_id = run_id
        self.placement = placement
        self.responder = responder
        #: newest trace ``ts`` already shipped to the bus (watermark — the
        #: responder's ring buffer trims from the front, so an index would drift).
        self.trace_watermark = 0.0

    def advertisement(self, host_id: str, advertise_host: str) -> dict[str, Any]:
        received = getattr(self.responder, "received", None)
        return {
            "instance_id": self.placement.get("instance_id"),
            "flow_id": self.placement.get("flow_id"),
            "flow_name": self.placement.get("flow_name") or self.placement.get("flow_id"),
            "node_id": self.placement.get("node_id"),
            "worker_id": host_id,
            "host": advertise_host,
            "port": getattr(self.responder, "port", None),
            "status": "up" if getattr(self.responder, "port", None) else "down",
            #: lightweight per-instance metric, refreshed with the heartbeat —
            #: the fleet's counterpart of a local participant's message count.
            "received": len(received) if received is not None else 0,
        }


async def ship_traces(bus, host_id: str, h: _Hosted) -> int:
    """Ship trace hops recorded since the last heartbeat to the bus, so the
    orchestrator's Network Trace stitches fleet-hosted hops too. Watermarked by
    record ``ts`` — already-shipped hops never re-ship. Returns how many were
    shipped; never raises (tracing must not kill a host)."""
    getter = getattr(h.responder, "traces", None)
    if getter is None:
        return 0
    try:
        new = [t for t in getter() if float(t.get("ts", 0)) > h.trace_watermark]
        if not new:
            return 0
        h.trace_watermark = max(float(t.get("ts", 0)) for t in new)
        iid = h.placement.get("instance_id")
        await bus.report_flow_traces(
            h.run_id, [{"participant": iid, "worker_id": host_id, **t} for t in new]
        )
        return len(new)
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] trace ship failed: %s", host_id, exc)
        return 0


async def run_flow_host(
    bus,
    *,
    host_id: str | None = None,
    advertise_host: str | None = None,
    stop_event: asyncio.Event | None = None,
    claim_timeout_s: float = 1.0,
) -> dict[str, Any]:
    """Serve placements until ``stop_event`` (or a worker drain) — returns a
    small summary for logs/tests."""
    host_id = host_id or f"fh-{uuid.uuid4().hex[:8]}"
    advertise_host = advertise_host or os.environ.get("ADVERTISE_HOST") or socket.gethostname()
    stop_event = stop_event or asyncio.Event()
    hosted: dict[str, _Hosted] = {}  # instance_id → hosted listener
    started_total = 0
    last_beat = 0.0
    started_at = time.time()

    def _presence() -> dict[str, Any]:
        return {
            "worker_id": host_id,
            "role": "flow_host",
            "host": advertise_host,
            "pid": os.getpid(),
            "status": "running",
            "started_at": started_at,
            "hosted": len(hosted),
            "runs": sorted({h.run_id for h in hosted.values()}),
        }

    async def _stop_instance(iid: str) -> None:
        h = hosted.pop(iid, None)
        if h is None:
            return
        try:
            await h.responder.stop()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            log.debug("[%s] stop of %s failed: %s", host_id, iid, exc)
        try:
            await bus.retract_instance(h.run_id, iid)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] retract of %s failed: %s", host_id, iid, exc)

    async def _heartbeat() -> None:
        await bus.worker_heartbeat(host_id, _presence())
        # fleet-wide capture gate (mirrors the orchestrator's global toggle)
        try:
            capture = await bus.get_flow_capture()
        except Exception:  # noqa: BLE001 — older bus without the primitive
            capture = None
        for h in list(hosted.values()):
            if capture is not None:
                h.responder._capture = capture  # type: ignore[attr-defined]
            await bus.advertise_instance(h.run_id, h.advertisement(host_id, advertise_host))
            await ship_traces(bus, host_id, h)

    await _heartbeat()
    try:
        while not stop_event.is_set():
            if await bus.is_worker_stopped(host_id):
                log.info("[%s] drained — stopping %d instance(s)", host_id, len(hosted))
                break

            placement = await bus.claim_placement(timeout_s=claim_timeout_s)
            if placement is not None:
                run_id = str(placement.get("run_id"))
                iid = str(placement.get("instance_id") or uuid.uuid4().hex[:8])
                # a stop raced the placement — don't bring up a zombie listener
                if await bus.is_network_stopped(run_id):
                    log.info("[%s] skipping %s — run %s already stopped", host_id, iid, run_id)
                elif any(
                    str(r.get("instance_id")) == iid and r.get("status") == "up"
                    for r in await bus.list_instances(run_id)
                ):
                    # re-placement raced a slow-but-alive host — the instance is
                    # already served; starting a second copy would duplicate it
                    # (or fail its fixed port). Drop the placement.
                    log.info("[%s] skipping %s — already advertised as up", host_id, iid)
                else:
                    try:
                        responder = _build_responder(placement)
                        # start with the network's capture posture (the fleet
                        # gate, when set, wins on the next heartbeat anyway)
                        responder._capture = bool(  # type: ignore[attr-defined]
                            placement.get("capture", False)
                        )
                        await responder.start()
                        h = _Hosted(run_id, placement, responder)
                        hosted[iid] = h
                        started_total += 1
                        await bus.advertise_instance(
                            run_id, h.advertisement(host_id, advertise_host)
                        )
                        log.info(
                            "[%s] hosting %s (%s) on :%s",
                            host_id,
                            iid,
                            placement.get("flow_id"),
                            getattr(responder, "port", "?"),
                        )
                    except Exception as exc:  # noqa: BLE001 — surface, don't die
                        log.warning("[%s] could not start %s: %s", host_id, iid, exc)
                        await bus.advertise_instance(
                            run_id,
                            {
                                "instance_id": iid,
                                "flow_id": placement.get("flow_id"),
                                "node_id": placement.get("node_id"),
                                "worker_id": host_id,
                                "host": advertise_host,
                                "port": None,
                                "status": "error",
                                "error": str(exc),
                            },
                        )

            # tear down any hosted run that was stopped network-wide
            for run_id in {h.run_id for h in hosted.values()}:
                if await bus.is_network_stopped(run_id):
                    for iid in [i for i, h in hosted.items() if h.run_id == run_id]:
                        await _stop_instance(iid)

            now = time.monotonic()
            if now - last_beat >= HOST_HEARTBEAT_S:
                last_beat = now
                await _heartbeat()
    finally:
        for iid in list(hosted):
            await _stop_instance(iid)
        try:
            await bus.drop_worker(host_id)
        except Exception as exc:  # noqa: BLE001 — best-effort deregister
            log.debug("[%s] deregister failed: %s", host_id, exc)
    return {"host_id": host_id, "started_total": started_total}


def _make_bus():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis.asyncio as aioredis

        return RedisLoadBus(aioredis.from_url(url, decode_responses=True))
    raise SystemExit("REDIS_URL is required for a standalone flow host")


async def _main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    await run_flow_host(_make_bus())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
