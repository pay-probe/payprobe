"""AdapterRegistry — resolves target name to adapter instance."""

import logging
from .base.base_adapter import BaseAdapter
from .mock.adapter import MockAdapter

log = logging.getLogger(__name__)

# Populated at import time; real adapters imported lazily to keep deps optional
ADAPTER_MAP: dict[str, type[BaseAdapter]] = {
    "mock": MockAdapter,
}


def _register_real_adapters():
    """Import real adapters only if their deps are installed.

    A missing optional dependency is expected and fine — but we log it at debug
    so a *broken* adapter module (an ImportError from inside the adapter itself)
    is distinguishable from "dep not installed" instead of silently vanishing
    and resurfacing later as a confusing "Unknown adapter" error.
    """

    def _try(register, *names: str) -> None:
        try:
            register()
        except ImportError as exc:
            log.debug("adapter(s) %s unavailable, skipping: %s", ", ".join(names), exc)

    def _http():
        # Generic REST/HTTP connection adapter (base_url + named actions).
        from .http.adapter import HttpAdapter

        ADAPTER_MAP["http"] = HttpAdapter

    def _hsm():
        # payShield host-command *client* (named actions: generate_cvv, verify_pin,
        # generate_mac, …). Registered under distinct keys so it doesn't collide
        # with the generic header-echo TCP adapter that also answers to "hsm".
        from .hsm.adapter import HSMAdapter

        ADAPTER_MAP["hsm_client"] = HSMAdapter
        ADAPTER_MAP["payshield"] = HSMAdapter

    def _tcp():
        # Universal persistent TCP adapter. One long-lived socket multiplexes
        # many messages; the wire protocol is pluggable (see tcp/protocols.py),
        # so the same class backs ISO 8583 hosts (switch/acquirer) AND
        # header-echo host-command links (HSM) — config picks the protocol.
        from .tcp.adapter import TcpAdapter

        for key in (
            "tcp",
            "tcp_iso8583",
            "switch",
            "switch_iso8583",
            "acquirer",
            "acquirer_host",
            "hsm",
            "hsm_tcp",
        ):
            ADAPTER_MAP[key] = TcpAdapter

    def _grpc():
        # General gRPC adapter (descriptor-driven; unary + streaming). Imports
        # cleanly without grpcio — grpc is only imported at connect().
        from .grpc.adapter import GrpcAdapter

        ADAPTER_MAP["grpc"] = GrpcAdapter

    def _nats():
        # Broker-mediated NATS client (ADR-0006): publish / request / js_publish.
        # Imports cleanly without nats-py — nats is only imported at connect().
        from .nats.adapter import NatsAdapter

        ADAPTER_MAP["nats"] = NatsAdapter

    def _db_probe():
        from .db_probe.adapter import DBProbeAdapter

        ADAPTER_MAP["db_probe_core"] = DBProbeAdapter
        ADAPTER_MAP["db_probe_switch"] = DBProbeAdapter

    def _insight():
        # The scenario `predict` step: mid-flow model inference against the
        # advisory insight service (ADR-0005). Stdlib-only.
        from .insight.adapter import InsightAdapter

        ADAPTER_MAP["insight"] = InsightAdapter

    _try(_http, "http")
    _try(_hsm, "hsm")
    _try(_tcp, "tcp", "switch", "acquirer", "hsm_tcp")
    _try(_grpc, "grpc")
    _try(_nats, "nats")
    _try(_db_probe, "db_probe_core", "db_probe_switch")
    _try(_insight, "insight")


_register_real_adapters()

# Self-report the registered adapter set at import time. This is the FIRST thing
# to check when a run errors with "Unknown adapter 'X'" — grep any process's logs
# (`docker logs <container> | grep 'adapters registered'`) and a stale build is
# obvious: it will be missing the newer adapters (nats, insight, …). Cheap, runs
# once per process, and turns a mid-run failure into a boot-time signal.
log.info("adapters registered: %s", sorted(ADAPTER_MAP))


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto a copy of ``base`` (dicts only)."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class AdapterRegistry:
    """Resolves a target *instance name* to a configured adapter instance.

    An environment may declare any number of named instances under ``adapters``
    — several switches, a primary and backup HSM, etc. Each instance picks its
    implementation with an ``adapter`` (alias ``type``) key, so the instance
    name is decoupled from the adapter class::

        "adapters": {
          "switch_visa": {"adapter": "tcp", "protocol": "iso8583",
                          "host": "10.0.1.50", "port": 7001},
          "switch_mc":   {"adapter": "tcp", "protocol": "iso8583",
                          "host": "10.0.1.50", "port": 7002},
          "hsm_primary": {"adapter": "tcp", "protocol": "header_echo",
                          "host": "10.0.2.10", "port": 1500},
          "hsm_backup":  {"extends": "hsm_primary", "host": "10.0.2.11"}
        }

    Shared config is factored out two ways: per-instance ``extends`` inherits
    another instance's config, and the env-level ``adapter_defaults`` block
    (keyed by implementation name) underlays every instance of that impl. Both
    are deep-merged, with instance config taking precedence.

    When ``adapter``/``type`` is omitted the instance name itself is used as the
    implementation key (backwards compatible: ``"switch"`` -> the ``switch``
    impl).
    """

    def __init__(self, env_config: dict):
        self.env_config = env_config
        self.budget = env_config.get("connection_budget", {}).get("adapters", {})
        self.adapter_configs = env_config.get("adapters", {})
        self.adapter_defaults = env_config.get("adapter_defaults", {})
        self._instances: dict[str, BaseAdapter] = {}
        self._config_cache: dict[str, dict] = {}
        self._warmup_errors: dict[str, str] = {}
        # Mock mode: the environment names real targets (http, hsm, …) but the
        # real adapter classes may not be importable (optional deps). When the
        # env or a per-adapter config opts into mock, route to MockAdapter.
        self.mock_mode = env_config.get("mode") == "mock"

    def _config_for(self, target: str, _seen: set[str] | None = None) -> dict:
        """Resolve an instance's effective config (extends + adapter_defaults)."""
        if target in self._config_cache:
            return self._config_cache[target]
        _seen = _seen or set()
        if target in _seen:
            raise ValueError(f"Circular 'extends' chain at adapter {target!r}.")
        _seen.add(target)

        raw = self.adapter_configs.get(target, {})
        parent = raw.get("extends")
        merged = self._config_for(parent, _seen) if parent else {}
        merged = _deep_merge(merged, {k: v for k, v in raw.items() if k != "extends"})

        impl = merged.get("adapter") or merged.get("type") or target
        defaults = self.adapter_defaults.get(impl, {})
        merged = _deep_merge(defaults, merged)

        self._config_cache[target] = merged
        return merged

    def _impl_name(self, target: str) -> str:
        config = self._config_for(target)
        return config.get("adapter") or config.get("type") or target

    def _resolve_class(self, target: str) -> type[BaseAdapter]:
        # Mock mode (global or per-adapter) wins even when a real adapter class
        # is importable — the environment names real targets but exercises them
        # against MockAdapter.
        config = self._config_for(target)
        if self.mock_mode or config.get("mock"):
            return MockAdapter
        impl = self._impl_name(target)
        cls = ADAPTER_MAP.get(impl)
        if cls is not None:
            return cls
        raise ValueError(
            f"Unknown adapter {impl!r} for instance {target!r}. "
            f"Set 'adapter' to one of: {sorted(ADAPTER_MAP)}"
        )

    async def warmup(self) -> None:
        # Best-effort: a target that can't connect must NOT abort the whole run —
        # it surfaces as a phase-1 (component health) failure instead. Otherwise
        # one unreachable host would crash the run before any result is emitted.
        for target in self.adapter_configs:
            try:
                await self.get(target)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"Warmup failed for {target}: {exc}")
                self._warmup_errors[target] = f"{type(exc).__name__}: {exc}"

    async def get(self, target: str) -> BaseAdapter:
        if target not in self._instances:
            config = self._config_for(target)
            pool_size = self.budget.get(target, config.get("pool_size", 100))

            # A participant group: several member connections under one name,
            # each a full config. Resolved before class lookup (there's no
            # "group" adapter class) — select a member per dispatch by policy.
            if config.get("adapter") == "group" or config.get("members"):
                from .group import GroupAdapter

                def _make_member(cfg: dict, _ps=pool_size) -> BaseAdapter:
                    if self.mock_mode or cfg.get("mock"):
                        return MockAdapter(cfg, pool_size=_ps)
                    impl = cfg.get("adapter") or cfg.get("type")
                    mcls = ADAPTER_MAP.get(impl)
                    if mcls is None:
                        raise ValueError(f"Unknown adapter {impl!r} for group member")
                    return mcls(cfg, pool_size=_ps)

                group: BaseAdapter = GroupAdapter(
                    config,
                    config.get("members") or [],
                    config.get("selection"),
                    make_child=_make_member,
                    pool_size=pool_size,
                )
                await group.connect()
                self._instances[target] = group
                return self._instances[target]

            cls = self._resolve_class(target)
            adapter = cls(config, pool_size=pool_size)
            await adapter.connect()
            self._instances[target] = adapter
        return self._instances[target]

    async def health_check_all(self) -> dict[str, bool]:
        import asyncio

        tasks = {t: asyncio.create_task(a.health_check()) for t, a in self._instances.items()}
        results = {}
        for target, task in tasks.items():
            try:
                results[target] = await task
            except Exception as e:
                log.warning(f"Health check failed for {target}: {e}")
                results[target] = False
        # adapters that never connected during warmup are unhealthy
        for target in self._warmup_errors:
            results.setdefault(target, False)
        return results

    async def retry_unhealthy(self, targets: set[str]) -> dict[str, bool]:
        """Second chance for phase 1: re-probe just the unhealthy targets.

        A relay/proxy with a stale upstream often heals itself on the first
        attempt (its reconnect can outlive the probe's timeout), so one retry
        turns that transient into a pass instead of blocking the whole run.
        Targets that failed warmup get a fresh connect (``get`` only caches
        successes, so calling it again re-dials).
        """
        results: dict[str, bool] = {}
        for target in sorted(targets):
            try:
                adapter = await self.get(target)
                self._warmup_errors.pop(target, None)
                results[target] = bool(await adapter.health_check())
            except Exception as exc:  # noqa: BLE001 — the failure IS the result
                log.warning(f"Health retry failed for {target}: {exc}")
                self._warmup_errors.setdefault(target, f"{type(exc).__name__}: {exc}")
                results[target] = False
        return results

    async def disconnect_all(self) -> None:
        for target, adapter in self._instances.items():
            try:
                await adapter.disconnect()
            except Exception as e:
                log.warning(f"Disconnect error for {target}: {e}")
        self._instances.clear()
