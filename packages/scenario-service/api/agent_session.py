"""Persistent change-journal sessions for the assistant.

The assistant makes autonomous writes; the safety net is one-click revert. For
that to work *across chat turns* (and across replicas), the per-session change
journal must outlive a single request. This module stores the serializable
journal records (from :mod:`api.agent_tools`) keyed by ``session_id``.

Two backends, chosen by ``REDIS_URL`` (same convention as the orchestrator's
run-control):

* :class:`InMemorySessionStore` — a local dict (dev / CI / single process).
* :class:`RedisSessionStore` — a Redis key per session with a TTL, visible to
  every replica.

Records are plain JSON (``{seq, tool, resource, key, summary, before}``); restore
is a pure function of them (:func:`api.agent_tools.restore_journal`), so any
replica can revert a session it didn't create.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

#: How long an idle session's journal is kept (seconds).
SESSION_TTL_S = int(os.environ.get("ASSIST_SESSION_TTL", str(24 * 3600)))
_KEY_PREFIX = "payprobe:assist:session:"


class SessionStore(Protocol):
    async def append(self, session_id: str, records: list[dict]) -> None: ...
    async def get(self, session_id: str) -> list[dict]: ...
    async def clear(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Single-process journal sessions (a local dict)."""

    def __init__(self) -> None:
        self._sessions: dict[str, list[dict]] = {}

    async def append(self, session_id: str, records: list[dict]) -> None:
        if not records:
            return
        self._sessions.setdefault(session_id, []).extend(records)

    async def get(self, session_id: str) -> list[dict]:
        return list(self._sessions.get(session_id, []))

    async def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class RedisSessionStore:
    """Cross-replica journal sessions (one Redis list/string per session)."""

    def __init__(self, redis, ttl_s: int = SESSION_TTL_S) -> None:
        self.redis = redis  # redis.asyncio.Redis, decode_responses=True
        self.ttl = ttl_s

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def append(self, session_id: str, records: list[dict]) -> None:
        if not records:
            return
        key = self._key(session_id)
        existing = await self.get(session_id)
        existing.extend(records)
        await self.redis.set(key, json.dumps(existing), ex=self.ttl)

    async def get(self, session_id: str) -> list[dict]:
        raw = await self.redis.get(self._key(session_id))
        return json.loads(raw) if raw else []

    async def clear(self, session_id: str) -> None:
        await self.redis.delete(self._key(session_id))


def build_session_store() -> SessionStore:
    """Pick the backend: Redis when ``REDIS_URL`` is set, else in-memory."""
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis.asyncio as aioredis  # imported only when configured

            return RedisSessionStore(aioredis.from_url(url, decode_responses=True))
        except Exception:  # noqa: BLE001 - fall back rather than fail startup
            pass
    return InMemorySessionStore()
