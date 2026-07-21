"""Platform doctor (api/diagnostics.py): layered checks, hints, correlation.

The engine is exercised through a fake DiagContext — no live services needed.
The listeners layer gets a real asyncio TCP server so "port accepts" is tested
against an actual socket, not a mock.
"""
import asyncio
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api.diagnostics import (  # noqa: E402
    DiagContext,
    run_diagnostics,
)
from orchestrator.api.main import _connection_effective  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402


# -- fakes ---------------------------------------------------------------------

class FakeHttp:
    """URL-suffix-keyed fake for ctx.http_get_json."""

    def __init__(self, routes: dict):
        self.routes = routes

    async def __call__(self, url: str):
        for suffix, result in self.routes.items():
            if url.endswith(suffix):
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected URL {url}")


def make_ctx(
    *,
    routes: dict | None = None,
    listeners: list[dict] | None = None,
    probe_results: dict | None = None,
    run_store: RunStore | None = None,
    capture_state: dict | None = None,
    nats_health=None,
    obtain_oauth_token=None,
) -> DiagContext:
    probes = probe_results or {}

    async def _probe(cfg: dict) -> dict:
        # keyed by host:port — the effective config has metadata (name, …)
        # stripped by _connection_effective, so the name isn't available here
        key = f"{cfg.get('host')}:{cfg.get('port')}"
        return probes.get(key, {"ok": True, "latency_ms": 3, "error": None})

    return DiagContext(
        http_get_json=FakeHttp(routes if routes is not None else {
            "/health": {"status": "ok"},
            "/connections": [],
            "/participant-groups": [],
        }),
        scenario_api_url="http://scenario:8000",
        auth_api_url="http://auth:8300",
        redis_url=None,
        mcp_api_url=None,
        assist_api_url=None,
        run_store=run_store or RunStore(":memory:"),
        listeners=lambda: listeners or [],
        probe_connection=_probe,
        connection_effective=_connection_effective,
        capture_state=(lambda: capture_state) if capture_state is not None else None,
        nats_health=nats_health,
        obtain_oauth_token=obtain_oauth_token,
    )


def by_id(report: dict) -> dict:
    return {c["id"]: c for c in report["checks"]}


# -- services layer --------------------------------------------------------------

async def test_all_healthy_overall_ok():
    report = await run_diagnostics(make_ctx())
    checks = by_id(report)
    assert checks["svc.scenario-service"]["status"] == "ok"
    assert checks["svc.auth-service"]["status"] == "ok"
    assert checks["svc.redis"]["status"] == "disabled"  # single-node mode
    assert checks["svc.mcp-server"]["status"] == "disabled"
    assert report["status"] == "ok"


async def test_core_service_down_fails_with_hint():
    ctx = make_ctx(routes={
        "scenario:8000/health": ConnectionError("refused"),
        "auth:8300/health": {"status": "ok"},
        "/connections": ConnectionError("refused"),
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["services", "connections"])
    checks = by_id(report)
    assert checks["svc.scenario-service"]["status"] == "fail"
    assert "SCENARIO_API_URL" in checks["svc.scenario-service"]["hint"]
    # the connections layer degrades cleanly when the registry is unreachable
    assert checks["conn.registry"]["status"] == "fail"
    assert report["status"] == "failing"


# -- connections layer -------------------------------------------------------------

async def test_connection_missing_host_fails_config_stage():
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [{"name": "visa", "adapter": "tcp_iso8583"}],
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["connections"])
    checks = by_id(report)
    assert checks["conn.visa.config"]["status"] == "fail"
    assert "no host/port" in checks["conn.visa.config"]["error"]
    # config failure short-circuits: no DNS / probe checks for this connection
    assert "conn.visa.dns" not in checks
    assert "conn.visa.probe" not in checks


async def test_disabled_connection_is_skipped():
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            {"name": "old", "adapter": "tcp", "host": "1.2.3.4",
             "port": 5000, "disabled": True},
        ],
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["connections"])
    assert by_id(report)["conn.old"]["status"] == "skip"


async def test_dns_failure_short_circuits_probe():
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            # .invalid is reserved (RFC 2606): resolution always fails
            {"name": "ext", "adapter": "tcp", "host": "no-such.invalid",
             "port": 5000},
        ],
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["connections"])
    checks = by_id(report)
    assert checks["conn.ext.dns"]["status"] == "fail"
    assert "resolve" in checks["conn.ext.dns"]["hint"]
    assert "conn.ext.probe" not in checks


async def test_probe_failure_carries_localhost_listener_hint():
    ctx = make_ctx(
        routes={
            "/health": {"status": "ok"},
            "/connections": [
                {"name": "sim", "adapter": "tcp_iso8583",
                 "host": "127.0.0.1", "port": 59999},
            ],
            "/participant-groups": [],
        },
        probe_results={"127.0.0.1:59999": {
            "ok": False, "latency_ms": 1,
            "error": "ConnectionRefusedError: [Errno 111] refused",
        }},
        listeners=[],  # nothing bound on 59999
    )
    report = await run_diagnostics(ctx, layers=["connections"])
    probe = by_id(report)["conn.sim.probe"]
    assert probe["status"] == "fail"
    assert "no simulator/participant listener" in probe["hint"]


async def test_health_check_failure_hints_protocol_not_network():
    ctx = make_ctx(
        routes={
            "/health": {"status": "ok"},
            "/connections": [
                {"name": "hsm", "adapter": "payshield",
                 "host": "127.0.0.1", "port": 1500},
            ],
            "/participant-groups": [],
        },
        probe_results={"127.0.0.1:1500": {
            "ok": False, "latency_ms": 12,
            "error": "connected, but health check failed",
        }},
    )
    report = await run_diagnostics(ctx, layers=["connections"])
    assert "dialect mismatch" in by_id(report)["conn.hsm.probe"]["hint"]


async def test_environment_override_changes_probed_endpoint():
    seen: list[dict] = []

    async def _probe(cfg: dict) -> dict:
        seen.append(cfg)
        return {"ok": True, "latency_ms": 1, "error": None}

    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [{
            "name": "visa", "adapter": "tcp_iso8583",
            "host": "127.0.0.1", "port": 5000,
            "environment_overrides": {"uat": {"port": 6000}},
        }],
        "/participant-groups": [],
    })
    ctx.probe_connection = _probe
    await run_diagnostics(ctx, layers=["connections"], environment="uat")
    assert seen and seen[0]["port"] == 6000


async def test_group_with_missing_and_disabled_members_warns():
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            {"name": "a", "adapter": "tcp", "host": "127.0.0.1", "port": 5001},
            {"name": "b", "adapter": "tcp", "host": "127.0.0.1", "port": 5002,
             "disabled": True},
        ],
        "/participant-groups": [{
            "name": "fleet",
            "members": [{"connection": "a"}, {"connection": "b"},
                        {"connection": "ghost"}],
        }],
    })
    report = await run_diagnostics(ctx, layers=["connections"])
    g = by_id(report)["group.fleet"]
    assert g["status"] == "warn"
    assert "ghost (missing)" in g["error"]
    assert "b (disabled)" in g["error"]


async def test_connection_scope_filters_to_one():
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            {"name": "a", "adapter": "tcp", "host": "127.0.0.1", "port": 5001},
            {"name": "b", "adapter": "tcp", "host": "127.0.0.1", "port": 5002},
        ],
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["connections"], connection="a")
    names = {c["name"] for c in report["checks"] if c["layer"] == "connections"}
    assert names == {"a"}


async def test_scoped_run_checks_group_members_against_full_registry():
    """Regression: ?connection=X must not report other members "missing" —
    the group cross-check uses the full registry, not the scoped subset."""
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            {"name": "a", "adapter": "tcp", "host": "127.0.0.1", "port": 5001},
            {"name": "b", "adapter": "tcp", "host": "127.0.0.1", "port": 5002},
        ],
        "/participant-groups": [{
            "name": "fleet",
            "members": [{"connection": "a"}, {"connection": "b"}],
        }],
    })
    report = await run_diagnostics(ctx, layers=["connections"], connection="a")
    assert "group.fleet" not in by_id(report)  # healthy group → no warn


# -- listeners layer ---------------------------------------------------------------

async def test_listener_bound_vs_dead():
    server = await asyncio.start_server(
        lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ctx = make_ctx(listeners=[
            {"id": "s1", "label": "live sim", "port": port,
             "protocol": "iso8583", "source": "simulator"},
            {"id": "s2", "label": "dead sim", "port": 1,  # nothing binds :1
             "protocol": "iso8583", "source": "simulator"},
        ])
        report = await run_diagnostics(ctx, layers=["listeners"])
        checks = by_id(report)
        assert checks["listen.s1"]["status"] == "ok"
        assert checks["listen.s2"]["status"] == "fail"
        assert "Restart it" in checks["listen.s2"]["hint"]
    finally:
        server.close()
        await server.wait_closed()


async def test_capture_off_warns_when_participants_running():
    """The #1 confused moment (empty Network Trace because capture is OFF by
    default) must surface in the doctor when participant flows are live."""
    server = await asyncio.start_server(
        lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        listeners = [{"id": "p1", "label": "issuer", "port": port,
                      "protocol": "iso8583", "source": "participant"}]
        off = await run_diagnostics(
            make_ctx(listeners=listeners,
                     capture_state={"capturing": False, "buffered": 0}),
            layers=["listeners"])
        cap = by_id(off)["listen.capture"]
        assert cap["status"] == "warn"
        assert "capture is off" in cap["hint"]

        on = await run_diagnostics(
            make_ctx(listeners=listeners,
                     capture_state={"capturing": True, "buffered": 7}),
            layers=["listeners"])
        assert by_id(on)["listen.capture"]["status"] == "ok"

        # simulators only → the participant gate is irrelevant, no check
        sims = [{**listeners[0], "source": "simulator"}]
        none = await run_diagnostics(
            make_ctx(listeners=sims,
                     capture_state={"capturing": False, "buffered": 0}),
            layers=["listeners"])
        assert "listen.capture" not in by_id(none)
    finally:
        server.close()
        await server.wait_closed()


async def test_inbound_connection_checks_local_listener():
    server = await asyncio.start_server(
        lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ctx = make_ctx(routes={
            "/health": {"status": "ok"},
            "/connections": [
                {"name": "in-ok", "adapter": "tcp", "mode": "inbound",
                 "host": "0.0.0.0", "port": port},
                {"name": "in-dead", "adapter": "tcp", "mode": "inbound",
                 "host": "0.0.0.0", "port": 1},
            ],
            "/participant-groups": [],
        })
        report = await run_diagnostics(ctx, layers=["connections"])
        checks = by_id(report)
        assert checks["conn.in-ok.listen"]["status"] == "ok"
        assert checks["conn.in-dead.listen"]["status"] == "fail"
        assert "nothing is bound" in checks["conn.in-dead.listen"]["hint"]
    finally:
        server.close()
        await server.wait_closed()


async def test_inbound_nats_connection_probes_broker_not_local_port():
    """An inbound NATS connection is port-less (ADR-0006) — the doctor must NOT
    do the local port-accept check (which would falsely 'refuse'); it probes
    broker reachability instead."""
    ctx = make_ctx(routes={
        "/health": {"status": "ok"},
        "/connections": [
            {"name": "nats-in", "adapter": "nats", "mode": "inbound",
             "host": "127.0.0.1", "port": 4222, "subjects": ["pay.auth"]},
        ],
        "/participant-groups": [],
    })
    report = await run_diagnostics(ctx, layers=["connections"])
    checks = by_id(report)
    assert "conn.nats-in.listen" not in checks     # no false port-accept check
    assert checks["conn.nats-in.probe"]["status"] == "ok"  # broker reachability


# -- runs layer ---------------------------------------------------------------------

def _store_with_failures() -> RunStore:
    store = RunStore(":memory:")
    store.create("r1", "test", 1)
    store.finish("r1", "failed", {"scenarios": [{
        "name": "auth", "steps": [
            {"step_id": "s1", "target": "visa", "action": "send_0200",
             "status": "error",
             "error": "ConnectionRefusedError: [Errno 111] refused"},
        ]}]})
    return store


async def test_run_scan_correlates_with_failing_connection():
    ctx = make_ctx(
        routes={
            "/health": {"status": "ok"},
            "/connections": [
                {"name": "visa", "adapter": "tcp_iso8583",
                 "host": "127.0.0.1", "port": 5010},
            ],
            "/participant-groups": [],
        },
        probe_results={"127.0.0.1:5010": {
            "ok": False, "latency_ms": 2,
            "error": "ConnectionRefusedError: refused"}},
        run_store=_store_with_failures(),
    )
    report = await run_diagnostics(ctx, layers=["connections", "runs"])
    checks = by_id(report)
    bucket = checks["runs.unreachable.visa"]
    assert bucket["status"] == "warn"
    # the run failure is tied to the connection failing its live probe NOW
    assert "failing its live probe right now" in bucket["hint"]
    # affected run ids ride along so the portal can deep-link to the trace
    assert bucket["runs"] == ["r1"]
    assert report["status"] == "failing"


async def test_unresolved_reference_gets_authoring_hint_and_run_ids():
    store = RunStore(":memory:")
    store.create("rX", "test", 1)
    store.finish("rX", "failed", {"scenarios": [{
        "name": "hsm flow", "steps": [
            {"step_id": "s2", "target": "cn_listener_hsm_9001",
             "action": "send", "status": "error",
             "error": "unresolved reference: 'gen_cvk.response.key'"},
        ]}]})
    ctx = make_ctx(run_store=store)
    report = await run_diagnostics(ctx, layers=["runs"])
    c = by_id(report)["runs.reference.cn_listener_hsm_9001"]
    assert c["status"] == "warn"
    assert "Trace tab" in c["hint"]           # authoring hint, not network
    assert "referenced" in c["hint"]
    assert c["runs"] == ["rX"]


async def test_run_scan_clean_when_only_assertions_failed():
    store = RunStore(":memory:")
    store.create("r1", "test", 1)
    store.finish("r1", "failed", {"scenarios": [{
        "name": "auth", "steps": [
            {"step_id": "s1", "target": "visa", "action": "send_0200",
             "status": "failed",
             "error": "assertion de39 equals '00' (got '05')"},
        ]}]})
    ctx = make_ctx(run_store=store)
    report = await run_diagnostics(ctx, layers=["runs"])
    scan = by_id(report)["runs.scan"]
    assert scan["status"] == "ok"  # functional failures aren't connectivity


# -- aggregation --------------------------------------------------------------------

async def test_layer_scoping_and_summary_counts():
    report = await run_diagnostics(make_ctx(), layers=["services"])
    assert report["layers"] == ["services"]
    assert all(c["layer"] == "services" for c in report["checks"])
    total = sum(report["summary"].values())
    assert total == len(report["checks"])


async def test_unknown_layer_falls_back_to_all():
    report = await run_diagnostics(make_ctx(), layers=["bogus"])
    assert report["layers"] == [
        "services", "connections", "providers", "nats", "listeners", "runs"]


# -- nats layer (ADR-0006) ------------------------------------------------------

def _nats_routes(conns, servers_reg=None):
    return {
        "/health": {"status": "ok"},
        "/connections": conns,
        "/nats-servers": servers_reg or [],
        "/participant-groups": [],
    }


async def test_nats_layer_skipped_without_probe():
    """No nats_health wired -> the layer produces nothing (back-compat)."""
    report = await run_diagnostics(make_ctx(), layers=["nats"])
    assert report["checks"] == []


async def test_nats_layer_healthy_cluster_from_connection():
    async def health(servers, monitoring):
        return {"total": 3, "live": 3, "healthy": True, "jetstream": True,
                "nodes": []}

    conns = [{"name": "bus", "adapter": "nats", "host": "nats-1", "port": 4222}]
    report = await run_diagnostics(
        make_ctx(routes=_nats_routes(conns), nats_health=health), layers=["nats"])
    checks = by_id(report)
    assert checks["nats.reach.connection:bus"]["status"] == "ok"
    assert checks["nats.jetstream.connection:bus"]["status"] == "ok"


async def test_nats_layer_flags_dead_node_and_no_jetstream():
    async def health(servers, monitoring):
        return {"total": 3, "live": 2, "healthy": False, "jetstream": False,
                "nodes": []}

    servers_reg = [{"key": "prod", "name": "prod",
                    "servers": ["nats://n1:4222", "nats://n2:4223"],
                    "monitoring": ["http://n1:8222"]}]
    report = await run_diagnostics(
        make_ctx(routes=_nats_routes([], servers_reg), nats_health=health),
        layers=["nats"])
    checks = by_id(report)
    assert checks["nats.reach.cluster:prod"]["status"] == "warn"   # 2/3 up
    assert checks["nats.jetstream.cluster:prod"]["status"] == "warn"


async def test_nats_layer_unreachable_is_fail():
    async def health(servers, monitoring):
        return {"total": 3, "live": 0, "healthy": False, "jetstream": False,
                "nodes": []}

    conns = [{"name": "bus", "adapter": "nats", "servers": ["nats://x:4222"]}]
    report = await run_diagnostics(
        make_ctx(routes=_nats_routes(conns), nats_health=health), layers=["nats"])
    assert by_id(report)["nats.reach.connection:bus"]["status"] == "fail"


async def test_nats_layer_no_targets_skips():
    report = await run_diagnostics(
        make_ctx(routes=_nats_routes([]), nats_health=lambda *a: None),
        layers=["nats"])
    assert by_id(report)["nats.none"]["status"] == "skip"


# -- providers layer (ADR-0009) ---------------------------------------------------

def _prov_routes(conns):
    return {"/health": {"status": "ok"}, "/connections": conns,
            "/participant-groups": []}


def _stripe_sandbox(token=""):
    return {"name": "stripe_sandbox", "adapter": "http", "external": True,
            "host": "api.stripe.com", "port": 443,
            "base_url": "https://api.stripe.com/v1",
            "authentication": {"type": "bearer", "token": token}}


def _paypal_sandbox(cid="", secret=""):
    return {"name": "paypal_sandbox", "adapter": "http", "external": True,
            "base_url": "https://api-m.sandbox.paypal.com",
            "authentication": {"type": "oauth2_client_credentials",
                               "client_id": cid, "client_secret": secret,
                               "token_url": "https://api-m.sandbox.paypal.com/v1/oauth2/token"}}


async def test_provider_unconfigured_credential_warns():
    # freshly-installed pack: empty key -> a helpful warn, no live probe
    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([_stripe_sandbox("")])), layers=["providers"])
    c = by_id(report)["prov.stripe_sandbox"]
    assert c["status"] == "warn"
    assert "no API key" in c["detail"]
    assert "encrypted" in c["hint"]


async def test_provider_configured_reachable_ok():
    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([_stripe_sandbox("sk_test_live")]),
                 probe_results={"api.stripe.com:443": {"ok": True, "latency_ms": 12}}),
        layers=["providers"])
    c = by_id(report)["prov.stripe_sandbox"]
    assert c["status"] == "ok"
    assert "credential accepted" in c["detail"]


async def test_provider_oauth_token_obtainable():
    async def token(auth):
        assert auth["client_id"] == "id1"
        return {"ok": True}

    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([_paypal_sandbox("id1", "shh")]),
                 obtain_oauth_token=token),
        layers=["providers"])
    c = by_id(report)["prov.paypal_sandbox"]
    assert c["status"] == "ok"
    assert "token obtained" in c["detail"]


async def test_provider_oauth_token_failure_fails():
    async def token(auth):
        return {"ok": False, "error": "HTTP 401"}

    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([_paypal_sandbox("id1", "bad")]),
                 obtain_oauth_token=token),
        layers=["providers"])
    c = by_id(report)["prov.paypal_sandbox"]
    assert c["status"] == "fail"
    assert "401" in c["error"]


async def test_provider_stdio_mcp_reported_manual_not_probed():
    probed = []

    async def _probe(cfg):
        probed.append(cfg)
        return {"ok": True}

    ctx = make_ctx(routes=_prov_routes([{
        "name": "adyen_mcp", "adapter": "mcp", "transport": "stdio",
        "external": True, "command": "npx",
        "args": ["-y", "@adyen/mcp", "--adyenApiKey=", "--env=TEST"]}]))
    ctx.probe_connection = _probe
    report = await run_diagnostics(ctx, layers=["providers"])
    c = by_id(report)["prov.adyen_mcp"]
    assert c["status"] == "skip"
    assert "stdio" in c["detail"]
    assert probed == []  # never spawns the subprocess


async def test_provider_remote_mcp_probed_reachable():
    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([{
            "name": "stripe_mcp", "adapter": "mcp", "transport": "http",
            "external": True, "base_url": "https://mcp.stripe.com",
            "authentication": {"type": "bearer", "token": "sk_test_x"}}]),
            probe_results={"None:None": {"ok": True, "latency_ms": 20}}),
        layers=["providers"])
    c = by_id(report)["prov.stripe_mcp"]
    assert c["status"] == "ok"
    assert "MCP server reachable" in c["detail"]


async def test_provider_connections_excluded_from_connections_layer():
    # a provider connection must be diagnosed once (providers), not double-probed
    conns = [_stripe_sandbox("sk_test_x"),
             {"name": "local_sim", "adapter": "http", "host": "127.0.0.1",
              "port": 8085, "base_url": "http://127.0.0.1:8085/v1"}]
    report = await run_diagnostics(
        make_ctx(routes=_prov_routes(conns),
                 probe_results={"127.0.0.1:8085": {"ok": True, "latency_ms": 1},
                                "api.stripe.com:443": {"ok": True}}),
        layers=["connections", "providers"])
    ids = by_id(report)
    # stripe_sandbox appears only under the providers layer
    assert ids["prov.stripe_sandbox"]["status"] == "ok"
    assert not any(cid.startswith("conn.stripe_sandbox") for cid in ids)
    # the local sim connection is still handled by the connections layer
    assert ids["conn.local_sim.probe"]["status"] == "ok"


async def test_provider_layer_no_providers_skips():
    report = await run_diagnostics(
        make_ctx(routes=_prov_routes([
            {"name": "local_sim", "adapter": "http", "host": "127.0.0.1",
             "port": 8085, "base_url": "http://127.0.0.1:8085"}])),
        layers=["providers"])
    assert by_id(report)["prov.none"]["status"] == "skip"
