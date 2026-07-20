"""Process-global registry of OUTBOUND sockets engine adapters hold open.

The Live Sessions page (/peers) has always shown the inbound side — every
client connected *to* a listener we host — but the sockets the engine itself
opens toward hosts (a TCP adapter dialing a simulator or a real endpoint)
were invisible. Adapters register here on connect and unregister on drop or
close, so the orchestrator (which runs the engine in-process for normal runs
and participant flows) can merge them into the ``outbound`` half of /peers.

Deliberately tiny and dependency-free: a dict behind a threading lock, safe
to call from any event loop or thread. Entries from separate load-worker
processes are NOT visible here — fleet-scale socket reporting (potentially
100K conns) is a different problem and is intentionally out of scope.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_socks: dict[int, dict[str, Any]] = {}
_seq = 0


def register(
    *,
    host: str,
    port: int | None,
    adapter: str = "tcp",
    local: str = "",
    owner: str = "",
) -> int:
    """Record one established outbound socket; returns a token for unregister."""
    global _seq
    with _lock:
        _seq += 1
        _socks[_seq] = {
            "host": host,
            "port": port,
            "adapter": adapter,
            "local": local,
            "owner": owner,
            "opened_at": time.time(),
        }
        return _seq


def unregister(token: int | None) -> None:
    if token is None:
        return
    with _lock:
        _socks.pop(token, None)


def snapshot() -> list[dict[str, Any]]:
    """Current outbound sockets, each with a computed age in seconds."""
    now = time.time()
    with _lock:
        return [{**d, "age_sec": round(now - d["opened_at"], 1)} for d in _socks.values()]
