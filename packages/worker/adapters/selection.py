"""EndpointSelector — pick one endpoint from several by a selection policy.

A connection may declare multiple ``endpoints`` (each ``{host, port, weight}``)
and a ``selection`` policy. One selector instance is owned by a routing adapter
and called once per dispatch:

- ``round_robin`` — even rotation (load spread across hosts of one system).
- ``weighted``    — weighted-random by ``weight`` (capacity / canary split).
- ``random``      — uniform random.
- ``failover``    — always the first healthy endpoint; ``advance()`` moves to the
                    next when the current one fails (HA).
- ``sticky``      — hash a request field (``key``) so a given entity (BIN /
                    terminal / STAN) always lands on the same endpoint.

Rotation state is per-selector and therefore per-worker — under distributed load
each worker rotates independently, which is the simplest correct behaviour.
"""

from __future__ import annotations

import random
from typing import Any

POLICIES = {"round_robin", "weighted", "random", "failover", "sticky"}


def _dig(data: Any, path: str) -> Any:
    """Resolve a dotted ``path`` (e.g. ``de.2`` or ``request.mti``) in ``data``."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class EndpointSelector:
    def __init__(
        self,
        endpoints: list[dict],
        policy: str = "round_robin",
        key: str | None = None,
        *,
        seed: Any = None,
    ) -> None:
        self.endpoints = list(endpoints or [])
        self.policy = policy if policy in POLICIES else "round_robin"
        self.key = key
        self._rr = 0
        self._failover = 0
        self._rng = random.Random(seed)

    def __len__(self) -> int:
        return len(self.endpoints)

    def select(self, payload: Any = None) -> tuple[int, dict]:
        """Return ``(index, endpoint)`` for this dispatch."""
        n = len(self.endpoints)
        if n == 0:
            raise ValueError("no endpoints to select from")
        if n == 1:
            return 0, self.endpoints[0]

        if self.policy == "round_robin":
            i = self._rr % n
            self._rr += 1
        elif self.policy == "random":
            i = self._rng.randrange(n)
        elif self.policy == "weighted":
            i = self._weighted()
        elif self.policy == "failover":
            i = self._failover % n
        elif self.policy == "sticky":
            i = self._sticky(payload, n)
        else:  # defensive — normalised in __init__
            i = self._rr % n
            self._rr += 1
        return i, self.endpoints[i]

    def advance(self) -> None:
        """Move past the current endpoint (failover: the current one failed)."""
        if self.endpoints:
            self._failover = (self._failover + 1) % len(self.endpoints)

    # -- policies ------------------------------------------------------------

    def _weighted(self) -> int:
        weights = [max(0, int(e.get("weight", 1) or 0)) for e in self.endpoints]
        total = sum(weights)
        if total <= 0:
            return self._rng.randrange(len(self.endpoints))
        r = self._rng.uniform(0, total)
        upto = 0.0
        for i, w in enumerate(weights):
            upto += w
            if r <= upto:
                return i
        return len(self.endpoints) - 1

    def _sticky(self, payload: Any, n: int) -> int:
        raw = _dig(payload, self.key) if self.key else None
        if raw is None:
            # no key value — stable but arbitrary: fall back to first endpoint
            return 0
        return hash(str(raw)) % n
