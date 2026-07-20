"""Server-side chat history (in-memory or Redis), keyed per user.

The portal keeps a localStorage cache for offline/fallback, but the durable
copy lives here so conversations survive browser changes and reloads keep
their revert sessions reachable. Keyed by the caller's JWT ``sub`` (or
"anonymous" in dev mode without auth) — one user never sees another's chats.

Documents are opaque to this store: the portal owns the shape (id, title,
mode, sessionId, turns, updatedAt). We only require ``id`` and keep the list
newest-first, capped.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

HISTORY_TTL_S = int(os.environ.get("ASSIST_HISTORY_TTL", str(30 * 24 * 3600)))
MAX_CHATS = int(os.environ.get("ASSIST_HISTORY_MAX", "40"))
_KEY_PREFIX = "payprobe:assist:chats:"


def _upsert(chats: list[dict], doc: dict) -> list[dict]:
    """Newest-first upsert by id, capped at MAX_CHATS."""
    rest = [c for c in chats if c.get("id") != doc.get("id")]
    return [doc, *rest][:MAX_CHATS]


class ChatHistoryStore(Protocol):
    async def list(self, user: str) -> list[dict]: ...
    async def get(self, user: str, chat_id: str) -> dict | None: ...
    async def put(self, user: str, doc: dict) -> None: ...
    async def delete(self, user: str, chat_id: str) -> None: ...
    async def clear(self, user: str) -> None: ...


class InMemoryChatHistory:
    def __init__(self) -> None:
        self._chats: dict[str, list[dict]] = {}

    async def list(self, user: str) -> list[dict]:
        return list(self._chats.get(user, []))

    async def get(self, user: str, chat_id: str) -> dict | None:
        return next((c for c in self._chats.get(user, [])
                     if c.get("id") == chat_id), None)

    async def put(self, user: str, doc: dict) -> None:
        self._chats[user] = _upsert(self._chats.get(user, []), doc)

    async def delete(self, user: str, chat_id: str) -> None:
        chats = [c for c in self._chats.get(user, [])
                 if c.get("id") != chat_id]
        if chats:
            self._chats[user] = chats
        else:
            self._chats.pop(user, None)

    async def clear(self, user: str) -> None:
        self._chats.pop(user, None)


class RedisChatHistory:
    def __init__(self, redis, ttl_s: int = HISTORY_TTL_S) -> None:
        self.redis = redis
        self.ttl = ttl_s

    def _key(self, user: str) -> str:
        return f"{_KEY_PREFIX}{user}"

    async def list(self, user: str) -> list[dict]:
        raw = await self.redis.get(self._key(user))
        return json.loads(raw) if raw else []

    async def get(self, user: str, chat_id: str) -> dict | None:
        return next((c for c in await self.list(user)
                     if c.get("id") == chat_id), None)

    async def put(self, user: str, doc: dict) -> None:
        chats = _upsert(await self.list(user), doc)
        await self.redis.set(self._key(user), json.dumps(chats), ex=self.ttl)

    async def delete(self, user: str, chat_id: str) -> None:
        chats = [c for c in await self.list(user) if c.get("id") != chat_id]
        if chats:
            await self.redis.set(self._key(user), json.dumps(chats),
                                 ex=self.ttl)
        else:
            await self.redis.delete(self._key(user))

    async def clear(self, user: str) -> None:
        await self.redis.delete(self._key(user))


def build_chat_history() -> ChatHistoryStore:
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis.asyncio as aioredis

            return RedisChatHistory(aioredis.from_url(url,
                                                      decode_responses=True))
        except Exception:  # noqa: BLE001
            pass
    return InMemoryChatHistory()
