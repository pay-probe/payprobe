"""Registry validation — the checks that need more than a field's own shape.

Guardrails live in machinery, not prompts (CLAUDE.md invariant #6). Publishing
is the moment a definition becomes runnable, so publish-time validation is
where the registry refuses what the runner must never see:

* a tool name that is not in the one toolkit registry;
* an ``advisor`` agent that lists write/execute tools (advisor = read tier);
* a workflow that escalates an agent beyond its registered mode;
* a ``full``-mode task on a non-mock environment with no ``approval`` ancestor
  (ADR-0010 D3);
* a reviewer that is the same agent as the executor it reviews (execution
  policy: executor ≠ reviewer).

Every function returns a list of human-readable problems; empty = valid.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable

from payprobe_common import agent_toolkit as toolkit

from .models import END, MODE_RANK, AgentSpec, WorkflowSpec, parse_ref

#: mock-like environment names where full mode needs no approval ancestor
MOCK_ENVIRONMENTS = {"mock"}


def tool_catalog() -> list[dict]:
    """The toolkit's tools as ``{name, tier, description}`` for the portal."""
    return [
        {"name": t.name, "tier": t.tier, "description": t.description}
        for t in toolkit.REGISTRY.values()
    ]


def _model_allowlist() -> set[str] | None:
    raw = os.environ.get("AGENT_HUB_MODEL_ALLOWLIST", "").strip()
    if not raw:
        return None
    return {m.strip() for m in raw.split(",") if m.strip()}


def validate_agent_spec(spec: AgentSpec) -> list[str]:
    problems: list[str] = []
    for name in spec.tools:
        t = toolkit.REGISTRY.get(name)
        if t is None:
            problems.append(f"unknown tool '{name}' (not in the toolkit registry)")
        elif spec.mode == "advisor" and t.tier != "read":
            problems.append(f"advisor mode is read-only; '{name}' is tier '{t.tier}'")
    if spec.mode == "full":
        has_write = any(
            toolkit.REGISTRY[n].tier in ("write", "execute")
            for n in spec.tools
            if n in toolkit.REGISTRY
        )
        if has_write and not (spec.write_scope.projects or spec.write_scope.environments):
            problems.append("full mode with write/execute tools needs a non-empty write_scope")
    allow = _model_allowlist()
    if spec.model and allow is not None and spec.model not in allow:
        problems.append(f"model '{spec.model}' is not in AGENT_HUB_MODEL_ALLOWLIST")
    if not spec.rbac.edit:
        problems.append("rbac.edit must name at least one role")
    return problems


#: resolver: agent reference → (name, version, spec) or None when unresolvable
Resolver = Callable[[str], tuple[str, int, AgentSpec] | None]


def validate_workflow_spec(spec: WorkflowSpec, resolve: Resolver) -> list[str]:
    problems: list[str] = []
    ids = [n.id for n in spec.nodes]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        problems.append(f"duplicate node ids: {', '.join(dup)}")
    known = set(ids)

    # -- edges reference real nodes; build adjacency ---------------------------
    out: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {i: 0 for i in ids}
    for e in spec.edges:
        if e.from_ not in known:
            problems.append(f"edge from unknown node '{e.from_}'")
            continue
        if e.to != END and e.to not in known:
            problems.append(f"edge to unknown node '{e.to}'")
            continue
        out[e.from_].append(e.to)
        if e.to != END:
            indeg[e.to] += 1

    # -- acyclic (Kahn) ---------------------------------------------------------
    if not problems:
        pending = dict(indeg)
        queue = [i for i, d in pending.items() if d == 0]
        seen = 0
        while queue:
            n = queue.pop()
            seen += 1
            for m in out.get(n, []):
                if m == END:
                    continue
                pending[m] -= 1
                if pending[m] == 0:
                    queue.append(m)
        if seen != len(ids):
            problems.append("workflow has a cycle")

    # -- per-node rules that need the registry ---------------------------------
    by_id = {n.id: n for n in spec.nodes}
    resolved: dict[str, tuple[str, int, AgentSpec]] = {}
    for n in spec.nodes:
        if n.type == "tool":
            if n.tool not in toolkit.REGISTRY:
                problems.append(f"node '{n.id}': unknown tool '{n.tool}'")
            continue
        if n.type != "agent_task":
            continue
        assert n.agent is not None
        hit = resolve(n.agent)
        if hit is None:
            name, ver = parse_ref(n.agent)
            what = f"{name}@{ver}" if ver else f"an active version of '{name}'"
            problems.append(f"node '{n.id}': cannot resolve {what}")
            continue
        resolved[n.id] = hit
        _, _, aspec = hit
        if n.mode and MODE_RANK[n.mode] > MODE_RANK[aspec.mode]:
            problems.append(
                f"node '{n.id}': mode '{n.mode}' escalates agent '{n.agent}' "
                f"beyond its registered mode '{aspec.mode}'"
            )
        if n.reviews:
            target = by_id.get(n.reviews)
            if target is None or target.type != "agent_task":
                problems.append(f"node '{n.id}': reviews unknown agent_task '{n.reviews}'")
            elif target.agent and parse_ref(target.agent)[0] == parse_ref(n.agent)[0]:
                problems.append(
                    f"node '{n.id}': reviewer '{n.agent}' is the executor of "
                    f"'{n.reviews}' (executor and reviewer must differ)"
                )

    # -- D3: full mode outside mock needs an approval ancestor -----------------
    if not any("cycle" in p for p in problems):
        ancestors = _ancestors(ids, spec)
        for n in spec.nodes:
            if n.type != "agent_task" or n.id not in resolved:
                continue
            eff = n.mode or resolved[n.id][2].mode
            if eff != "full":
                continue
            if n.environment in MOCK_ENVIRONMENTS:
                continue
            if not any(by_id[a].type == "approval" for a in ancestors[n.id]):
                problems.append(
                    f"node '{n.id}': full mode"
                    + (f" on '{n.environment}'" if n.environment else "")
                    + " needs an approval node before it (ADR-0010 D3)"
                )
    return problems


def _ancestors(ids: list[str], spec: WorkflowSpec) -> dict[str, set[str]]:
    """Transitive predecessors per node (edges only; END is not a node)."""
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in spec.edges:
        if e.to != END and e.from_ in preds and e.to in preds:
            preds[e.to].add(e.from_)
    anc: dict[str, set[str]] = {}

    def walk(n: str, stack: set[str]) -> set[str]:
        if n in anc:
            return anc[n]
        acc: set[str] = set()
        for p in preds[n]:
            if p in stack:  # defensive; cycles were reported above
                continue
            acc.add(p)
            acc |= walk(p, stack | {n})
        anc[n] = acc
        return acc

    for i in ids:
        walk(i, set())
    return anc
