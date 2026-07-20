"""Network flows — canvas-authored composites of participants and scenarios.

ADR-0004: a *network flow* is the third graph level. Its nodes reference shared
artifacts — ``participant`` nodes point at participant flows, ``scenario`` nodes
at scenarios (traffic initiators) — and its edges are **wiring** ("source sends
traffic to target"), *not* control flow. Starting a network flow launches every
participant node's instances callees-first (derived by topological sort of the
wiring edges) and then fires its autostart initiators — the canvas-authored
successor of :class:`~models.topology.Topology`.

Nodes reuse :class:`~models.scenario.Step` and edges reuse
:class:`~models.scenario.Edge` (``source_port`` is always ``"out"`` here), so the
persistence and editor substrate is shared with scenarios and participant flows.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .scenario import Edge, NodePosition, Step, ValidationIssue, ValidationResult

#: Node kinds a network-flow canvas may place. Disjoint from both the scenario
#: partition (``call``/``init``) and the participant-flow partition
#: (``trigger``/``reply``/``state``/``relay``).
#:
#: - ``participant`` — a participant flow to run (× instances).
#: - ``scenario`` — a traffic initiator, fired after the listeners are up.
#: - ``simulator`` — a saved simulator brought up with the network (and stopped
#:   with it, if this run started it). Config: ``{simulator_id: str}``.
#: - ``group`` — a participant group as a passive wiring target (nothing to
#:   launch; its members are resolved by the flows that call it). Config:
#:   ``{group_id: str}``.
NETWORK_NODE_KINDS = ("participant", "scenario", "simulator", "group")

#: A simulator only ever *receives* traffic — an edge may point at it but never
#: leave it. A ``group`` may additionally fan out to the participants whose
#: listeners its member connections reach (switch → group → issuers), which is
#: how the auto-wiring draws a fleet; it still can't feed anything else.
TARGET_ONLY_KINDS = frozenset({"simulator"})


class NetworkFlowDraft(BaseModel):
    """Editable fields of a network flow.

    Node configs:

    - ``participant`` — ``{flow_id: str, instances?: int>=1, port?: int}``
      (mirrors ``TopologyParticipant`` + optional explicit base port).
    - ``scenario`` — ``{scenario_id: str, environment?: str, autostart?: bool}``
      (generalises ``TopologyInitiator`` from 0..1 to N drivers).
    """

    name: str
    description: str = ""
    #: Graph nodes (kinds restricted to ``NETWORK_NODE_KINDS``). When there are
    #: no edges, node list order doubles as start order (callees first) — this
    #: is exactly the legacy Topology contract, which migration preserves.
    nodes: list[Step] = Field(default_factory=list)
    #: Wiring: ``source`` sends traffic to ``target`` ⇒ target starts first.
    edges: list[Edge] = Field(default_factory=list)
    #: Editor-only canvas positions keyed by node id.
    layout: dict[str, NodePosition] = Field(default_factory=dict)


class NetworkFlow(NetworkFlowDraft):
    id: str
    updated_at: str | None = None


class NetworkFlowPlan(BaseModel):
    """Resolved launch plan: participants in start order, then initiators."""

    #: ``{node_id, flow_id, instances, port}`` in callees-first start order.
    participants: list[dict] = Field(default_factory=list)
    #: ``{node_id, scenario_id, environment, autostart}`` in node-list order.
    initiators: list[dict] = Field(default_factory=list)
    #: ``{node_id, simulator_id}`` in node-list order — saved simulators to
    #: bring up *before* the participants (they are pure callees).
    simulators: list[dict] = Field(default_factory=list)
    #: Non-fatal planning notes (e.g. a wiring cycle broken by list order).
    warnings: list[str] = Field(default_factory=list)


def validate_network_flow(draft: NetworkFlowDraft) -> ValidationResult:
    """Structural validation of a network flow.

    Wiring edges are validated against *nodes and direction*, not output ports:
    every node has a single implicit ``out``. Referential checks against the
    flow/scenario registries stay in the API layer (the model has no store
    access); this covers everything shape-level.
    """
    issues: list[ValidationIssue] = []

    seen: set[str] = set()
    for node in draft.nodes:
        if node.id in seen:
            issues.append(ValidationIssue(
                severity="error", step_id=node.id,
                message=f"duplicate node id '{node.id}'"))
        seen.add(node.id)
        cfg = node.config or {}
        if node.kind == "participant":
            if not str(cfg.get("flow_id", "")).strip():
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message="participant node needs a flow_id"))
            try:
                if int(cfg.get("instances", 1)) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message="participant 'instances' must be a positive integer"))
        elif node.kind == "scenario":
            if not str(cfg.get("scenario_id", "")).strip():
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message="scenario node needs a scenario_id"))
        elif node.kind == "simulator":
            if not str(cfg.get("simulator_id", "")).strip():
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message="simulator node needs a simulator_id (saved simulator)"))
        elif node.kind == "group":
            if not str(cfg.get("group_id", "")).strip():
                issues.append(ValidationIssue(
                    severity="error", step_id=node.id,
                    message="group node needs a group_id (participant group)"))
        else:
            issues.append(ValidationIssue(
                severity="error", step_id=node.id,
                message=(
                    f"node kind '{node.kind}' is not allowed on a network canvas "
                    f"(expected one of {', '.join(NETWORK_NODE_KINDS)})"),
            ))

    by_id = {n.id: n for n in draft.nodes}
    for edge in draft.edges:
        if edge.source not in by_id:
            issues.append(ValidationIssue(
                severity="error",
                message=f"edge from unknown node '{edge.source}'"))
            continue
        if edge.target not in by_id:
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=f"edge to unknown node '{edge.target}'"))
            continue
        if edge.source == edge.target:
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message="a node cannot wire to itself"))
        if by_id[edge.target].kind == "scenario":
            # Traffic flows *from* an initiator into the network, never into it.
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=(
                    f"edge into scenario node '{edge.target}' — initiators drive "
                    "traffic, nothing wires into them"),
            ))
        if by_id[edge.source].kind in TARGET_ONLY_KINDS:
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=(
                    f"edge out of simulator node '{edge.source}' — simulators "
                    "only receive traffic"),
            ))
        if (by_id[edge.source].kind == "group"
                and by_id[edge.target].kind != "participant"):
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=(
                    f"edge from group '{edge.source}' to "
                    f"{by_id[edge.target].kind} '{edge.target}' — a group only "
                    "fans out to the participants its members reach"),
            ))

    if _participant_cycle(draft):
        issues.append(ValidationIssue(
            severity="warning",
            message=(
                "wiring contains a cycle — start order falls back to node list "
                "order for the cyclic part"),
        ))

    if not draft.nodes:
        issues.append(ValidationIssue(
            severity="warning", message="network flow has no nodes"))

    return ValidationResult(
        valid=not any(i.severity == "error" for i in issues), issues=issues)


def _participant_edges(draft: NetworkFlowDraft) -> list[tuple[str, str]]:
    """Start-order dependencies between participant nodes.

    Direct participant→participant wiring counts, and a group hop is collapsed
    into a transitive dependency: ``switch → group → issuer`` means the switch
    sends through the group to the issuer, so the issuer must be listening
    before the switch starts. (Initiator edges never constrain listener start
    order; simulators aren't started by this sort at all.)"""
    kinds = {n.id: n.kind for n in draft.nodes}
    direct = [
        (e.source, e.target) for e in draft.edges
        if kinds.get(e.source) == "participant" and kinds.get(e.target) == "participant"
    ]
    for gid in (n.id for n in draft.nodes if n.kind == "group"):
        callers = [e.source for e in draft.edges
                   if e.target == gid and kinds.get(e.source) == "participant"]
        callees = [e.target for e in draft.edges
                   if e.source == gid and kinds.get(e.target) == "participant"]
        direct.extend((a, b) for a in callers for b in callees if a != b)
    return direct


def _participant_cycle(draft: NetworkFlowDraft) -> bool:
    order, cyclic = _toposort(draft)
    return bool(cyclic)


def _toposort(draft: NetworkFlowDraft) -> tuple[list[str], list[str]]:
    """Kahn's algorithm over the wiring, callees first.

    An edge A→B means "A calls B", so B must be listening before A starts —
    i.e. we sort by *incoming-call* dependencies: a node is ready once every
    node it calls has started. Ties and independent nodes keep node list order
    (stable, and preserves the legacy Topology contract when there are no
    edges). Returns ``(ordered_ids, leftover_cyclic_ids)``; leftovers keep list
    order too.
    """
    participant_ids = [n.id for n in draft.nodes if n.kind == "participant"]
    edges = _participant_edges(draft)
    #: node → set of nodes it calls (must start before it).
    calls: dict[str, set[str]] = {pid: set() for pid in participant_ids}
    for src, dst in edges:
        calls[src].add(dst)

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(participant_ids)
    while remaining:
        ready = [pid for pid in remaining if calls[pid] <= placed]
        if not ready:
            break  # cycle — caller appends leftovers in list order
        for pid in ready:
            ordered.append(pid)
            placed.add(pid)
        remaining = [pid for pid in remaining if pid not in placed]
    return ordered, remaining


def plan_network_flow(draft: NetworkFlowDraft) -> NetworkFlowPlan:
    """Resolve the launch plan: participants callees-first, then initiators."""
    by_id = {n.id: n for n in draft.nodes}
    ordered, cyclic = _toposort(draft)
    warnings: list[str] = []
    if cyclic:
        warnings.append(
            "wiring cycle among: " + ", ".join(cyclic)
            + " — started in node list order")

    participants: list[dict] = []
    for pid in [*ordered, *cyclic]:
        cfg = by_id[pid].config or {}
        participants.append({
            "node_id": pid,
            "flow_id": str(cfg.get("flow_id", "")),
            "instances": max(1, int(cfg.get("instances", 1) or 1)),
            "port": cfg.get("port"),
        })

    initiators: list[dict] = []
    simulators: list[dict] = []
    for node in draft.nodes:
        cfg = node.config or {}
        if node.kind == "scenario":
            initiators.append({
                "node_id": node.id,
                "scenario_id": str(cfg.get("scenario_id", "")),
                "environment": cfg.get("environment"),
                "autostart": bool(cfg.get("autostart", True)),
            })
        elif node.kind == "simulator":
            simulators.append({
                "node_id": node.id,
                "simulator_id": str(cfg.get("simulator_id", "")),
            })
        # ``group`` nodes are passive wiring targets — nothing to launch.

    return NetworkFlowPlan(
        participants=participants, initiators=initiators,
        simulators=simulators, warnings=warnings)
