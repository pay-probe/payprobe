"""Agent tools — the standalone (REST) deployment of the shared toolkit.

The registry, guardrails, journal and every handler live ONCE in
``payprobe_common.agent_toolkit`` (shared with scenario-service's in-process
``/agent/chat``). This module supplies the **REST backend** — the primitive
resource operations over the PayProbe services' HTTP APIs
(:mod:`assistant_service.rest`) — plus thin compatibility wrappers so the
loop/session/main modules keep their existing imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # packages/ on the path (dev: PYTHONPATH=packages; container: /app)
    from payprobe_common import agent_toolkit as _tk
except ModuleNotFoundError:  # pragma: no cover — running from the package dir
    import sys
    from pathlib import Path

    _here = Path(__file__).resolve()
    for _cand in (_here.parents[1], _here.parents[2]):
        if (_cand / "payprobe_common").is_dir():
            sys.path.insert(0, str(_cand))
            break
    from payprobe_common import agent_toolkit as _tk

from payprobe_common import rest_backend as _common_rest

from . import rest

# Re-exports: the public surface predating the extraction, unchanged.
GuardrailError = _tk.GuardrailError
ToolError = _tk.ToolError
ChangeRecord = _tk.ChangeRecord
ChangeJournal = _tk.ChangeJournal
ToolSpec = _tk.ToolSpec
REGISTRY = _tk.REGISTRY
tools_for = _tk.tools_for
schemas_for = _tk.schemas_for
dispatch = _tk.dispatch


def _get_or_none(url: str) -> Any:
    """GET that returns None on a 404 (used to capture prior state)."""
    try:
        return rest.request("GET", url)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


class RestBackend(_common_rest.RestBackend):
    """The shared toolkit's primitive ops over the PayProbe REST APIs.

    The implementation moved to ``payprobe_common.rest_backend`` (ADR-0010:
    agent-hub needs the same backend and a second copy is invariant #3's
    drift). This subclass only binds this deployment's transport and base
    URLs. ``rest.request`` is looked up at call time so tests can still
    monkeypatch it.
    """

    def __init__(self) -> None:
        super().__init__(
            request=lambda *a, **kw: rest.request(*a, **kw),
            scenario_api=rest.SCENARIO_API,
            run_api=rest.RUN_API,
            insight_api=rest.INSIGHT_API,
        )


@dataclass
class ToolContext:
    """Backwards-compatible context: no-arg construction, REST backend."""

    journal: ChangeJournal = field(default_factory=ChangeJournal)
    referenced_connections: set[str] = field(default_factory=set)
    backend: Any = field(default_factory=RestBackend)


def restore_one(rec: dict) -> None:
    """Pre-unification signature (record only) — kept for callers/tests."""
    _tk.restore_one(ToolContext(), rec)


def restore_journal(records: list[dict]) -> int:
    """Pre-unification signature (records only) — kept for callers/tests."""
    return _tk.restore_journal(ToolContext(), records)


def referenced_connection_names() -> set[str]:
    """Connection names a scenario currently references (best-effort), for the
    delete guardrail. Scans each scenario's serialized JSON for quoted names."""
    import json

    try:
        conns = {c.get("name") for c in rest.request("GET", rest.s("/connections"))}
        conns.discard(None)
        if not conns:
            return set()
        referenced: set[str] = set()
        for row in rest.request("GET", rest.s("/scenarios")):
            sid = row.get("id")
            if not sid:
                continue
            doc = _get_or_none(rest.s(f"/scenarios/{sid}"))
            if not doc:
                continue
            blob = json.dumps(doc)
            for name in conns:
                if f'"{name}"' in blob:
                    referenced.add(name)
        return referenced
    except RuntimeError:
        return set()
