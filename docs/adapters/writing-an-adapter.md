# Writing an Adapter

An adapter is the translation layer between PayProbe's generic step model
and a specific target system's protocol. Writing one typically takes 2–4 hours.

## 1. Understand the Base Contract

Read `packages/worker/adapters/base/base_adapter.py` before writing a single line.
You must implement four methods: `connect`, `health_check`, `execute`, `disconnect`.

## 2. Create Your Package

```
packages/worker/adapters/your_system/
├── __init__.py
├── adapter.py       # Your YourSystemAdapter class
├── config.py        # Pydantic config model (optional but recommended)
├── actions.py       # String constants for supported actions
├── tests/
│   ├── test_adapter.py
│   └── conftest.py
└── README.md        # Config reference and actions table
```

## 3. Implement the Adapter

```python
import aiohttp
import time
from ..base.base_adapter import BaseAdapter, StepResult

class YourSystemAdapter(BaseAdapter):

    async def connect(self) -> None:
        connector = aiohttp.TCPConnector(limit=self.pool_size)
        self.session = aiohttp.ClientSession(connector=connector)

    async def health_check(self) -> bool:
        try:
            async with self.session.get(
                f"{self.config['base_url']}/health", timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()
        if action == "your_action":
            response = await self._your_action(payload)
        else:
            raise ValueError(f"Unsupported action: {action!r}")
        duration = int((time.monotonic() - start) * 1000)
        return StepResult(
            success=True,
            request_payload=payload,
            response_payload=response,
            duration_ms=duration,
        )

    async def disconnect(self) -> None:
        await self.session.close()
```

## 4. Write a Mock

Add `adapters/mock/your_system_responses.py` with canned responses
so tests and local dev work without a real system.

## 5. Register

Add your adapter to `packages/worker/adapters/registry.py`:

```python
from .your_system.adapter import YourSystemAdapter
ADAPTER_MAP["your_system"] = YourSystemAdapter
```

## 6. Test

```bash
cd packages/worker
pytest adapters/your_system/tests/ -v
```

## 7. Add an Example Scenario

Add a `.json` file to `examples/scenarios/` that uses your adapter.

## 8. Submit a PR

Follow the [Contributing Guide](https://github.com/pay-probe/payprobe/blob/main/CONTRIBUTING.md).
The PR template has an adapter-specific checklist.
