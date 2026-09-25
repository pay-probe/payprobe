"""Database engines behind the probe adapter (ADR-0012).

One small interface, one module per engine. ``postgresql`` (asyncpg, already a
worker dependency) and ``sqlite`` (stdlib) ship; ``oracle``, ``mssql`` and
``mysql`` are named here so a misspelt engine and a not-yet-built one give
different errors, but they are extras, not built.
"""

from __future__ import annotations

from .base import Engine, FetchResult, ReadOnlyViolation

#: engine name -> (module, class); imported lazily so a missing optional driver
#: only fails the connection that asked for it.
ENGINES: dict[str, tuple[str, str]] = {
    "postgresql": ("worker.adapters.db_probe.engines.postgresql", "PostgresEngine"),
    "postgres": ("worker.adapters.db_probe.engines.postgresql", "PostgresEngine"),
    "sqlite": ("worker.adapters.db_probe.engines.sqlite", "SqliteEngine"),
}
#: recognised but not built (ADR-0012 defers them to optional extras)
PLANNED_ENGINES: dict[str, str] = {
    "oracle": "python-oracledb (thin, async)",
    "mssql": "aioodbc + an ODBC driver in the image",
    "mysql": "aiomysql",
}


def make_engine(name: str, config: dict, *, pool_size: int) -> Engine:
    key = (name or "postgresql").lower()
    if key in PLANNED_ENGINES:
        raise ValueError(
            f"db_probe engine {key!r} is not built yet (ADR-0012 defers it to an optional "
            f"extra: {PLANNED_ENGINES[key]}); use postgresql or sqlite"
        )
    if key not in ENGINES:
        raise ValueError(
            f"unknown db_probe engine {name!r}; expected one of {', '.join(sorted(ENGINES))}"
        )
    import importlib

    module, cls_name = ENGINES[key]
    cls = getattr(importlib.import_module(module), cls_name)
    return cls(config, pool_size=pool_size)


__all__ = [
    "ENGINES",
    "PLANNED_ENGINES",
    "Engine",
    "FetchResult",
    "ReadOnlyViolation",
    "make_engine",
]
