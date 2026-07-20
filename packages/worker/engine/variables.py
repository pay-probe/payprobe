"""Inter-step variable resolution.

Steps may reference values produced by *earlier* steps using
``${step_id.response.path}`` syntax, e.g.::

    {"rrn": "${step_003.response.rrn}"}

At runtime the engine resolves these against a context of completed step
results before handing the payload to an adapter. The structural validator in
``scenario-service`` already guarantees references point at earlier steps; this
is the execution-time counterpart.
"""

from __future__ import annotations

import re
from typing import Any

from .generators import GeneratorContext

#: Matches ${step.path.to.value} or a generator call ${rand.pan(411111,19)}.
#: ``path`` may contain dots and list indices; an optional trailing ``(...)``
#: carries generator arguments.
VAR_REF_RE = re.compile(r"\$\{(?P<expr>[A-Za-z0-9_.\[\]\-]+(?:\([^(){}]*\))?)\}")

#: Reserved context key holding the run's :class:`GeneratorContext`, if any.
GEN_KEY = "__gen__"


class UnresolvedReferenceError(KeyError):
    """A ``${...}`` reference could not be resolved against the run context."""


def _dig(root: Any, path: str) -> Any:
    """Walk a dotted/indexed path into nested dict/list structures."""
    current = root
    for raw in path.split("."):
        # support trailing list indices like foo[0]
        key, *indices = re.split(r"\[(\d+)\]", raw)
        if key:
            if not isinstance(current, dict) or key not in current:
                raise UnresolvedReferenceError(path)
            current = current[key]
        for idx in (i for i in indices if i != ""):
            if not isinstance(current, (list, tuple)):
                raise UnresolvedReferenceError(path)
            current = current[int(idx)]
    return current


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """Recursively resolve ``${...}`` references inside any JSON-like value.

    ``context`` maps step id -> ``{"response": {...}, "request": {...}}``.

    A whole-string reference (``"${step.response.rrn}"``) is replaced with the
    referenced value preserving its type. A reference embedded in a larger
    string is interpolated as text.
    """
    if isinstance(value, dict):
        return {k: resolve_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, context) for v in value]
    if not isinstance(value, str):
        return value

    gen = context.get(GEN_KEY) if isinstance(context, dict) else None

    def _one(expr: str) -> Any:
        if gen is not None and GeneratorContext.is_generator(expr):
            return gen.resolve(expr)
        return _dig(context, expr)

    whole = VAR_REF_RE.fullmatch(value)
    if whole:
        return _one(whole.group("expr"))

    def _sub(m: re.Match) -> str:
        return str(_one(m.group("expr")))

    return VAR_REF_RE.sub(_sub, value)


def resolve_inputs(rows: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Resolve a node's ``[{name, value}]`` input rows into a ``{name: value}``
    dict, with ``${...}`` references resolved against ``context``. Unresolved
    references become ``None`` rather than raising, so a tool still runs."""
    out: dict[str, Any] = {}
    for row in rows or []:
        name = (row or {}).get("name")
        if not name:
            continue
        try:
            out[name] = resolve_value(row.get("value", ""), context)
        except UnresolvedReferenceError:
            out[name] = None
    return out
