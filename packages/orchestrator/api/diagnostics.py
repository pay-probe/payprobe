"""Platform doctor — layered connectivity diagnostics ("why can't X reach Y?").

The platform has many moving parts (portal → orchestrator → scenario-service /
auth / Redis → workers → adapters → simulators or external hosts) and a
connection failure can hide in any hop. Existing tools each see one slice:
``/status`` probes sibling services, ``/connections/test`` probes one
connection, ``/runs/{id}/diagnose`` explains one finished run. This module runs
the whole stack through ONE layered check pass and cross-correlates the
results, so a single call answers "what exactly is broken, where, and what do
I do about it".

Layers (each check carries a ``layer``):

* ``services``     — core service-to-service links (scenario-service, auth,
                     Redis, optional MCP/assistant) plus the run store.
* ``connections``  — every saved connection, staged: config sanity → DNS
                     resolution → live adapter probe (connect + sign-on/health).
                     Environment overrides are applied first, so the doctor
                     tests what a run would actually dial.
* ``listeners``    — every running simulator / participant listener is probed
                     for a real accepting socket, and outbound connections that
                     dial localhost are cross-checked against bound ports
                     ("you're dialing :5010 but nothing listens there").
* ``runs``         — recent failed runs are scanned and their errors bucketed
                     with the shared taxonomy (report_service.diagnose); a
                     bucket that matches a connection failing *right now* is
                     linked to that check.

Every check is ``{id, layer, name, target, status, latency_ms, detail, error,
hint}`` where ``status`` ∈ ok|warn|fail|skip|disabled and ``hint`` is a
concrete next action, not a restatement of the error. Checks never raise —
a probe failure IS the result.

Dependency-injected via ``DiagContext`` so the engine is unit-testable without
a running platform; the orchestrator wires the real callables in ``main.py``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from report_service.diagnose import _CATEGORY_HELP, classify_error

LAYERS = ("services", "connections", "providers", "nats", "listeners", "runs")

#: cap concurrent live probes so a doctor pass can't stampede the platform
_MAX_CONCURRENCY = 8
#: per-check budget; a hung probe becomes a clean "timeout" fail
_CHECK_TIMEOUT_SEC = 8.0
#: how many recent runs the runs layer scans
_RUN_SCAN_LIMIT = 20
#: fields that mean "this adapter family dials host:port"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


# -- check record --------------------------------------------------------------

def _check(
    cid: str,
    layer: str,
    name: str,
    status: str,
    *,
    target: str = "",
    latency_ms: int | None = None,
    detail: str = "",
    error: str | None = None,
    hint: str = "",
) -> dict[str, Any]:
    return {
        "id": cid, "layer": layer, "name": name, "target": target,
        "status": status, "latency_ms": latency_ms, "detail": detail,
        "error": error, "hint": hint,
    }


# -- hints ---------------------------------------------------------------------

#: taxonomy category → what to actually do. More specific than the generic
#: run-diagnose help because here we know the failing hop precisely.
_PROBE_HINTS = {
    "unreachable": (
        "Nothing is accepting on that host/port. If it's a bundled simulator, "
        "start it (Simulators page); if external, verify host/port and that "
        "this machine can reach it (firewall, Docker network)."
    ),
    "timeout": (
        "The host resolved but never answered. Usually a firewall silently "
        "dropping packets, the wrong network (Docker service name vs "
        "localhost), or a hung target process."
    ),
    "auth": (
        "The target answered but rejected credentials. Check the API key / "
        "shared secret on the connection (or its environment override) and "
        "that it matches what the target expects."
    ),
    "tls": (
        "TLS handshake failed. Check the scheme (http vs https), certificate "
        "validity, and any client-cert requirement on the target."
    ),
    "binding": (
        "The connection references something that isn't configured. Open the "
        "connection in Manage → Connections and fill in the missing fields."
    ),
    "url": (
        "The connection's base_url is empty or missing http(s):// — set it to "
        "a full URL on the connection, or fill the variable it interpolates "
        "(e.g. a *_base_url global variable)."
    ),
    "unknown": "Inspect the raw error; it didn't match a known failure class.",
    "error": "Inspect the raw error; it didn't match a known failure class.",
    "assertion": "Inspect the raw error; the link works but responses mismatch.",
}

_HEALTH_CHECK_HINT = (
    "TCP connect worked but the protocol-level sign-on/health exchange failed. "
    "That's usually the wrong adapter family for this target, or a message "
    "format / dialect mismatch — not a network problem."
)

_SERVICE_HINTS = {
    "scenario-service": (
        "The orchestrator can't load scenarios, connections or environments "
        "without it. Check the scenario-service container/process and the "
        "orchestrator's SCENARIO_API_URL."
    ),
    "auth-service": (
        "Logins and service-to-service auth will fail. Check the auth-service "
        "container and AUTH_API_URL."
    ),
    "redis": (
        "Cross-replica run control, load coordination and the event backbone "
        "run through Redis. Check REDIS_URL and that Redis is up."
    ),
    "mcp-server": (
        "Only MCP clients are affected. Check MCP_API_URL and, on a 421, "
        "MCP_ALLOWED_HOSTS."
    ),
    "assistant": "Only the AI assistant is affected. Check ASSIST_API_URL.",
    "run-store": (
        "Run history can't be read or written. Check RUN_DB (path, permissions, "
        "disk)."
    ),
}


# -- context -------------------------------------------------------------------

@dataclass
class DiagContext:
    """Everything the engine needs, injected so tests can fake any layer."""

    #: async GET returning parsed JSON (auth header attached by the caller)
    http_get_json: Callable[[str], Awaitable[Any]]
    scenario_api_url: str
    auth_api_url: str
    redis_url: str | None
    mcp_api_url: str | None
    assist_api_url: str | None
    #: run registry with .list() / .get(run_id)
    run_store: Any
    #: live listeners right now: [{id, label, port, protocol, source}]
    listeners: Callable[[], list[dict]]
    #: live adapter probe: worker-shaped config -> {ok, latency_ms, error}
    probe_connection: Callable[[dict], Awaitable[dict]]
    #: connection doc + env name -> effective worker-shaped config
    connection_effective: Callable[[dict, str | None], dict]
    #: live trace-capture gate: () -> {"capturing": bool, "buffered": int}.
    #: Optional so test fakes and older callers keep working unchanged.
    capture_state: Callable[[], dict] | None = None
    #: NATS cluster health probe (ADR-0006): (servers, monitoring) -> the
    #: nats_admin.cluster_health dict. Optional — when absent the nats layer is
    #: skipped, so existing callers/tests keep working unchanged.
    nats_health: Callable[[list[str], list[str] | None], Awaitable[dict]] | None = None
    #: OAuth2 client-credentials token probe (ADR-0009): an authentication
    #: block -> {ok, error}. Used by the providers layer to answer "is a token
    #: obtainable?" for oauth2 provider connections (PayPal). Optional — when
    #: absent, oauth2 provider connections report config-completeness only.
    obtain_oauth_token: Callable[[dict], Awaitable[dict]] | None = None


# -- small probes ----------------------------------------------------------------

async def _timed(coro: Awaitable[Any]) -> tuple[Any, int]:
    start = time.monotonic()
    result = await coro
    return result, int((time.monotonic() - start) * 1000)


async def _tcp_accepts(host: str, port: int, timeout: float = 3.0) -> str | None:
    """None if a TCP connect to host:port succeeds, else the error string."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close errors don't matter
            pass
        return None
    except Exception as exc:  # noqa: BLE001 — the error IS the result
        return f"{type(exc).__name__}: {exc}"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _conn_endpoint(cfg: dict) -> tuple[str | None, int | None]:
    """Best-effort (host, port) a connection dials, across adapter families."""
    host, port = cfg.get("host"), cfg.get("port")
    if not host and cfg.get("base_url"):          # http family
        parsed = urlparse(str(cfg["base_url"]))
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host and cfg.get("target"):            # grpc "host:port"
        raw = str(cfg["target"])
        if ":" in raw:
            h, _, p = raw.rpartition(":")
            host, port = h, int(p) if p.isdigit() else None
    if str(cfg.get("adapter") or "").lower() == "nats":  # nats broker (ADR-0006)
        # A NATS connection's reachability is the broker's client port. Prefer an
        # explicit servers[] first URL; else host/port (default 4222). The TCP
        # accept probe on this endpoint verifies the cluster is reachable — deeper
        # JetStream / subject-claim diagnostics are deferred (documented).
        servers = cfg.get("servers")
        if servers:
            first = servers[0] if isinstance(servers, list) else servers
            parsed = urlparse(str(first) if "://" in str(first) else f"nats://{first}")
            host = parsed.hostname or host
            port = parsed.port or port or 4222
        else:
            host = host or "127.0.0.1"
            port = port or 4222
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        port = None
    return (str(host) if host else None), port


# -- services layer --------------------------------------------------------------

async def _service_checks(ctx: DiagContext) -> list[dict]:
    checks: list[dict] = []

    async def _http(name: str, base: str | None, path: str, optional: bool) -> None:
        if not base:
            checks.append(_check(
                f"svc.{name}", "services", name, "disabled",
                detail="not configured",
            ))
            return
        url = base.rstrip("/") + path
        try:
            _, ms = await _timed(asyncio.wait_for(
                ctx.http_get_json(url), timeout=_CHECK_TIMEOUT_SEC))
            checks.append(_check(
                f"svc.{name}", "services", name, "ok",
                target=url, latency_ms=ms,
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(
                f"svc.{name}", "services", name,
                "warn" if optional else "fail",
                target=url, error=f"{type(exc).__name__}: {exc}",
                hint=_SERVICE_HINTS.get(name, ""),
            ))

    async def _redis() -> None:
        if not ctx.redis_url:
            checks.append(_check(
                "svc.redis", "services", "redis", "disabled",
                detail="not configured (single-node mode)",
            ))
            return
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(ctx.redis_url, decode_responses=True)
            try:
                _, ms = await _timed(asyncio.wait_for(client.ping(), timeout=3))
                checks.append(_check(
                    "svc.redis", "services", "redis", "ok",
                    target=ctx.redis_url, latency_ms=ms,
                ))
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(
                "svc.redis", "services", "redis", "fail",
                target=ctx.redis_url, error=f"{type(exc).__name__}: {exc}",
                hint=_SERVICE_HINTS["redis"],
            ))

    def _store() -> None:
        try:
            ctx.run_store.list()
            checks.append(_check("svc.run-store", "services", "run-store", "ok"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(
                "svc.run-store", "services", "run-store", "fail",
                error=f"{type(exc).__name__}: {exc}",
                hint=_SERVICE_HINTS["run-store"],
            ))

    await asyncio.gather(
        _http("scenario-service", ctx.scenario_api_url, "/health", optional=False),
        _http("auth-service", ctx.auth_api_url, "/health", optional=False),
        # /healthz: the MCP /mcp route only answers POST (bare GET is a 405)
        _http("mcp-server", ctx.mcp_api_url, "/healthz", optional=True),
        _http("assistant", ctx.assist_api_url, "/health", optional=True),
        _redis(),
    )
    _store()
    return checks


# -- connections layer -------------------------------------------------------------

async def _one_connection(
    ctx: DiagContext, doc: dict, env_name: str | None, bound_ports: set[int]
) -> list[dict]:
    """Config sanity → DNS → live probe for one saved connection.

    Stages short-circuit: a missing host never reaches the DNS check, a DNS
    failure never reaches the dial — so each failure surfaces at the layer
    that actually owns it.
    """
    name = str(doc.get("name") or doc.get("id") or "?")
    checks: list[dict] = []

    if doc.get("disabled"):
        checks.append(_check(
            f"conn.{name}", "connections", name, "skip",
            detail="connection is disabled",
        ))
        return checks

    cfg = ctx.connection_effective(doc, env_name)
    host, port = _conn_endpoint(cfg)
    mode = str(doc.get("mode") or "outbound")
    adapter = str(cfg.get("adapter") or cfg.get("type") or "tcp")
    endpoint = f"{host}:{port}" if host and port else (host or "")

    # stage 1: config sanity — fail fast with a named missing field, so the
    # user gets "no host configured" instead of a cryptic dial error.
    if not host or not port:
        missing = "host/port" if adapter not in ("http",) else "base_url"
        checks.append(_check(
            f"conn.{name}.config", "connections", name, "fail",
            target=endpoint, detail=f"adapter={adapter} mode={mode}",
            error=f"connection has no {missing} configured"
            + (f" (environment '{env_name}')" if env_name else ""),
            hint=_PROBE_HINTS["binding"],
        ))
        return checks

    # NATS is broker-mediated (ADR-0006): a connection — inbound or outbound —
    # binds no local port; it always connects OUT to the broker. So the
    # inbound "is a local port accepting?" check doesn't apply — fall through to
    # the broker-reachability probe below. Whether a responder/flow is actually
    # subscribed to the subject is covered by the dedicated 'nats' layer.
    is_nats = adapter == "nats"

    # inbound connections don't dial anywhere — the platform should be
    # LISTENING on that port. Verify a local socket accepts.
    if mode == "inbound" and not is_nats:
        err = await _tcp_accepts("127.0.0.1", port)
        if err is None:
            checks.append(_check(
                f"conn.{name}.listen", "connections", name, "ok",
                target=f":{port}", detail=f"inbound {adapter}: port accepting",
            ))
        else:
            in_registry = port in bound_ports
            checks.append(_check(
                f"conn.{name}.listen", "connections", name, "fail",
                target=f":{port}", error=err,
                hint=(
                    "A listener claims this port but it isn't accepting — "
                    "restart the simulator / participant flow."
                    if in_registry else
                    "Inbound connection expects a local listener on this port "
                    "but nothing is bound. Start the simulator or participant "
                    "flow that should own it."
                ),
            ))
        return checks

    # stage 2: DNS — separated from the dial so "name doesn't resolve" (typo,
    # wrong Docker network) never masquerades as "connection refused".
    if not _is_ip_literal(host) and host != "localhost":
        loop = asyncio.get_running_loop()
        try:
            _, ms = await _timed(asyncio.wait_for(
                loop.getaddrinfo(host, port), timeout=4))
            checks.append(_check(
                f"conn.{name}.dns", "connections", name, "ok",
                target=host, latency_ms=ms, detail="hostname resolves",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(
                f"conn.{name}.dns", "connections", name, "fail",
                target=host, error=f"{type(exc).__name__}: {exc}",
                hint=(
                    "The hostname doesn't resolve from here. Check for a typo, "
                    "and whether it's a Docker service name being used outside "
                    "the compose network (or vice versa: 'localhost' inside a "
                    "container)."
                ),
            ))
            return checks  # dialing an unresolvable host adds no signal

    # stage 3: live probe — real adapter connect + sign-on/health.
    try:
        result = await asyncio.wait_for(
            ctx.probe_connection(cfg), timeout=_CHECK_TIMEOUT_SEC + 2)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "latency_ms": None,
                  "error": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        checks.append(_check(
            f"conn.{name}.probe", "connections", name, "ok",
            target=endpoint, latency_ms=result.get("latency_ms"),
            detail=f"{adapter}: connected + health check passed",
        ))
    else:
        err = result.get("error") or "probe failed"
        cat = classify_error(err)
        hint = _PROBE_HINTS.get(cat, "")
        if "health check failed" in err:
            hint = _HEALTH_CHECK_HINT
        elif cat == "unreachable" and host.lower() in _LOCAL_HOSTS \
                and port not in bound_ports:
            hint = (
                f"This dials localhost:{port} but no simulator/participant "
                "listener is bound there. Start the listener, or fix the port."
            )
        checks.append(_check(
            f"conn.{name}.probe", "connections", name, "fail",
            target=endpoint, latency_ms=result.get("latency_ms"),
            error=err, hint=hint,
        ))
    return checks


async def _connection_checks(
    ctx: DiagContext, env_name: str | None, only: str | None,
    bound_ports: set[int],
) -> list[dict]:
    try:
        docs = await ctx.http_get_json(
            ctx.scenario_api_url.rstrip("/") + "/connections")
    except Exception as exc:  # noqa: BLE001 — surfaced by the services layer too
        return [_check(
            "conn.registry", "connections", "connection registry", "fail",
            error=f"{type(exc).__name__}: {exc}",
            hint="Can't list connections while scenario-service is down.",
        )]
    all_docs = [d for d in docs or [] if isinstance(d, dict)]
    docs = all_docs
    if only:
        docs = [d for d in docs if str(d.get("name")) == only]
        if not docs:
            return [_check(
                f"conn.{only}", "connections", only, "fail",
                error=f"no saved connection named '{only}'",
            )]
    if not docs:
        return [_check(
            "conn.none", "connections", "connections", "skip",
            detail="no saved connections",
        )]

    # Provider connections (external real APIs, and MCP control-plane servers)
    # are owned by the dedicated 'providers' layer (ADR-0009): it does
    # credential-completeness + token-obtainable checks the generic reachability
    # probe here can't, and MCP stdio connections have no host/port for this
    # layer to dial. Defer them so they're diagnosed once, correctly.
    docs = [d for d in docs if not _is_provider_conn(d)]
    if not docs:
        return [_check(
            "conn.none", "connections", "connections", "skip",
            detail="only provider connections saved (see the providers layer)",
        )]

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _guarded(doc: dict) -> list[dict]:
        async with sem:
            return await _one_connection(ctx, doc, env_name, bound_ports)

    grouped = await asyncio.gather(*(_guarded(d) for d in docs))
    out = [c for group in grouped for c in group]

    # participant groups: members must reference existing, enabled connections
    # (a dead member silently shrinks the fleet at run time).
    try:
        groups = await ctx.http_get_json(
            ctx.scenario_api_url.rstrip("/") + "/participant-groups")
        # members must be checked against the FULL registry, not the scoped
        # subset — otherwise ?connection=X reports every other member "missing"
        by_name = {str(d.get("name")): d for d in all_docs}
        for g in groups or []:
            gname = str(g.get("name") or "?")
            bad = []
            for m in g.get("members") or []:
                cname = str(m.get("connection") or "")
                member = by_name.get(cname)
                if member is None:
                    bad.append(f"{cname} (missing)")
                elif member.get("disabled"):
                    bad.append(f"{cname} (disabled)")
            if bad:
                out.append(_check(
                    f"group.{gname}", "connections", f"group {gname}", "warn",
                    detail=f"{len(g.get('members') or [])} members",
                    error="unusable members: " + ", ".join(bad),
                    hint="Fix or remove these members — the worker will skip "
                         "them and the fleet is smaller than it looks.",
                ))
    except Exception:  # noqa: BLE001 — groups are optional signal
        pass
    return out


# -- providers layer (ADR-0009) ---------------------------------------------------

#: adapter families that are provider control-plane servers (never a wire hop)
_MCP_ADAPTERS = {"mcp"}


def _is_provider_conn(doc: dict) -> bool:
    """A provider connection: a real external API (``external: true`` — the
    load-guardrail marker) or an MCP control-plane server."""
    adapter = str(doc.get("adapter") or doc.get("type") or "").lower()
    return bool(doc.get("external")) or adapter in _MCP_ADAPTERS


def _provider_credential(cfg: dict) -> tuple[bool, str]:
    """(credential present?, human name of what's missing) for a provider
    connection's auth block. The provider packs ship these EMPTY on purpose —
    reporting "installed but not configured" is the single most useful provider
    diagnostic, and nothing else surfaces it (a live probe with an empty key
    just returns a generic 401)."""
    auth = cfg.get("authentication") or {}
    kind = str(auth.get("type") or "none")
    if kind == "bearer":
        return bool(str(auth.get("token") or "").strip()), "API key / bearer token"
    if kind == "header":
        return bool(str(auth.get("headerValue") or "").strip()), \
            f"{auth.get('headerName') or 'API key'} header value"
    if kind == "oauth2_client_credentials":
        ok = bool(str(auth.get("client_id") or "").strip()) and \
            bool(str(auth.get("client_secret") or "").strip())
        return ok, "OAuth2 client_id / client_secret"
    if kind == "basic":
        ok = bool(str(auth.get("username") or "").strip()) and \
            bool(str(auth.get("password") or "").strip())
        return ok, "basic username / password"
    # no auth block, or a scheme we don't gate on — nothing to check
    return True, ""


_PROVIDER_UNCONFIGURED_HINT = (
    "This provider connection was installed by a pack but has no credential set "
    "yet. Open it in Manage → Connections and fill in the key/secret (stored "
    "encrypted). Until then, scenarios and the playground can't reach the real "
    "provider — the offline provider simulator needs no credential."
)


async def _one_provider(ctx: DiagContext, doc: dict, env_name: str | None) -> list[dict]:
    name = str(doc.get("name") or doc.get("id") or "?")
    if doc.get("disabled"):
        return [_check(f"prov.{name}", "providers", name, "skip",
                       detail="connection is disabled")]

    cfg = ctx.connection_effective(doc, env_name)
    adapter = str(cfg.get("adapter") or cfg.get("type") or "").lower()
    is_mcp = adapter in _MCP_ADAPTERS
    transport = str(cfg.get("transport") or ("stdio" if cfg.get("command") else "http")).lower()
    auth = cfg.get("authentication") or {}
    kind = str(auth.get("type") or "none")
    label = f"{name} ({'MCP ' + transport if is_mcp else 'external'})"

    # A self-hosted (stdio) MCP server is a local subprocess the worker spawns
    # at run time — the doctor must NOT spawn provider processes, so report it
    # as manual rather than live-probing it.
    if is_mcp and transport == "stdio":
        cmd = " ".join([str(cfg.get("command") or "")] + [str(a) for a in cfg.get("args") or []])
        return [_check(
            f"prov.{name}", "providers", label, "skip",
            target=cmd.strip(),
            detail="self-hosted MCP (stdio) — started as a local subprocess at "
                   "run time",
            hint="Can't probe a stdio MCP server from here (it isn't running "
                 "yet). Verify the command runs locally and its key/token is set "
                 "(in the args/env), then use it from a scenario step.",
        )]

    # credential completeness (pure, no network) — short-circuit if missing
    present, missing = _provider_credential(cfg)
    if not present:
        return [_check(
            f"prov.{name}", "providers", label, "warn",
            detail=f"no {missing} configured",
            hint=_PROVIDER_UNCONFIGURED_HINT,
        )]

    # token-obtainable (oauth2): actually attempt the client-credentials
    # exchange — reachability alone doesn't prove a token comes back.
    if kind == "oauth2_client_credentials" and ctx.obtain_oauth_token is not None:
        try:
            res, ms = await _timed(asyncio.wait_for(
                ctx.obtain_oauth_token(auth), timeout=_CHECK_TIMEOUT_SEC))
        except Exception as exc:  # noqa: BLE001 — the failure IS the result
            res, ms = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, None
        if res.get("ok"):
            return [_check(
                f"prov.{name}", "providers", label, "ok", latency_ms=ms,
                target=str(auth.get("token_url") or ""),
                detail="OAuth2 token obtained")]
        err = res.get("error") or "token request failed"
        return [_check(
            f"prov.{name}", "providers", label, "fail", latency_ms=ms,
            target=str(auth.get("token_url") or ""), error=err,
            hint=_PROBE_HINTS.get(classify_error(err), "")
            or "The credential is set but no token came back — check the "
               "client_id/secret and token_url.")]

    # reachable + authorized (http provider APIs, remote MCP servers): reuse the
    # shared live probe (the http adapter's health_any_status treats a provider
    # API root's 401/404 as reachable; MCP does initialize + list_tools).
    try:
        result = await asyncio.wait_for(
            ctx.probe_connection(cfg), timeout=_CHECK_TIMEOUT_SEC + 2)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "latency_ms": None,
                  "error": f"{type(exc).__name__}: {exc}"}
    if result.get("ok"):
        return [_check(
            f"prov.{name}", "providers", label, "ok",
            latency_ms=result.get("latency_ms"),
            detail="MCP server reachable (list_tools ok)" if is_mcp
            else "provider reachable + credential accepted")]
    err = result.get("error") or "probe failed"
    return [_check(
        f"prov.{name}", "providers", label, "fail",
        latency_ms=result.get("latency_ms"), error=err,
        hint=_PROBE_HINTS.get(classify_error(err), ""))]


async def _provider_checks(
    ctx: DiagContext, env_name: str | None, only: str | None,
) -> list[dict]:
    try:
        docs = await ctx.http_get_json(
            ctx.scenario_api_url.rstrip("/") + "/connections")
    except Exception as exc:  # noqa: BLE001 — surfaced by the services layer too
        return [_check(
            "prov.registry", "providers", "provider registry", "fail",
            error=f"{type(exc).__name__}: {exc}",
            hint="Can't list connections while scenario-service is down.")]
    docs = [d for d in docs or [] if isinstance(d, dict) and _is_provider_conn(d)]
    if only:
        docs = [d for d in docs if str(d.get("name")) == only]
    if not docs:
        return [_check(
            "prov.none", "providers", "providers", "skip",
            detail="no provider connections (install a provider pack — Stripe / "
                   "Adyen / PayPal — to add them)")]

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _guarded(doc: dict) -> list[dict]:
        async with sem:
            return await _one_provider(ctx, doc, env_name)

    grouped = await asyncio.gather(*(_guarded(d) for d in docs))
    return [c for group in grouped for c in group]


# -- listeners layer ---------------------------------------------------------------

async def _listener_checks(ctx: DiagContext) -> list[dict]:
    checks: list[dict] = []
    listeners = ctx.listeners() or []
    if not listeners:
        return [_check(
            "listen.none", "listeners", "listeners", "skip",
            detail="no simulators or participant flows running",
        )]

    # Trace capture is OFF by default (buffer churn under load) — the single
    # most confusing empty-page moment in the platform. Surface the gate here
    # so "why is Network Trace empty?" is answered by the doctor, not a hunt.
    if ctx.capture_state is not None and any(
        e.get("source") == "participant" for e in listeners
    ):
        try:
            cap = ctx.capture_state() or {}
        except Exception as exc:  # noqa: BLE001 — the error IS the result
            cap = None
            checks.append(_check(
                "listen.capture", "listeners", "trace capture", "warn",
                error=f"{type(exc).__name__}: {exc}",
            ))
        if cap is not None:
            if cap.get("capturing"):
                checks.append(_check(
                    "listen.capture", "listeners", "trace capture", "ok",
                    detail=f"recording — {cap.get('buffered', 0)} hops buffered",
                ))
            else:
                checks.append(_check(
                    "listen.capture", "listeners", "trace capture", "warn",
                    detail="paused — Network Trace will stay empty",
                    hint="Trace capture is off (the default). Listeners keep "
                         "serving, but transactions are not being recorded — "
                         "resume from the Network Trace page, the map capture "
                         "chip, or POST /participants/capture.",
                ))

    async def _one(entry: dict) -> None:
        lid = str(entry.get("id") or "?")
        label = str(entry.get("label") or lid)
        port = entry.get("port")
        if not port:
            return
        err, ms = await _timed(_tcp_accepts("127.0.0.1", int(port)))
        if err is None:
            checks.append(_check(
                f"listen.{lid}", "listeners", label, "ok",
                target=f":{port}", latency_ms=ms,
                detail=f"{entry.get('protocol') or ''} accepting".strip(),
            ))
        else:
            checks.append(_check(
                f"listen.{lid}", "listeners", label, "fail",
                target=f":{port}", error=err,
                hint="The registry says this listener is running but its port "
                     "doesn't accept. Restart it (stop/start on the Simulators "
                     "page) — it likely crashed or failed to bind.",
            ))

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _guarded(entry: dict) -> None:
        async with sem:
            await _one(entry)

    await asyncio.gather(*(_guarded(e) for e in listeners))
    checks.sort(key=lambda c: c["id"])
    return checks


# -- runs layer ---------------------------------------------------------------------

def _run_checks(ctx: DiagContext, conn_checks: list[dict]) -> list[dict]:
    """Bucket recent failed-run errors and tie them to live connection state.

    Sync on purpose: the run store is local SQLite. Correlation is the point —
    "recent runs failed with 'unreachable' against visa AND visa's probe is
    failing right now" turns two mysteries into one answer.
    """
    try:
        recent = ctx.run_store.list()[:_RUN_SCAN_LIMIT]
    except Exception as exc:  # noqa: BLE001 — store failure already reported
        return [_check(
            "runs.scan", "runs", "recent runs", "skip",
            error=f"{type(exc).__name__}: {exc}",
        )]
    if not recent:
        return [_check("runs.none", "runs", "recent runs", "skip",
                       detail="no runs recorded yet")]

    failing_conns = {
        c["name"] for c in conn_checks
        if c["layer"] == "connections" and c["status"] == "fail"
    }
    # (category, target) -> {count, runs, example}
    buckets: dict[tuple[str, str], dict] = {}
    failed_runs = 0
    for row in recent:
        if row.get("status") not in ("failed", "error", "completed"):
            continue
        detail = ctx.run_store.get(row["id"]) or {}
        summary = detail.get("summary") or {}
        had_failure = False
        for sc in summary.get("scenarios") or []:
            for st in sc.get("steps") or []:
                if st.get("status") not in ("failed", "error"):
                    continue
                reason = st.get("error")
                cat = classify_error(reason)
                if cat in ("assertion",):   # functional, not connectivity
                    continue
                had_failure = True
                tgt = str(st.get("target") or "?")
                b = buckets.setdefault((cat, tgt), {
                    "count": 0, "runs": set(), "example": reason or "",
                })
                b["count"] += 1
                b["runs"].add(row["id"])
        if had_failure:
            failed_runs += 1

    if not buckets:
        return [_check(
            "runs.scan", "runs", "recent runs", "ok",
            detail=f"no connectivity-class failures in the last "
                   f"{len(recent)} runs",
        )]

    checks: list[dict] = []
    for (cat, tgt), b in sorted(buckets.items(), key=lambda kv: -kv[1]["count"]):
        cause, suggestion = _CATEGORY_HELP.get(cat, ("", ""))
        hint = suggestion
        if tgt in failing_conns:
            hint = (f"Connection '{tgt}' is failing its live probe right now "
                    f"(see the connections layer) — fix that and re-run.")
        c = _check(
            f"runs.{cat}.{tgt}", "runs", f"{tgt}: {cat}", "warn",
            target=tgt,
            detail=f"{b['count']} failing steps across "
                   f"{len(b['runs'])} of the last {len(recent)} runs",
            error=str(b["example"])[:300] or None,
            hint=hint or cause,
        )
        # the affected run ids (newest-ish first, capped) let the portal
        # deep-link straight to the run report / trace for the full error
        c["runs"] = sorted(b["runs"])[-5:]
        checks.append(c)
    return checks


# -- nats layer (ADR-0006) --------------------------------------------------------

def _nats_targets(conns: list[dict], servers_reg: list[dict]) -> list[dict]:
    """De-duplicated NATS targets to probe: each registry cluster, plus every
    NATS *connection* not already covered by a registry entry. Each target is
    ``{name, servers, monitoring}``."""
    targets: list[dict] = []
    seen: set[tuple] = set()

    def _add(name: str, servers: list[str], monitoring: list[str] | None) -> None:
        key = tuple(sorted(servers))
        if not servers or key in seen:
            return
        seen.add(key)
        targets.append({"name": name, "servers": servers, "monitoring": monitoring})

    for s in servers_reg or []:
        _add(f"cluster:{s.get('key') or s.get('name')}",
             list(s.get("servers") or []), list(s.get("monitoring") or []) or None)
    for c in conns or []:
        if str(c.get("adapter") or "").lower() != "nats":
            continue
        servers = list(c.get("servers") or [])
        if not servers and c.get("host"):
            host = str(c["host"])
            port = int(c.get("port") or 4222)
            servers = [host if "://" in host else f"nats://{host}:{port}"]
        _add(f"connection:{c.get('name')}", servers, None)
    return targets


async def _nats_checks(ctx: DiagContext) -> list[dict]:
    """Cluster reachable → JetStream enabled, per NATS connection / registry
    cluster. Skipped cleanly when no NATS health probe is wired."""
    if ctx.nats_health is None:
        return []
    try:
        conns = await ctx.http_get_json(f"{ctx.scenario_api_url}/connections")
    except Exception:  # noqa: BLE001 — surfaced by the services layer too
        conns = []
    try:
        servers_reg = await ctx.http_get_json(f"{ctx.scenario_api_url}/nats-servers")
    except Exception:  # noqa: BLE001 — registry optional
        servers_reg = []

    targets = _nats_targets(conns or [], servers_reg or [])
    if not targets:
        return [_check("nats.none", "nats", "NATS targets", "skip",
                       detail="no NATS connections or registered clusters")]

    checks: list[dict] = []
    for t in targets:
        cid = t["name"]
        try:
            health, ms = await _timed(ctx.nats_health(t["servers"], t["monitoring"]))
        except Exception as exc:  # noqa: BLE001 — a probe error IS the result
            checks.append(_check(
                f"nats.reach.{cid}", "nats", f"NATS reachable — {cid}", "fail",
                target=", ".join(t["servers"]), error=str(exc),
                hint="check the broker is running and the monitoring port (:8222) "
                     "is reachable from the orchestrator"))
            continue
        total, live = health.get("total", 0), health.get("live", 0)
        if live == 0:
            checks.append(_check(
                f"nats.reach.{cid}", "nats", f"NATS reachable — {cid}", "fail",
                target=", ".join(t["servers"]), latency_ms=ms,
                detail=f"0/{total} nodes healthy",
                hint="broker unreachable — verify the cluster is up and the "
                     "monitoring port is published"))
            continue
        status = "ok" if live == total else "warn"
        checks.append(_check(
            f"nats.reach.{cid}", "nats", f"NATS reachable — {cid}", status,
            target=", ".join(t["servers"]), latency_ms=ms,
            detail=f"{live}/{total} nodes healthy",
            hint="" if status == "ok" else
            "a node is down — queue-group / JetStream-replica failover should "
            "cover it, but investigate the dead node"))
        checks.append(_check(
            f"nats.jetstream.{cid}", "nats", f"JetStream enabled — {cid}",
            "ok" if health.get("jetstream") else "warn",
            target=", ".join(t["servers"]),
            detail="JetStream reported by at least one node"
            if health.get("jetstream") else "no node reports JetStream",
            hint="" if health.get("jetstream") else
            "start the broker with --jetstream if you need durable streams"))
    return checks


# -- engine -----------------------------------------------------------------------

async def run_diagnostics(
    ctx: DiagContext,
    layers: list[str] | None = None,
    connection: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Run the requested layers (all by default) and aggregate a verdict."""
    started = time.monotonic()
    want = [x for x in (layers or LAYERS) if x in LAYERS] or list(LAYERS)

    bound_ports: set[int] = set()
    try:
        bound_ports = {
            int(e["port"]) for e in (ctx.listeners() or []) if e.get("port")
        }
    except Exception:  # noqa: BLE001 — cross-check just degrades
        pass

    checks: list[dict] = []
    if "services" in want:
        checks += await _service_checks(ctx)
    conn_checks: list[dict] = []
    if "connections" in want:
        conn_checks = await _connection_checks(
            ctx, environment, connection, bound_ports)
        checks += conn_checks
    if "providers" in want:
        checks += await _provider_checks(ctx, environment, connection)
    if "nats" in want:
        checks += await _nats_checks(ctx)
    if "listeners" in want:
        checks += await _listener_checks(ctx)
    if "runs" in want:
        checks += _run_checks(ctx, conn_checks)

    summary = {s: 0 for s in ("ok", "warn", "fail", "skip", "disabled")}
    for c in checks:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    overall = ("failing" if summary["fail"]
               else "degraded" if summary["warn"] else "ok")
    return {
        "status": overall,
        "summary": summary,
        "layers": want,
        "environment": environment,
        "checks": checks,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
