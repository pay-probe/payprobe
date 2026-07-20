"""GroupAdapter — one logical participant backed by several member connections.

A *participant group* is N connection instances (often the same adapter type)
acting as one logical participant — a fleet of issuers, several acquirer hosts.
GroupAdapter selects among *members* — each a full worker-shaped adapter config,
possibly a different adapter class — using the
:class:`~worker.adapters.selection.EndpointSelector` policies (round_robin /
weighted / random / failover / sticky).

The orchestrator inlines each member's resolved config into the group's
``members`` list at run-build time, so the worker needs no registry lookups.
"""

from __future__ import annotations

from typing import Callable

from .base.base_adapter import BaseAdapter, StepResult
from .selection import EndpointSelector


class GroupAdapter(BaseAdapter):
    def __init__(
        self,
        config: dict,
        members: list[dict],
        selection: dict | None,
        make_child: Callable[[dict], BaseAdapter],
        pool_size: int = 100,
    ) -> None:
        super().__init__(config, pool_size)
        # members: [{"connection"|"name": str, "config": {...}, "weight": int}]
        self.members = list(members or [])
        sel = selection or {}
        self.selector = EndpointSelector(
            self.members, sel.get("policy", "round_robin"), sel.get("key")
        )
        self._make_child = make_child
        self._children: dict[int, BaseAdapter] = {}

    async def connect(self) -> None:
        return  # members connect lazily on first dispatch

    async def _child(self, index: int, member: dict) -> BaseAdapter:
        if index not in self._children:
            child = self._make_child(member.get("config") or {})
            await child.connect()
            self._children[index] = child
        return self._children[index]

    async def execute(self, action: str, payload: dict) -> StepResult:
        failover = self.selector.policy == "failover"
        attempts = len(self.selector) if failover else 1
        last: StepResult | None = None
        last_exc: Exception | None = None
        for _ in range(max(1, attempts)):
            index, member = self.selector.select(payload)
            try:
                child = await self._child(index, member)
                result = await child.execute(action, payload)
            except Exception as exc:  # noqa: BLE001 — connect/transport blew up
                last_exc = exc
                await self._evict(index)  # rebuild next time so a restart recovers
                if not failover:
                    raise
                self.selector.advance()
                continue
            if not result.success:
                # Transport failure (no response). TcpAdapter returns
                # success=False rather than raising, so drop the stale child —
                # otherwise a member that is stopped and restarted is never
                # reconnected and traffic never returns to it.
                await self._evict(index)
                last = result
                if failover:
                    self.selector.advance()
                    continue
            return result
        if last is not None:
            return last
        assert last_exc is not None
        raise last_exc

    async def _evict(self, index: int) -> None:
        """Drop a cached child so the next selection of this member reconnects
        (a stopped→restarted member otherwise keeps a dead connection)."""
        child = self._children.pop(index, None)
        if child is not None:
            try:
                await child.disconnect()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    async def health_check(self) -> bool:
        if not self.members:
            return False
        try:
            child = await self._child(0, self.members[0])
            return await child.health_check()
        except Exception:  # noqa: BLE001
            return False

    async def disconnect(self) -> None:
        for child in self._children.values():
            try:
                await child.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._children.clear()
