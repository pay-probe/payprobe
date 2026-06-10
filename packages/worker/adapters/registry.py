"""AdapterRegistry — resolves target name to adapter instance."""
import logging
from .base.base_adapter import BaseAdapter
from .mock.adapter import MockAdapter

log = logging.getLogger(__name__)

# Populated at import time; real adapters imported lazily to keep deps optional
ADAPTER_MAP: dict[str, type[BaseAdapter]] = {
    "mock":   MockAdapter,
}

def _register_real_adapters():
    """Import real adapters only if their deps are installed."""
    try:
        from .xpay.adapter import XPayAdapter
        ADAPTER_MAP["xpay"] = XPayAdapter
    except ImportError:
        pass
    try:
        from .hsm.adapter import HSMAdapter
        ADAPTER_MAP["hsm"] = HSMAdapter
    except ImportError:
        pass
    try:
        from .terminal.adapter import SimulatedTerminalAdapter, PhysicalTerminalAdapter
        ADAPTER_MAP["terminal_sim"] = SimulatedTerminalAdapter
        ADAPTER_MAP["terminal_physical"] = PhysicalTerminalAdapter
    except ImportError:
        pass
    try:
        from .core_banking.adapter import CoreBankingAdapter
        ADAPTER_MAP["core_banking"] = CoreBankingAdapter
    except ImportError:
        pass
    try:
        from .switch.adapter import SwitchAdapter
        ADAPTER_MAP["switch"] = SwitchAdapter
    except ImportError:
        pass
    try:
        from .acquirer.adapter import AcquirerAdapter
        ADAPTER_MAP["acquirer"] = AcquirerAdapter
    except ImportError:
        pass
    try:
        from .db_probe.adapter import DBProbeAdapter
        ADAPTER_MAP["db_probe_core"] = DBProbeAdapter
        ADAPTER_MAP["db_probe_switch"] = DBProbeAdapter
    except ImportError:
        pass

_register_real_adapters()


class AdapterRegistry:

    def __init__(self, env_config: dict):
        self.env_config = env_config
        self.budget = env_config.get("connection_budget", {}).get("adapters", {})
        self.adapter_configs = env_config.get("adapters", {})
        self._instances: dict[str, BaseAdapter] = {}

    async def warmup(self) -> None:
        for target in self.adapter_configs:
            await self.get(target)

    async def get(self, target: str) -> BaseAdapter:
        if target not in self._instances:
            cls = ADAPTER_MAP.get(target)
            if cls is None:
                raise ValueError(f"Unknown adapter: {target!r}. Available: {list(ADAPTER_MAP)}")
            config = self.adapter_configs.get(target, {})
            pool_size = self.budget.get(target, 100)
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
        return results

    async def disconnect_all(self) -> None:
        for target, adapter in self._instances.items():
            try:
                await adapter.disconnect()
            except Exception as e:
                log.warning(f"Disconnect error for {target}: {e}")
        self._instances.clear()
