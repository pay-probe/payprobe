"""Persistent change-journal sessions (in-memory or Redis).

Keyed by ``session_id``, stores the serializable journal records so one-click
revert works across chat turns and across replicas. Backend chosen by
``REDIS_URL`` — the same convention as the orchestrator's run-control.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

SESSION_TTL_S = int(os.environ.get("ASSIST_SESSION_TTL", str(24 * 3600)))
_KEY_PREFIX = "payprobe:assist:session:"


class SessionStore(Protocol):
    async def append(self, session_id: str, records: list[dict]) -> None: ...
    async def get(self, session_id: str) -> list[dict]: ...
    async def replace(self, session_id: str, records: list[dict]) -> None: ...
    async def clear(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, list[dict]] = {}

    async def append(self, session_id: str, records: list[dict]) -> None:
        if records:
            self._sessions.setdefault(session_id, []).extend(records)

    async def get(self, session_id: str) -> list[dict]:
        return list(self._sessions.get(session_id, []))

    async def replace(self, session_id: str, records: list[dict]) -> None:
        if records:
            self._sessions[session_id] = list(records)
        else:
            self._sessions.pop(session_id, None)

    async def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class RedisSessionStore:
    def __init__(self, redis, ttl_s: int = SESSION_TTL_S) -> None:
        self.redis = redis
        self.ttl = ttl_s

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def append(self, session_id: str, records: list[dict]) -> None:
        if not records:
            return
        existing = await self.get(session_id)
        existing.extend(records)
        await self.redis.set(self._key(session_id), json.dumps(existing), ex=self.ttl)

    async def get(self, session_id: str) -> list[dict]:
        raw = await self.redis.get(self._key(session_id))
        return json.loads(raw) if raw else []

    async def replace(self, session_id: str, records: list[dict]) -> None:
        if records:
            await self.redis.set(self._key(session_id), json.dumps(records),
                                 ex=self.ttl)
        else:
            await self.redis.delete(self._key(session_id))

    async def clear(self, session_id: str) -> None:
        await self.redis.delete(self._key(session_id))


def build_session_store() -> SessionStore:
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis.asyncio as aioredis

            return RedisSessionStore(aioredis.from_url(url, decode_responses=True))
        except Exception:  # noqa: BLE001
            pass
    return InMemorySessionStore()
