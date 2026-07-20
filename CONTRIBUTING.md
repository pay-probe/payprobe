# Contributing to PayProbe

Thank you for your interest in contributing. PayProbe is built by backend and integration engineers for backend and integration engineers — your real-world experience is the most valuable thing you can bring.

## Ways to Contribute

| Type | Examples | Difficulty |
|---|---|---|
| **New adapter** | Add support for a new protocol, service, or HSM vendor | Medium |
| **Bug fix** | Fix a defect in the worker engine, portal, or backend services | Low–Medium |
| **Scenario examples** | Add example test scenarios for common integration flows | Low |
| **Documentation** | Improve guides, fix typos, add examples | Low |
| **Feature** | New portal feature, worker capability, report format | Medium–High |

## Getting Started

### 1. Fork and clone

```bash
git clone https://github.com/pay-probe/payprobe.git
cd payprobe
git remote add upstream https://github.com/pay-probe/payprobe.git
```

### 2. Start the development environment

```bash
docker compose -f infra/docker/docker-compose.dev.yml up
```

This starts all services with hot-reload enabled and mock adapters active.

### 3. Find something to work on

- Check [Issues](https://github.com/pay-probe/payprobe/issues) labelled `good first issue`
- Check [Issues](https://github.com/pay-probe/payprobe/issues) labelled `help wanted`
- Propose a new adapter in [Discussions](https://github.com/pay-probe/payprobe/discussions)

## Writing a New Adapter

This is the highest-value contribution. A new adapter adds support for a protocol or system component that PayProbe doesn't yet know about.

### Step 1 — Read the base class

All adapters extend `BaseAdapter` in `packages/worker/adapters/base/base_adapter.py`. Read it fully before starting.

### Step 2 — Create your adapter package

```
packages/worker/adapters/your_system/
├── __init__.py
├── adapter.py          # Your adapter class
├── config.py           # Pydantic config model
├── actions.py          # Supported action constants
├── tests/
│   ├── test_adapter.py # Unit tests (mock only, no real system needed)
│   └── conftest.py
└── README.md           # Document what this adapter connects to
```

### Step 3 — Implement the four required methods

```python
class YourSystemAdapter(BaseAdapter):

    async def connect(self) -> None:
        # Warm up connection pool
        ...

    async def health_check(self) -> bool:
        # Return True if system is reachable and ready
        ...

    async def execute(self, action: str, payload: dict) -> StepResult:
        # Route action to the right method
        ...

    async def disconnect(self) -> None:
        # Clean up connections
        ...
```

### Step 4 — Register the adapter

Add your adapter to `packages/worker/adapters/registry.py`:

```python
from adapters.your_system.adapter import YourSystemAdapter

ADAPTER_MAP = {
    ...
    "your_system": YourSystemAdapter,
}
```

### Step 5 — Add a mock version

Every adapter must have a mock counterpart in `adapters/mock/` so the test suite and local dev environment work without real hardware.

### Step 6 — Write tests

```bash
cd packages/worker
pytest adapters/your_system/tests/ -v
```

Tests must pass without any real external system. Use the mock.

### Step 7 — Add an example scenario

Add at least one example scenario in `examples/scenarios/` that demonstrates your adapter.

### Step 8 — Update the adapter table in README.md

Add a row to the adapters table with your adapter name, protocol, and status `✅ Stable` or `🧪 Beta`.

## Pull Request Guidelines

- **One PR per concern** — one adapter, one bug fix, one feature
- **All tests must pass** — `make test` must exit 0 (the CI gate)
- **Include a description** — what does this change, why, how to test it
- **Reference any related issue** — `Closes #123`
- **No credentials or hostnames** — never commit real system addresses or secrets

## Branch Naming

```
feature/adapter-thales-hsm
fix/worker-semaphore-leak
docs/improve-quickstart
chore/update-dependencies
```

## Commit Style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(adapter): add Thales HSM adapter with PKCS#11 support
fix(worker): release semaphore on step timeout
docs(adapter): add writing guide for ISO 8583 adapters
chore: update aiohttp to 3.9.1
```

## Code Style

**Python:** `ruff` for linting, `black` for formatting
```bash
cd packages/worker
ruff check . && black --check .
```

**TypeScript/Angular:** Prettier (the enforced style — the portal has no
angular-eslint target, so `ng lint` is not configured)
```bash
cd packages/portal
npm run format:check
```

## Questions?

Open a [Discussion](https://github.com/pay-probe/payprobe/discussions) — don't open an Issue for questions.
