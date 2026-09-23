"""Test fixtures for agent-hub.

The registry is PostgreSQL-only (ADR-0010): the suite runs against a real
database, never a file. ``AGENT_HUB_TEST_DATABASE_URL`` (default: the compose
dev credentials on localhost) points at it; CI's ``test-services`` job
provides the ``postgres:15`` service. If the database is unreachable the
suite is *skipped with a reason*, mirroring how the NATS suites behave when
nats-py is absent (CLAUDE.md: environmental, not a regression).

The caller gate fails closed outside dev/test; unit tests drive endpoints
without tokens, so mark the environment (auth itself is tested explicitly
with a monkeypatched env in test_api.py). Each test starts from truncated
tables, then the app's lifespan re-seeds the builtins.
"""

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("PAYPROBE_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parent))  # hub_testkit
from hub_testkit import TEST_DSN, truncate  # noqa: E402

os.environ["AGENT_HUB_DATABASE_URL"] = TEST_DSN

# agent_hub importable when running from packages/, and packages/ itself
# importable (payprobe_common) when running from the package dir. The folded-in
# assistant (D2) lives in the sibling package, mounted under /assistant.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "payprobe-assistant"))

import pytest
from agent_hub.main import app
from agent_hub.store import RegistryStore
from fastapi.testclient import TestClient


def _reachable() -> str | None:
    """Connect once; return the failure reason or None."""
    import asyncpg

    async def probe():
        con = await asyncpg.connect(TEST_DSN, timeout=3)
        await con.close()

    try:
        asyncio.run(probe())
        return None
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ skip with reason
        return f"{type(exc).__name__}: {exc}"


_UNREACHABLE = _reachable()


def pytest_collection_modifyitems(config, items):
    if _UNREACHABLE:
        skip = pytest.mark.skip(
            reason=f"agent-hub tests need PostgreSQL at {TEST_DSN} ({_UNREACHABLE}); "
            "set AGENT_HUB_TEST_DATABASE_URL"
        )
        for item in items:
            item.add_marker(skip)


@pytest.fixture()
async def store():
    """A connected store over truncated tables (direct, no HTTP)."""
    s = await RegistryStore.connect(TEST_DSN, pool_max=2)
    await s.reset()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture()
def client():
    """The app over truncated tables; lifespan connects, migrates and seeds."""
    asyncio.run(truncate())
    with TestClient(app) as c:
        yield c
