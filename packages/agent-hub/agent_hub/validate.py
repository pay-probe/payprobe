"""Registry validation — the checks that need more than a field's own shape.

Guardrails live in machinery, not prompts (CLAUDE.md invariant #6). Publishing
is the moment a definition becomes runnable, so publish-time validation is
where the registry refuses what the runner must never see:

* a tool name that is not in the one toolkit registry;
* an ``advisor`` agent that lists write/execute tools (advisor = read tier);
* a workflow that escalates an agent beyond its registered mode;
* a ``full``-mode task (or a write/execute ``tool`` node) on a non-mock
  environment that is not *gated*: every path that can reach it must pass
  through an ``approval`` node's ``approved`` edge (ADR-0010 D3; an approval
  that a condition can skip, or one reached on its ``rejected`` edge, gates
  nothing);
* a ``full``-mode agent with an unattended trigger (schedule, event, webhook):
  writes need a human wake or an approved workflow gate, never a timer;
* a reviewer that is the same agent as the executor it reviews (execution
  policy: executor ≠ reviewer).

:func:`widens` compares two versions of an agent so that a holder of the
definition's ``rbac.edit`` cannot grow their own grant (mode, tools, write
scope, rbac, unattended triggers, budget): only an admin publishes a wider
version.

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

#: trigger kinds with no human behind the wake
UNATTENDED_TRIGGERS = {"schedule", "event", "webhook"}


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
    if spec.mode == "full":
        unattended = sorted({t.kind for t in spec.triggers if t.kind in UNATTENDED_TRIGGERS})
        if unattended:
            problems.append(
                "full mode cannot be woken unattended (triggers: "
                + ", ".join(unattended)
                + "); writes need a human wake or an approved workflow gate (ADR-0010 D3)"
            )
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

    # -- edge labels must match what the source node can branch on -----------------
    for e in spec.edges:
        src = by_id.get(e.from_)
        if src is None or e.when is None:
            continue
        label = e.when.lower()
        if src.type == "condition" and label not in ("true", "false"):
            problems.append(f"edge {e.from_} -> {e.to}: a condition branches on 'true'/'false'")
        elif src.type == "approval" and label not in ("approved", "rejected"):
            problems.append(
                f"edge {e.from_} -> {e.to}: an approval branches on 'approved'/'rejected'"
            )
        elif src.type not in ("condition", "approval"):
            problems.append(f"edge {e.from_} -> {e.to}: '{src.type}' nodes take no 'when'")

    # -- D3: full mode outside mock must be gated on every path ----------------
    if not any("cycle" in p for p in problems):
        gated = approval_gated(ids, spec)
        for n in spec.nodes:
            if n.type == "tool":
                # a direct write/execute call is a full-mode act by another name
                t = toolkit.REGISTRY.get(n.tool or "")
                if t is None or t.tier == "read" or n.environment in MOCK_ENVIRONMENTS:
                    continue
                if not gated[n.id]:
                    problems.append(
                        f"node '{n.id}': tool '{n.tool}' is tier '{t.tier}' and needs an "
                        "approval node on every path to it (ADR-0010 D3)"
                    )
                continue
            if n.type != "agent_task" or n.id not in resolved:
                continue
            eff = n.mode or resolved[n.id][2].mode
            if eff != "full":
                continue
            if n.environment in MOCK_ENVIRONMENTS:
                continue
            if not gated[n.id]:
                problems.append(
                    f"node '{n.id}': full mode"
                    + (f" on '{n.environment}'" if n.environment else "")
                    + " needs an approval node on every path to it (ADR-0010 D3)"
                )
    return problems


def approval_gated(ids: list[str], spec: WorkflowSpec) -> dict[str, bool]:
    """Per node: is every path that can reach it gated by a human approval?

    A path is gated where it crosses an ``approval`` node's ``approved`` edge
    (unlabelled edges out of an approval mean approved). An approval reached
    on its ``rejected`` edge gates nothing (the human said no); a node with no
    incoming edge is a start node, so nothing gates it. The engine applies the
    same rule at run time over the edges that actually fired
    (:meth:`agent_hub.engine.Engine._approved_on_path`)."""
    by_id = {n.id: n for n in spec.nodes}
    preds: dict[str, list] = {i: [] for i in ids}
    for e in spec.edges:
        if e.to != END and e.from_ in preds and e.to in preds:
            preds[e.to].append(e)
    memo: dict[str, bool] = {}

    def walk(n: str, stack: frozenset[str]) -> bool:
        if n in memo:
            return memo[n]
        edges = preds[n]
        ok = bool(edges)
        for e in edges:
            src = by_id[e.from_]
            if src.type == "approval" and (e.when or "approved").lower() == "approved":
                continue  # this path is gated right here
            if e.from_ in stack:  # defensive; cycles were reported above
                ok = False
                break
            if not walk(e.from_, stack | {n}):
                ok = False
                break
        memo[n] = ok
        return ok

    return {i: walk(i, frozenset()) for i in ids}


def widens(prev: dict, new: dict) -> list[str]:
    """How ``new`` grows the grant of ``prev`` (two agent spec dicts), as
    human-readable reasons; empty = ``new`` is no wider. Used at publish time:
    an editor named only in ``rbac.edit`` may not publish a wider version."""
    p, n = AgentSpec.model_validate(prev), AgentSpec.model_validate(new)
    out: list[str] = []
    if MODE_RANK[n.mode] > MODE_RANK[p.mode]:
        out.append(f"mode '{p.mode}' -> '{n.mode}'")
    added = sorted(set(n.tools) - set(p.tools))
    if added:
        out.append("tools added: " + ", ".join(added))
    for field in ("projects", "environments"):
        before, after = getattr(p.write_scope, field), getattr(n.write_scope, field)
        if "*" in before:
            continue
        if "*" in after or set(after) - set(before):
            out.append(f"write_scope.{field} grows")
    if n.rbac.model_dump() != p.rbac.model_dump():
        out.append("rbac changes")
    kinds_before = {t.kind for t in p.triggers}
    new_kinds = sorted({t.kind for t in n.triggers} - kinds_before)
    if new_kinds:
        out.append("trigger kinds added: " + ", ".join(new_kinds))
    if (n.budget.daily_tokens or 0) > (p.budget.daily_tokens or 0) or (
        p.budget.daily_tokens and not n.budget.daily_tokens
    ):
        out.append("daily token budget grows")
    if n.limits.max_tokens > p.limits.max_tokens or n.limits.max_steps > p.limits.max_steps:
        out.append("per-wake limits grow")
    return out
