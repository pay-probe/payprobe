"""Outbound socket registry — the /peers outbound leg for engine adapters.

A TcpAdapter must be visible in the registry while its socket is up, survive
a drop→reconnect as one (re-registered) entry, and vanish on disconnect.
"""

import asyncio

import pytest

from worker.adapters import socket_registry
from worker.adapters.tcp.adapter import TcpAdapter


@pytest.fixture(autouse=True)
def _clean_registry():
    # tests share the process-global registry — start and end empty
    for row in list(socket_registry._socks):  # noqa: SLF001 — test reset
        socket_registry._socks.pop(row, None)
    yield
    for row in list(socket_registry._socks):  # noqa: SLF001 — test reset
        socket_registry._socks.pop(row, None)


async def _silent_server():
    """A listener that accepts and holds connections without answering."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_adapter_registers_and_unregisters():
    server, port = await _silent_server()
    try:
        adapter = TcpAdapter(
            {
                "host": "127.0.0.1",
                "port": port,
                "adapter": "tcp_iso8583",
                "name": "to-switch",
                "sign_on": {"enabled": False},
            }
        )
        await adapter.connect()
        rows = socket_registry.snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["host"] == "127.0.0.1" and row["port"] == port
        assert row["adapter"] == "tcp_iso8583" and row["owner"] == "to-switch"
        assert row["age_sec"] >= 0 and row["local"].startswith("127.0.0.1:")

        await adapter.disconnect()
        assert socket_registry.snapshot() == []
    finally:
        server.close()
        await server.wait_closed()


async def test_drop_unregisters_then_reconnect_reregisters():
    server, port = await _silent_server()
    try:
        adapter = TcpAdapter(
            {
                "host": "127.0.0.1",
                "port": port,
                "sign_on": {"enabled": False},
                "reconnect": {"enabled": True, "backoff_initial_sec": 0.05},
            }
        )
        await adapter.connect()
        assert len(socket_registry.snapshot()) == 1

        # sever the socket server-side; the drop must unregister…
        adapter._writer.close()  # noqa: SLF001 — simulate a peer drop
        for _ in range(50):
            if not socket_registry.snapshot():
                break
            await asyncio.sleep(0.02)

        # …and the reconnect loop must register the new socket
        for _ in range(100):
            if socket_registry.snapshot():
                break
            await asyncio.sleep(0.02)
        assert len(socket_registry.snapshot()) == 1

        await adapter.disconnect()
        assert socket_registry.snapshot() == []
    finally:
        server.close()
        await server.wait_closed()
