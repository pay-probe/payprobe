"""``${...}`` templating and the condition evaluator for workflows (ADR-0010 phase 3).

Two small, pure pieces the engine leans on:

* :func:`render` substitutes ``${path}`` references inside a node's ``input``
  or ``args`` with values from the run context (``inputs.<name>`` and
  ``<node_id>.<field>``). A string that is exactly one reference yields the
  referenced value itself (so a dict or list can be passed through); mixed
  strings interpolate, JSON-encoding anything that is not already a string.
* :func:`evaluate` decides a ``condition`` node. The expression is parsed with
  :mod:`ast` and walked against a whitelist (comparisons, ``and``/``or``/
  ``not``, ``in``, literals, ``${...}`` references). Nothing is executed:
  no names, calls, attributes or subscripts survive the walk, so an
  expression authored in a workflow spec can only compare data.

Missing paths resolve to ``None`` rather than raising: a workflow that
compares against a field the upstream agent did not produce should route to
its false branch, not crash the run.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_REF = re.compile(r"\$\{\s*([A-Za-z0-9_.-]+)\s*\}")


class ExpressionError(ValueError):
    """The expression is not in the supported subset."""


def lookup(ctx: dict[str, Any], path: str) -> Any:
    """Dotted lookup into nested dicts/lists; ``None`` when any hop is missing."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Substitute ``${path}`` references throughout a JSON-like value."""
    if isinstance(value, str):
        m = _REF.fullmatch(value.strip())
        if m:
            return lookup(ctx, m.group(1))

        def sub(mm: re.Match) -> str:
            v = lookup(ctx, mm.group(1))
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v, default=str)

        return _REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


_LITERALS = {"true": True, "false": False, "null": None, "none": None}

_CMP = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: _num(a) < _num(b),
    ast.LtE: lambda a, b: _num(a) <= _num(b),
    ast.Gt: lambda a, b: _num(a) > _num(b),
    ast.GtE: lambda a, b: _num(a) >= _num(b),
    ast.In: lambda a, b: _contains(b, a),
    ast.NotIn: lambda a, b: not _contains(b, a),
}


def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            return float(v)
        except (TypeError, ValueError) as exc:
            raise ExpressionError(f"cannot order {v!r}") from exc
    return float(v)


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, (list, tuple, set, dict, str)):
        return item in container
    return False


def evaluate(expr: str, ctx: dict[str, Any]) -> bool:
    """Evaluate a condition to a bool. Raises :class:`ExpressionError` for
    anything outside the subset, so a bad expression fails the run loudly
    instead of silently taking a branch."""
    refs: dict[str, Any] = {}

    def bind(m: re.Match) -> str:
        name = f"__ref{len(refs)}"
        refs[name] = lookup(ctx, m.group(1))
        return name

    source = _REF.sub(bind, expr)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"bad condition {expr!r}: {exc.msg}") from exc
    return bool(_walk(tree.body, refs))


def _walk(node: ast.AST, refs: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in refs:
            return refs[node.id]
        if node.id.lower() in _LITERALS:
            return _LITERALS[node.id.lower()]
        raise ExpressionError(f"unknown name {node.id!r} (use ${{path}} references)")
    if isinstance(node, ast.BoolOp):
        vals = [_walk(v, refs) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _walk(node.operand, refs)
    if isinstance(node, ast.Compare):
        left = _walk(node.left, refs)
        for op, comp in zip(node.ops, node.comparators, strict=True):
            fn = _CMP.get(type(op))
            if fn is None:
                raise ExpressionError(f"operator {type(op).__name__} is not supported")
            right = _walk(comp, refs)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_walk(e, refs) for e in node.elts]
    raise ExpressionError(f"{type(node).__name__} is not allowed in a condition")


__all__ = ["ExpressionError", "evaluate", "lookup", "render"]
