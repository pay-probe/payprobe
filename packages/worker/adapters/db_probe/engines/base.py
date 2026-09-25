"""The engine interface the probe adapter talks to (ADR-0012).

An engine owns connections to one database and runs statements under the
guarantees the adapter promises: every read runs in a **read-only session**
(so a write is refused by the database itself, not by a regex), bounded by a
statement timeout and a row cap. Values come back JSON-friendly because step
responses land in run history and on the WebSocket.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ReadOnlyViolation(Exception):
    """The database refused a statement because the session is read-only."""


@dataclass
class FetchResult:
    rows: list[dict[str, Any]]
    columns: list[str]
    truncated: bool = False
    rows_affected: int | None = None
    notes: list[str] = field(default_factory=list)


def jsonable(value: Any) -> Any:
    """A JSON-serialisable view of a database value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


class Engine(ABC):
    """One database, one set of guarantees."""

    name = "abstract"

    def __init__(self, config: dict, *, pool_size: int) -> None:
        self.config = config
        self.pool_size = pool_size

    @abstractmethod
    async def connect(self) -> None:
        """Open the pool / connections. Raise on a bad config or unreachable host."""

    @abstractmethod
    async def ping(self) -> None:
        """``SELECT 1`` under the timeout; raise when the database is unusable."""

    @abstractmethod
    async def fetch(
        self, sql: str, params: list[Any], *, max_rows: int, timeout_ms: int
    ) -> FetchResult:
        """Run one read statement in a read-only session, positional parameters
        only, at most ``max_rows`` rows (``truncated`` set when more existed)."""

    async def execute_write(self, sql: str, params: list[Any], *, timeout_ms: int) -> FetchResult:
        """Run one write statement in an ordinary transaction (ADR-0012 phase 2).
        Only called when the connection opted in with ``writes``; engines that
        cannot write raise ``NotImplementedError``."""
        raise NotImplementedError(f"{self.name} engine does not support writes")

    async def validate(self, sql: str, nparams: int) -> str | None:
        """Check that ``sql`` parses and binds ``nparams`` parameters without
        running it (the diagnostics databases layer). ``None`` when fine, else
        the database's error text."""
        return None

    @abstractmethod
    async def close(self) -> None:
        """Release everything."""

    def describe(self) -> str:
        """One line for logs and raw_log; never includes credentials."""
        return self.name


__all__ = ["Engine", "FetchResult", "ReadOnlyViolation", "jsonable"]
