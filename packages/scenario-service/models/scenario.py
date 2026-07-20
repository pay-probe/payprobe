"""Scenario document models and validation.

The document shape matches examples/scenarios/*.json — the format the worker
engine executes. Validation covers structural rules the schema alone cannot
express, most importantly inter-step variable references like
``${step_003.response.rrn}``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Operator = Literal[
    "eq", "ne", "lt", "lte", "gt", "gte", "present", "absent", "contains", "matches"
]

#: Operators that do not take an ``expected`` value.
UNARY_OPERATORS = {"present", "absent"}

#: Node kinds the visual constructor can place on the canvas. ``action`` is the
#: original step (call an adapter); ``code`` runs a custom Python/TypeScript
#: snippet; ``http`` performs an outbound HTTP request; the rest are n8n-style
#: control-flow nodes.
NodeKind = Literal[
    "action", "if", "switch", "loop", "wait", "merge", "code", "http", "init",
    "crypto", "call",
    # Participant-flow entry/exit kinds (reuse the Step shape). Scenarios never
    # use these; a flow's graph is trigger → logic → reply.
    "trigger", "reply",
    # Participant-flow stateful node: mutate the flow instance's shared state
    # (set/incr/append), readable elsewhere as ${state.*}.
    "state",
    # Participant-flow relay/proxy node: forward every inbound frame to an
    # upstream connection (or group) byte-exact — turns the flow into a proxy.
    "relay",
    # Network-flow node kinds (ADR-0004): a network canvas places *references*
    # to shared artifacts — a participant flow to run, a scenario to drive
    # traffic, a saved simulator to bring up, a participant group as a wiring
    # target. Never valid inside scenarios or participant flows; see
    # models.network_flow.NETWORK_NODE_KINDS.
    "participant", "scenario", "simulator", "group",
]

#: Kinds that only exist on a network-flow canvas (kept in sync with
#: models.network_flow.NETWORK_NODE_KINDS; duplicated here to avoid a cycle).
NETWORK_ONLY_KINDS = frozenset({"participant", "scenario", "simulator", "group"})

#: A node kind is "non-action" when it is not a plain adapter call. These take
#: the ``config``-driven path through validation and the worker.
CONTROL_KINDS = {
    "if", "switch", "loop", "wait", "merge", "code", "http", "init", "crypto",
    "call",
}

#: Operations a ``crypto`` node may perform.
CRYPTO_OPERATIONS = (
    "des", "kcv", "pin_block_encode", "pin_block_decode",
    "retail_mac", "cvv", "arqc", "arpc", "emv_icc_mk", "emv_session_key",
)

#: Languages a ``code`` node may be authored in.
CODE_LANGUAGES = ("python", "typescript", "javascript")

#: HTTP methods an ``http`` node may use.
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

VAR_REF_RE = re.compile(r"\$\{(?P<step>[A-Za-z0-9_-]+)\.(?P<path>[A-Za-z0-9_.\[\]-]+)\}")

STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Reference prefixes that are runtime-injected, not prior steps — references
#: into these must NOT be flagged as "does not resolve to any step". Kept in
#: sync with the worker's generator namespaces (worker/engine/generators.py
#: ``GENERATOR_NAMESPACES``) plus ``loop`` (loop body) and ``vars`` (scoped
#: variables) and ``tables`` (table lookups).
NON_STEP_REF_PREFIXES = frozenset(
    {"loop", "vars", "tables", "rand", "seq", "uuid", "now", "pool", "card"}
)


class Assertion(BaseModel):
    field: str
    operator: Operator
    expected: Any | None = None


class Step(BaseModel):
    """A node in the scenario graph.

    ``kind`` discriminates plain adapter actions from control-flow nodes.
    ``target``/``action``/``payload``/``assertions`` carry the adapter call for
    ``action`` nodes; ``config`` carries the control parameters (condition,
    cases, loop mode, …) for control nodes. Both default to empty so a control
    node and an action node share one schema, keeping persistence simple.
    """

    id: str
    kind: NodeKind = "action"
    target: str = ""
    action: str = ""
    #: Usually a dict; a flow's reply node may instead be a whole-string
    #: reference (e.g. ``"${fwd.response}"``) that resolves to the full
    #: response object at run time.
    payload: dict[str, Any] | str = Field(default_factory=dict)
    assertions: list[Assertion] = Field(default_factory=list)
    #: Control-flow parameters; shape depends on ``kind`` (see ``node_output_ports``).
    config: dict[str, Any] = Field(default_factory=dict)
    #: Optional per-step environment override. When set to a named environment,
    #: this ``action`` step runs against that environment's adapter config instead
    #: of the run's default environment. Resolution happens in the orchestrator at
    #: run-build time (see ``_attach_step_environments``); ``None`` means inherit
    #: the run/scenario default.
    environment_override: str | None = None

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not STEP_ID_RE.match(v):
            raise ValueError("step id may only contain letters, digits, '_' and '-'")
        return v


class Edge(BaseModel):
    """A directed connection between two nodes.

    ``source_port`` selects which output of the source node the wire leaves
    from (``out`` for actions; ``true``/``false`` for ``if``; ``case_N``/
    ``default`` for ``switch``; ``loop``/``done`` for ``loop``).
    """

    source: str
    source_port: str = "out"
    target: str


def node_output_ports(step: Step) -> list[str]:
    """The output port ids a node exposes, given its kind and config."""
    kind = step.kind
    if kind == "if":
        return ["true", "false"]
    if kind == "loop":
        return ["loop", "done"]
    if kind == "switch":
        cases = step.config.get("cases") or []
        return [f"case_{i}" for i in range(len(cases))] + ["default"]
    # action, wait, merge
    return ["out"]


class NodePosition(BaseModel):
    """Canvas position of a step's node in the visual constructor."""

    x: float
    y: float


#: Pipeline-aligned classification (see spec §9: three-phase execution).
TestClass = Literal["component", "integration", "e2e"]

TEST_CLASSES: tuple[TestClass, ...] = ("component", "integration", "e2e")


class ScenarioDraft(BaseModel):
    """Client-supplied scenario content (no server-managed fields)."""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    #: Presentation metadata for the constructor list: an optional category
    #: label (e.g. "ISO 8583", "HSM", "NATS"), an accent ``color`` and a
    #: category logo ``icon`` (a ``pp-icon`` name). Advisory only — the worker
    #: ignores them.
    category: str = ""
    color: str = ""
    icon: str = ""
    tags: list[str] = Field(default_factory=list)
    stop_on_failure: bool = True
    timeout_ms: int = Field(default=10_000, ge=1)
    test_class: TestClass = "e2e"
    steps: list[Step] = Field(default_factory=list)
    #: Scenario-scoped variables, referenced anywhere as ``${vars.NAME}``. At run
    #: time these are merged under global/project/set variables (scenario wins).
    variables: dict[str, Any] = Field(default_factory=dict)
    #: Names (subset of ``variables`` keys) whose values are secret — masked in
    #: the UI and redacted from run logs / events.
    secret_vars: list[str] = Field(default_factory=list)
    #: Optional names of saved test-data pools (see scenario-service
    #: ``TestDataStore``). The orchestrator resolves these to inline
    #: ``cards``/``terminals`` lists before a run, feeding the ``${pool.card}`` /
    #: ``${pool.terminal}`` generators. Inline values, if any, take precedence.
    card_pool: str = ""
    terminal_pool: str = ""
    #: Explicit control-flow connections between nodes. When empty, the worker
    #: falls back to running ``steps`` in list order (legacy linear scenarios).
    edges: list[Edge] = Field(default_factory=list)
    #: Editor-only metadata (node positions keyed by step id). Ignored by the
    #: worker; entries for unknown steps are dropped on validation-free paths.
    layout: dict[str, NodePosition] = Field(default_factory=dict)


class Scenario(ScenarioDraft):
    """Stored scenario — draft plus server-managed metadata."""

    id: str
    project_id: str
    version: int
    created_at: datetime
    updated_at: datetime


class ScenarioSummary(BaseModel):
    id: str
    project_id: str
    name: str
    description: str
    category: str = ""
    color: str = ""
    icon: str = ""
    tags: list[str]
    test_class: TestClass
    step_count: int
    version: int
    updated_at: datetime


# -- organisation: projects and named sets -----------------------------------


class ProjectDraft(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    #: Presentation metadata for the constructor list — a category label plus
    #: an accent ``color`` and a category logo ``icon`` (a ``pp-icon`` name).
    category: str = ""
    color: str = ""
    icon: str = ""


class Project(ProjectDraft):
    id: str
    scenario_count: int = 0
    set_count: int = 0
    created_at: datetime
    updated_at: datetime


class ScenarioSetDraft(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    scenario_ids: list[str] = Field(default_factory=list)


class ScenarioSet(ScenarioSetDraft):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime


class SearchResults(BaseModel):
    """Mixed, typed results for the global search box."""

    query: str
    projects: list[Project]
    sets: list[ScenarioSet]
    scenarios: list[ScenarioSummary]


class ScenarioVersionInfo(BaseModel):
    version: int
    created_at: datetime
    comment: str = ""
    step_count: int


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    step_id: str | None = None
    message: str


class ValidationResult(BaseModel):
    valid: bool
    issues: list[ValidationIssue]


def _iter_strings(value: Any):
    """Yield every string nested anywhere inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def _validate_control_node(step: Step, issues: list[ValidationIssue]) -> None:
    """Per-kind sanity checks for a control-flow node's ``config``."""
    cfg = step.config or {}
    if step.kind == "if":
        if not str(cfg.get("left", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="if node needs a 'left' value to compare",
            ))
    elif step.kind == "switch":
        if not str(cfg.get("value", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="switch node needs a 'value' to route on",
            ))
        if not (cfg.get("cases") or []):
            issues.append(ValidationIssue(
                severity="warning", step_id=step.id,
                message="switch node has no cases — every value falls through to default",
            ))
    elif step.kind == "loop":
        mode = cfg.get("mode", "times")
        if mode not in ("times", "forEach", "while"):
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message=f"loop mode '{mode}' is not one of times/forEach/while",
            ))
        if mode == "forEach" and not str(cfg.get("list", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="forEach loop needs a 'list' reference to iterate",
            ))
        if mode == "while" and not (cfg.get("condition") or {}).get("left"):
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="while loop needs a condition",
            ))
    elif step.kind == "wait":
        try:
            float(cfg.get("ms", 0))
        except (TypeError, ValueError):
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="wait node 'ms' must be a number",
            ))
    elif step.kind == "code":
        if not str(cfg.get("code", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="code node needs a code snippet",
            ))
        lang = cfg.get("language", "python")
        if lang not in CODE_LANGUAGES:
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message=f"code node language '{lang}' must be one of "
                        f"{', '.join(CODE_LANGUAGES)}",
            ))
    elif step.kind == "crypto":
        op = cfg.get("operation", "")
        if op not in CRYPTO_OPERATIONS:
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message=f"crypto operation '{op}' must be one of "
                        f"{', '.join(CRYPTO_OPERATIONS)}",
            ))
    elif step.kind == "init":
        raw = cfg.get("data")
        if isinstance(raw, str) and raw.strip():
            import json as _json
            try:
                _json.loads(raw)
            except (ValueError, TypeError):
                issues.append(ValidationIssue(
                    severity="error", step_id=step.id,
                    message="init node 'data' is not valid JSON",
                ))
    elif step.kind == "http":
        if not str(cfg.get("url", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="HTTP node needs a URL",
            ))
        method = str(cfg.get("method", "GET")).upper()
        if method not in HTTP_METHODS:
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message=f"HTTP method '{method}' must be one of "
                        f"{', '.join(HTTP_METHODS)}",
            ))
    elif step.kind == "call":
        if not str(cfg.get("scenario_id", "")).strip():
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message="Call node needs a scenario to run (scenario_id)",
            ))


def validate_scenario(draft: ScenarioDraft) -> ValidationResult:
    """Structural validation beyond the Pydantic schema.

    Rules:
    - step ids must be unique
    - action nodes must name a target and action
    - control nodes must carry the config their kind requires
    - edges must connect existing nodes through valid output ports
    - variable references must point at an existing step; for plain linear
      scenarios (no edges) the reference must point at an *earlier* step, in a
      branching graph the ordering is path-dependent so it is downgraded to a
      warning
    - unary assertion operators must not carry an ``expected`` value,
      all others must
    - empty scenarios get a warning, not an error (drafts are legal)
    """
    issues: list[ValidationIssue] = []

    seen: set[str] = set()
    for step in draft.steps:
        if step.id in seen:
            issues.append(
                ValidationIssue(
                    severity="error",
                    step_id=step.id,
                    message=f"duplicate step id '{step.id}'",
                )
            )
        seen.add(step.id)
        if step.kind == "action":
            if not step.target or not step.action:
                issues.append(ValidationIssue(
                    severity="error", step_id=step.id,
                    message=f"step '{step.id}' must name a target and action",
                ))
        elif step.kind in NETWORK_ONLY_KINDS:
            issues.append(ValidationIssue(
                severity="error", step_id=step.id,
                message=(
                    f"node kind '{step.kind}' only exists on a network-flow "
                    "canvas, not inside a scenario"),
            ))
        else:
            _validate_control_node(step, issues)

    # -- edges: endpoints exist and leave through a real port --
    has_edges = bool(draft.edges)
    by_id = {s.id: s for s in draft.steps}
    for edge in draft.edges:
        if edge.source not in by_id:
            issues.append(ValidationIssue(
                severity="error",
                message=f"edge from unknown node '{edge.source}'",
            ))
            continue
        if edge.target not in by_id:
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=f"edge to unknown node '{edge.target}'",
            ))
        ports = node_output_ports(by_id[edge.source])
        if edge.source_port not in ports:
            issues.append(ValidationIssue(
                severity="error", step_id=edge.source,
                message=(
                    f"node '{edge.source}' has no output port "
                    f"'{edge.source_port}' (expected one of {', '.join(ports)})"
                ),
            ))

    ref_severity = "warning" if has_edges else "error"
    earlier: set[str] = set()
    for step in draft.steps:
        for text in _iter_strings(step.payload):
            for match in VAR_REF_RE.finditer(text):
                ref = match.group("step")
                if ref == step.id:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            step_id=step.id,
                            message=f"step '{step.id}' references itself",
                        )
                    )
                elif ref in NON_STEP_REF_PREFIXES:
                    # ${loop.*}, ${vars.*}, ${tables.*} and the generator
                    # namespaces (${rand.*}/${seq.*}/${now.*}/${pool.*}/${card.*})
                    # are runtime-injected, not prior steps.
                    continue
                elif ref not in seen:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            step_id=step.id,
                            message=(
                                f"reference '${{{ref}.{match.group('path')}}}' does not "
                                "resolve to any step"
                            ),
                        )
                    )
                elif ref not in earlier:
                    issues.append(
                        ValidationIssue(
                            severity=ref_severity,
                            step_id=step.id,
                            message=(
                                f"reference '${{{ref}.{match.group('path')}}}' does not "
                                "resolve to an earlier step"
                            ),
                        )
                    )
        for assertion in step.assertions:
            if assertion.operator in UNARY_OPERATORS and assertion.expected is not None:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        step_id=step.id,
                        message=(
                            f"assertion on '{assertion.field}': operator "
                            f"'{assertion.operator}' ignores the expected value"
                        ),
                    )
                )
            if assertion.operator not in UNARY_OPERATORS and assertion.expected is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        step_id=step.id,
                        message=(
                            f"assertion on '{assertion.field}': operator "
                            f"'{assertion.operator}' requires an expected value"
                        ),
                    )
                )
        earlier.add(step.id)

    if not draft.steps:
        issues.append(
            ValidationIssue(severity="warning", message="scenario has no steps")
        )

    return ValidationResult(
        valid=not any(i.severity == "error" for i in issues),
        issues=issues,
    )
