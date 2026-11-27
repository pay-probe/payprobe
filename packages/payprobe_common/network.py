"""Where PayProbe's own listeners bind, and the host the UI suggests dialing.

Every listener PayProbe starts (simulators, participant-flow listeners, the
standalone responder) binds the host its config names. A config that names no
host binds ``listen_host()``:

    PAYPROBE_LISTEN_HOST      default bind address (default ``127.0.0.1``:
                              reachable from this machine only; ``0.0.0.0``
                              accepts connections from outside a container)
    PAYPROBE_ADVERTISED_HOST  host the portal pre-fills when it suggests where
                              to dial one of those listeners (default
                              ``127.0.0.1``), e.g. the name a deployment
                              publishes the listener ports under

Both are read on every call, so a test or a restarted process sees the current
environment.
"""
from __future__ import annotations

import os

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_ADVERTISED_HOST = "127.0.0.1"


def listen_host() -> str:
    """Bind address for a PayProbe listener whose config names no host."""
    return os.environ.get("PAYPROBE_LISTEN_HOST", "").strip() or DEFAULT_LISTEN_HOST


def advertised_host() -> str:
    """Host to suggest to someone dialing a PayProbe listener."""
    return (os.environ.get("PAYPROBE_ADVERTISED_HOST", "").strip()
            or DEFAULT_ADVERTISED_HOST)
