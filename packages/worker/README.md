# PayProbe Worker Engine

The async test execution engine. Runs test scenarios against target systems, manages connection pools, enforces phase gates, and streams results back to the orchestrator.

## Architecture

```
WorkerEngine
├── AdapterRegistry     — resolves target name → adapter instance
├── PhaseRunner         — phase 1/2/3 gate logic
├── StepExecutor        — individual step execution + assertion
└── ResultStreamer       — publishes events to Redis
```

## Performance

- **50,000 concurrent outbound connections** via asyncio semaphore
- **10,000 operations/second** sustained throughput
- Built on **uvloop** (libuv-based event loop, ~2x faster than default asyncio)
- All adapters use **aiohttp** async connection pools

## Configuration

The worker receives a full environment config at startup:

```json
{
  "connection_budget": {
    "xpay": 20000,
    "terminal": 15000,
    "switch": 8000,
    "core_banking": 5000,
    "hsm": 1000
  },
  "adapters": {
    "xpay": { "base_url": "...", "api_key": "..." }
  }
}
```

## OS Requirements

The worker host must have these kernel parameters set. See [docs/deployment/os-tuning.md](https://github.com/pay-probe/payprobe/blob/main/docs/deployment/os-tuning.md).

```bash
fs.file-max = 200000
net.ipv4.ip_local_port_range = 1024 65535
net.core.somaxconn = 65535
```

## Development

```bash
cd packages/worker
pip install -e ".[dev]"
pytest tests/ -v
```
