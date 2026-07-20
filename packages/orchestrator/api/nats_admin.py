"""NATS cluster health + JetStream administration (ADR-0006).

The broker is external infrastructure PayProbe connects to; this module lets the
portal *observe* it (per-node health from the monitoring port) and *manage* its
JetStream streams/consumers (list / create / delete) without embedding the broker.

Everything here is written against injectable callables — an async HTTP fetch for
the monitoring endpoints, and the adapter's ``_nats_connect`` for the client — so
the orchestrator endpoints stay thin and the logic is unit-testable without a live
broker or ``nats-py``.

Monitoring: each node exposes an HTTP port (``:8222`` in our compose) with
``/healthz`` (liveness), ``/varz`` (server vars: version, uptime, connections,
mem) and ``/jsz`` (JetStream account: streams, consumers, messages, bytes). The
health view aggregates those per node.

JetStream admin uses the same lazy ``nats-py`` client the adapter uses. Deleting a
stream/consumer here is an explicit operator action (distinct from a *run's*
stop-ownership, which only ever removes what that run created).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

#: (status_code, parsed_json_or_None) for one monitoring GET. None json = the
#: endpoint answered but wasn't JSON (or a connection error, with status 0).
Fetch = Callable[[str], Awaitable[tuple[int, Any]]]
NatsConnect = Callable[..., Awaitable[Any]]

DEFAULT_MONITOR_PORT = 8222


def monitoring_bases(servers: list[str], monitoring: list[str] | None) -> list[str]:
    """The HTTP monitoring bases to probe. Prefer an explicit ``monitoring`` list;
    otherwise derive one per client URL (host + default monitoring port)."""
    if monitoring:
        return [m.rstrip("/") for m in monitoring]
    bases: list[str] = []
    for url in servers or []:
        parsed = urlparse(url if "://" in url else f"nats://{url}")
        host = parsed.hostname or "127.0.0.1"
        bases.append(f"http://{host}:{DEFAULT_MONITOR_PORT}")
    return bases


async def cluster_health(
    servers: list[str], monitoring: list[str] | None, fetch: Fetch
) -> dict:
    """Probe every node's monitoring port and aggregate a cluster view."""
    nodes: list[dict] = []
    for base in monitoring_bases(servers, monitoring):
        node: dict[str, Any] = {"monitor": base, "healthy": False}
        try:
            status, _ = await fetch(f"{base}/healthz")
            node["healthy"] = status == 200
        except Exception as exc:  # noqa: BLE001 — unreachable node stays unhealthy
            node["error"] = str(exc)
        try:
            _, varz = await fetch(f"{base}/varz")
            if isinstance(varz, dict):
                node["server_name"] = varz.get("server_name") or varz.get("name")
                node["version"] = varz.get("version")
                node["connections"] = varz.get("connections")
                node["uptime"] = varz.get("uptime")
                # /varz carries a "jetstream" key only when JS is enabled (the
                # value is a config dict — may be empty), so test presence.
                node["jetstream_enabled"] = "jetstream" in varz
                cluster = varz.get("cluster") or {}
                node["cluster_name"] = cluster.get("name") if isinstance(cluster, dict) else None
        except Exception:  # noqa: BLE001 — varz is best-effort enrichment
            pass
        try:
            _, jsz = await fetch(f"{base}/jsz")
            if isinstance(jsz, dict):
                node["jetstream"] = {
                    "streams": jsz.get("streams"),
                    "consumers": jsz.get("consumers"),
                    "messages": jsz.get("messages"),
                    "bytes": jsz.get("bytes"),
                }
        except Exception:  # noqa: BLE001
            pass
        nodes.append(node)
    live = sum(1 for n in nodes if n.get("healthy"))
    return {
        "servers": servers,
        "nodes": nodes,
        "total": len(nodes),
        "live": live,
        "healthy": len(nodes) > 0 and live == len(nodes),
        "jetstream": any(n.get("jetstream_enabled") for n in nodes),
    }


def _connect_opts(auth: dict | None) -> dict:
    auth = auth or {}
    opts: dict[str, Any] = {"connect_timeout": 5, "max_reconnect_attempts": 2}
    if auth.get("token"):
        opts["token"] = str(auth["token"])
    if auth.get("user"):
        opts["user"] = str(auth["user"])
    if auth.get("password"):
        opts["password"] = str(auth["password"])
    if auth.get("nkey_seed"):
        opts["nkeys_seed_str"] = str(auth["nkey_seed"])
    if auth.get("creds"):
        opts["user_credentials"] = str(auth["creds"])
    return opts


def _stream_view(info: Any) -> dict:
    """Normalize a nats-py StreamInfo (or a dict) into a JSON-friendly row."""
    cfg = getattr(info, "config", None)
    state = getattr(info, "state", None)
    if cfg is None and isinstance(info, dict):
        cfg = info.get("config")
        state = info.get("state")
    name = getattr(cfg, "name", None) if cfg is not None else None
    subjects = getattr(cfg, "subjects", None) if cfg is not None else None
    if isinstance(cfg, dict):
        name, subjects = cfg.get("name"), cfg.get("subjects")
    msgs = getattr(state, "messages", None) if state is not None else None
    byts = getattr(state, "bytes", None) if state is not None else None
    cons = getattr(state, "consumer_count", None) if state is not None else None
    if isinstance(state, dict):
        msgs, byts, cons = state.get("messages"), state.get("bytes"), state.get("consumer_count")
    return {"name": name, "subjects": list(subjects or []),
            "messages": msgs, "bytes": byts, "consumers": cons}


async def _js_for(servers: list[str], auth: dict | None, nats_connect: NatsConnect):
    nc = await nats_connect(servers, **_connect_opts(auth))
    return nc, nc.jetstream()


async def _drain(nc) -> None:
    try:
        await nc.drain()
    except Exception:  # noqa: BLE001
        try:
            await nc.close()
        except Exception:  # noqa: BLE001
            pass


async def _collect(result) -> list:
    """streams_info()/consumers_info() may return a coroutine (awaitable), a list
    or an async iterator — accept any."""
    import inspect

    if inspect.isawaitable(result):
        result = await result
    if hasattr(result, "__aiter__"):
        return [x async for x in result]
    return list(result or [])


async def list_streams(
    servers: list[str], auth: dict | None, nats_connect: NatsConnect
) -> dict:
    nc, js = await _js_for(servers, auth, nats_connect)
    try:
        infos = await _collect(js.streams_info())
        return {"streams": [_stream_view(i) for i in infos]}
    finally:
        await _drain(nc)


async def create_stream(
    servers: list[str], auth: dict | None, nats_connect: NatsConnect,
    *, name: str, subjects: list[str],
) -> dict:
    if not name or not subjects:
        raise ValueError("a stream needs a name and at least one subject")
    nc, js = await _js_for(servers, auth, nats_connect)
    try:
        info = await js.add_stream(name=name, subjects=list(subjects))
        return {"created": True, "stream": _stream_view(info) if info is not None
                else {"name": name, "subjects": list(subjects)}}
    finally:
        await _drain(nc)


async def delete_stream(
    servers: list[str], auth: dict | None, nats_connect: NatsConnect, *, name: str,
) -> dict:
    nc, js = await _js_for(servers, auth, nats_connect)
    try:
        await js.delete_stream(name)
        return {"deleted": True, "stream": name}
    finally:
        await _drain(nc)


async def list_consumers(
    servers: list[str], auth: dict | None, nats_connect: NatsConnect, *, stream: str,
) -> dict:
    nc, js = await _js_for(servers, auth, nats_connect)
    try:
        infos = await _collect(js.consumers_info(stream))
        rows = []
        for i in infos:
            name = getattr(i, "name", None)
            cfg = getattr(i, "config", None)
            durable = getattr(cfg, "durable_name", None) if cfg is not None else None
            if isinstance(i, dict):
                name = i.get("name")
                durable = (i.get("config") or {}).get("durable_name")
            rows.append({"name": name, "durable": durable})
        return {"stream": stream, "consumers": rows}
    finally:
        await _drain(nc)


async def delete_consumer(
    servers: list[str], auth: dict | None, nats_connect: NatsConnect,
    *, stream: str, durable: str,
) -> dict:
    nc, js = await _js_for(servers, auth, nats_connect)
    try:
        await js.delete_consumer(stream, durable)
        return {"deleted": True, "stream": stream, "consumer": durable}
    finally:
        await _drain(nc)
