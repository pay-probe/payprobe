"""Scenario runner — executes a single scenario.

A scenario is either a *linear* list of steps (legacy: no ``edges``) or a
*graph* of nodes connected by ``edges`` (n8n-style branching and loops). Both
share the same per-step machinery:

- resolve ``${...}`` references against earlier step results
- call the adapter (via the engine-provided step executor, which owns the
  concurrency semaphores)
- run assertions and decide step pass/fail
- honor ``stop_on_failure``
- emit ``step.result`` events and return an aggregate ``ScenarioResult``

The graph executor adds control-flow nodes: ``if`` (true/false), ``switch``
(value → case_N/default), ``loop`` (times/forEach/while, body via the ``loop``
port, continuation via ``done``), ``wait`` (delay) and ``merge`` (join).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator

from ..adapters.base.base_adapter import StepResult
from .code_runner import run_code
from .crypto_tools import run_crypto
from .generators import GeneratorContext
from .http_runner import run_http
from .events import RunEvent, STEP_RESULT, EventSink, NullSink
from .variables import resolve_inputs, resolve_value, UnresolvedReferenceError, GEN_KEY

# Status constants used across engine, persistence and reporting.
PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
ERROR = "error"

# (target, action, payload, assertions) -> StepResult (with assertions evaluated)
StepExecutor = Callable[[str, str, dict, list], Awaitable[StepResult]]

# Runaway guards for graph execution.
NODE_BUDGET = 10_000  # total node visits per scenario before we bail
DEFAULT_MAX_ITERATIONS = 1000  # per-loop iteration cap
MAX_WAIT_MS = 60_000  # clamp on a single wait node
#: Context key a participant flow's ``reply`` node writes its resolved response
#: payload to; the flow driver reads it after the graph walk. Inert in scenarios.
REPLY_KEY = "__reply__"
MAX_CALL_DEPTH = 8  # nested sub-scenario (call) recursion guard


class _StopScenario(Exception):
    """Internal signal to abort a scenario walk (stop_on_failure / budget)."""


def _loose_eq(a: Any, b: Any) -> bool:
    """Equality that tolerates string/number boundary mismatches."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _compare(left: Any, operator: str, right: Any) -> bool:
    """Evaluate a control-flow condition operator (mirrors assertion operators)."""
    if operator == "present":
        return left is not None
    if operator == "absent":
        return left is None
    if operator == "eq":
        return _loose_eq(left, right)
    if operator == "ne":
        return not _loose_eq(left, right)
    if operator in ("lt", "lte", "gt", "gte"):
        try:
            lhs, rhs = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return {
            "lt": lhs < rhs,
            "lte": lhs <= rhs,
            "gt": lhs > rhs,
            "gte": lhs >= rhs,
        }[operator]
    if operator == "contains":
        try:
            return right in left  # type: ignore[operator]
        except TypeError:
            return str(right) in str(left)
    if operator == "matches":
        try:
            return re.search(str(right), str(left)) is not None
        except re.error:
            return False
    return False


def _compact(value: Any, limit: int = 400) -> str:
    """One-line JSON repr of a payload, truncated for the trace log."""
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_trace(
    target: str,
    action: str,
    sr: StepResult,
    status: str,
    environment_override: str | None = None,
) -> list[dict]:
    """Synthesise structured engine-log lines describing one step's execution.

    Built from data the engine already has so every adapter gets a consistent
    process trace without per-adapter wiring. Adapter ``raw_log`` (wire bytes)
    is folded in as ``wire`` lines.
    """
    lines: list[dict] = [
        {"level": "info", "msg": f"dispatch {target} · {action}"},
    ]
    if environment_override:
        lines.append({"level": "info", "msg": f"environment override · {environment_override}"})
    lines.append({"level": "debug", "msg": f"request {_compact(sr.request_payload)}"})
    for wire in (sr.raw_log or "").splitlines():
        if wire.strip():
            lines.append({"level": "wire", "msg": wire})
    lines.append(
        {
            "level": "info" if sr.success else "error",
            "msg": f"response {status} · {sr.duration_ms} ms",
        }
    )
    lines.append({"level": "debug", "msg": f"response {_compact(sr.response_payload)}"})
    for a in sr.assertions:
        lines.append(
            {
                "level": "pass" if a.passed else "fail",
                "msg": f"assert {a.field} {a.operator} {a.expected!r} → {a.actual!r}",
            }
        )
    if sr.error:
        lines.append({"level": "error", "msg": str(sr.error)})
    return lines


@dataclass
class StepOutcome:
    step_id: str
    target: str
    action: str
    status: str
    duration_ms: int
    request: Any = None
    response: Any = None
    assertions: list = field(default_factory=list)
    error: str | None = None
    #: Wall-clock epoch (ms) when the step was dispatched. Lets the trace
    #: viewer lay steps out on a shared timeline / waterfall.
    started_at_ms: int = 0
    #: Adapter-provided wire detail (e.g. ISO 8583 bytes sent/received).
    raw_log: str = ""
    #: Structured engine log lines describing the execution process. Each
    #: entry is ``{"level": str, "msg": str}`` — surfaced in the Trace tab.
    trace: list = field(default_factory=list)
    #: Named environment this step was pinned to via ``environment_override``
    #: (resolved by the orchestrator). ``None`` means it ran on the run default.
    environment_override: str | None = None


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    status: str
    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASSED


def scenario_targets(scenario: dict) -> set[str]:
    """The set of adapter targets a scenario touches (control nodes excluded)."""
    return {
        s["target"]
        for s in scenario.get("steps", [])
        if s.get("kind", "action") == "action" and s.get("target")
    }


class GraphExecutor:
    """Shared graph/node execution core.

    Drives a node+edge graph by node ``kind`` (action/code/http/init/crypto/call
    and control flow), resolves ``${...}`` references, and emits trace events.
    Holds no scenario-verdict assumptions: the scenario driver (``run``) lives in
    :class:`ScenarioRunner`, and the message-triggered flow driver lives in
    :class:`~worker.engine.flow_runner.FlowRunner`. Both reuse the node executors
    and graph walk defined here.
    """

    def __init__(
        self,
        sink: EventSink,
        run_id: str,
        debug_hook: Callable[[str, dict], Awaitable[None]] | None = None,
        secrets: list[str] | None = None,
        depth: int = 0,
        generators: GeneratorContext | None = None,
    ) -> None:
        self.sink = sink
        self.run_id = run_id
        #: Nesting level for sub-scenario (call) nodes — guards infinite recursion.
        self._depth = depth
        #: Per-transaction data source for ``${rand.*}`` / ``${seq.*}`` / ``${pool.*}``
        #: tokens. Shared across transactions on a load shard so counters advance;
        #: a fresh one is created per run when not supplied.
        self._generators = generators
        #: Sub-scenario definitions a ``call`` node can run, keyed by scenario id
        #: (attached by the launcher). Set per-run in ``run``.
        self._subflows: dict[str, dict] = {}
        #: Optional async hook called before each node executes (node_id, context).
        #: Used by the orchestrator to implement breakpoints / step-through.
        self._debug_hook = debug_hook
        #: Secret variable values to redact (as "***") from emitted events and the
        #: stored summary — they stay live in the context for resolution.
        self._secrets = [s for s in (secrets or []) if s]

    def _redact(self, value: Any) -> Any:
        """Deep-copy ``value`` with any secret substrings replaced by ``***``."""
        if not self._secrets:
            return value
        if isinstance(value, str):
            out = value
            for sv in self._secrets:
                if sv in out:
                    out = out.replace(sv, "***")
            return out
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value

    async def _execute_node(
        self,
        step: dict,
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome | None":
        """Run one non-control node by ``kind`` and return its outcome.

        Returns ``None`` for control-flow kinds (if/switch/loop/merge/wait),
        which only make sense inside the graph walk. Shared by the linear and
        graph paths so every node kind is dispatched the same way — the linear
        path no longer assumes every node is an ``action`` with a ``target``.
        """
        kind = step.get("kind", "action")
        if kind == "action":
            return await self._run_step(step, execute_step, context, scenario_id, phase)
        if kind == "code":
            return await self._run_code_node(step, context, scenario_id, phase)
        if kind == "http":
            return await self._run_http_node(step, context, scenario_id, phase)
        if kind == "init":
            return await self._run_init_node(step, context, scenario_id, phase)
        if kind == "crypto":
            return await self._run_crypto_node(step, context, scenario_id, phase)
        if kind == "call":
            return await self._run_call_node(
                step,
                execute_step,
                context,
                scenario_id,
                phase,
            )
        return None

    # -- graph execution -----------------------------------------------------

    async def _run_graph(
        self,
        scenario: dict,
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
        result: "ScenarioResult",
        stop_on_failure: bool,
    ) -> None:
        """Walk the node graph from its entry node, following control flow."""
        nodes: dict[str, dict] = {s["id"]: s for s in scenario.get("steps", [])}
        # adjacency: (source_id, source_port) -> target_id (last wire wins)
        adj: dict[tuple[str, str], str] = {}
        indegree: dict[str, int] = {nid: 0 for nid in nodes}
        for edge in scenario.get("edges", []):
            src, port, dst = edge["source"], edge.get("source_port", "out"), edge["target"]
            adj[(src, port)] = dst
            if dst in indegree:
                indegree[dst] += 1

        start = self._graph_entry(scenario, indegree)
        budget = {"n": 0}
        try:
            await self._walk(
                start,
                nodes,
                adj,
                execute_step,
                context,
                scenario_id,
                phase,
                result,
                stop_on_failure,
                budget,
            )
        except _StopScenario:
            pass

    @staticmethod
    def _graph_entry(scenario: dict, indegree: dict[str, int]) -> str | None:
        """The node to start at: first with no incoming edge, else first step."""
        for step in scenario.get("steps", []):
            if indegree.get(step["id"], 0) == 0:
                return step["id"]
        steps = scenario.get("steps", [])
        return steps[0]["id"] if steps else None

    @staticmethod
    def _next(adj: dict[tuple[str, str], str], node_id: str, port: str) -> str | None:
        return adj.get((node_id, port))

    async def _walk(
        self,
        node_id: str | None,
        nodes: dict[str, dict],
        adj: dict[tuple[str, str], str],
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
        result: "ScenarioResult",
        stop_on_failure: bool,
        budget: dict[str, int],
    ) -> None:
        while node_id is not None:
            budget["n"] += 1
            if budget["n"] > NODE_BUDGET:
                raise _StopScenario()
            step = nodes.get(node_id)
            if step is None:
                return
            if self._debug_hook:
                await self._debug_hook(node_id, context)
            kind = step.get("kind", "action")

            if kind == "action":
                outcome = await self._run_step(
                    step,
                    execute_step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "code":
                outcome = await self._run_code_node(
                    step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "http":
                outcome = await self._run_http_node(
                    step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "init":
                outcome = await self._run_init_node(
                    step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "crypto":
                outcome = await self._run_crypto_node(
                    step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "call":
                outcome = await self._run_call_node(
                    step,
                    execute_step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "wait":
                ms = 0.0
                try:
                    ms = float((step.get("config") or {}).get("ms", 0) or 0)
                except (TypeError, ValueError):
                    ms = 0.0
                if ms > 0:
                    await asyncio.sleep(min(ms, MAX_WAIT_MS) / 1000)
                await self._record_control(step, PASSED, result, scenario_id, phase)
                node_id = self._next(adj, node_id, "out")

            elif kind == "merge":
                await self._record_control(step, PASSED, result, scenario_id, phase)
                node_id = self._next(adj, node_id, "out")

            elif kind == "if":
                taken = self._eval_condition(step.get("config") or {}, context)
                await self._record_control(
                    step,
                    PASSED,
                    result,
                    scenario_id,
                    phase,
                    branch=("true" if taken else "false"),
                )
                node_id = self._next(adj, node_id, "true" if taken else "false")

            elif kind == "switch":
                port = self._switch_port(step, context)
                await self._record_control(
                    step,
                    PASSED,
                    result,
                    scenario_id,
                    phase,
                    branch=port,
                )
                node_id = self._next(adj, node_id, port)

            elif kind == "loop":
                body = self._next(adj, node_id, "loop")
                for index, item in self._loop_iterations(step, context):
                    previous = context.get("loop")
                    context["loop"] = {"index": index, "item": item}
                    if body is not None:
                        await self._walk(
                            body,
                            nodes,
                            adj,
                            execute_step,
                            context,
                            scenario_id,
                            phase,
                            result,
                            stop_on_failure,
                            budget,
                        )
                    if previous is None:
                        context.pop("loop", None)
                    else:
                        context["loop"] = previous
                await self._record_control(step, PASSED, result, scenario_id, phase)
                node_id = self._next(adj, node_id, "done")

            elif kind == "state":
                # Mutate the flow instance's shared state (set / incr / append).
                # Read elsewhere as ${state.*}. The state dict is shared across
                # messages by the responder, so a stand-in can hold balances,
                # sequence counters, sign-on flags, etc.
                self._apply_state(step.get("config") or {}, context)
                await self._record_control(step, PASSED, result, scenario_id, phase)
                node_id = self._next(adj, node_id, "out")

            elif kind in ("relay", "proxy"):
                # Graph-computed relay: forward the flow's inbound request to the
                # relay's upstream connection and expose the upstream's answer as
                # this node's ``${<id>.response}`` (a following reply node can
                # echo or reshape it). A flow whose relay is TERMINAL never gets
                # here — the orchestrator launches it as a transparent byte-level
                # TcpProxy instead of a graph responder.
                outcome = await self._run_relay_node(
                    step,
                    execute_step,
                    context,
                    scenario_id,
                    phase,
                )
                result.steps.append(outcome)
                if outcome.status in (FAILED, ERROR):
                    result.status = FAILED
                    if stop_on_failure:
                        raise _StopScenario()
                node_id = self._next(adj, node_id, "out")

            elif kind == "reply":
                # A participant flow's terminal node: resolve its payload as the
                # response to send back on the inbound connection, stashed under a
                # well-known context key the flow driver reads. Scenarios never
                # contain reply nodes, so this is inert for them.
                try:
                    context[REPLY_KEY] = resolve_value(step.get("payload", {}), context)
                except UnresolvedReferenceError:
                    context[REPLY_KEY] = step.get("payload", {})
                await self._record_control(step, PASSED, result, scenario_id, phase)
                node_id = self._next(adj, node_id, "out")

            else:  # unknown kind (e.g. trigger) — no-op pass-through
                node_id = self._next(adj, node_id, "out")

    # -- control-node evaluation ---------------------------------------------

    @staticmethod
    def _resolve(value: Any, context: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return resolve_value(value, context)
        except UnresolvedReferenceError:
            return None

    def _eval_condition(self, cfg: dict, context: dict[str, Any]) -> bool:
        left = self._resolve(cfg.get("left"), context)
        right = self._resolve(cfg.get("right"), context)
        return _compare(left, cfg.get("operator", "eq"), right)

    def _apply_state(self, cfg: dict, context: dict[str, Any]) -> None:
        """Apply a ``state`` node's ``set`` / ``incr`` / ``append`` directives to
        the flow's shared ``context['state']`` dict (mutated in place)."""
        state = context.setdefault("state", {})
        if not isinstance(state, dict):
            return

        def _val(v: Any) -> Any:
            return self._resolve(v, context) if isinstance(v, str) else v

        for k, v in (cfg.get("set") or {}).items():
            state[k] = _val(v)
        for k, amt in (cfg.get("incr") or {}).items():
            try:
                base = float(state.get(k, 0) or 0)
            except (TypeError, ValueError):
                base = 0.0
            try:
                delta = float(_val(amt))
            except (TypeError, ValueError):
                delta = 0.0
            nv = base + delta
            state[k] = int(nv) if nv == int(nv) else nv
        for k, v in (cfg.get("append") or {}).items():
            lst = state.setdefault(k, [])
            if isinstance(lst, list):
                lst.append(_val(v))

    def _switch_port(self, step: dict, context: dict[str, Any]) -> str:
        cfg = step.get("config") or {}
        value = self._resolve(cfg.get("value"), context)
        for i, case in enumerate(cfg.get("cases") or []):
            if _loose_eq(value, case.get("value")):
                return f"case_{i}"
        return "default"

    def _loop_iterations(
        self,
        step: dict,
        context: dict[str, Any],
    ) -> Iterator[tuple[int, Any]]:
        """Yield (index, item) per iteration, evaluating lazily so that a
        ``while`` condition re-checks against the latest context each pass."""
        cfg = step.get("config") or {}
        mode = cfg.get("mode", "times")
        try:
            max_it = int(
                cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS) or DEFAULT_MAX_ITERATIONS
            )
        except (TypeError, ValueError):
            max_it = DEFAULT_MAX_ITERATIONS

        if mode == "times":
            try:
                n = int(cfg.get("count", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            for i in range(min(n, max_it)):
                yield i, i
        elif mode == "forEach":
            items = self._resolve(cfg.get("list", ""), context)
            if isinstance(items, (list, tuple)):
                for i, item in enumerate(items):
                    if i >= max_it:
                        break
                    yield i, item
        elif mode == "while":
            i = 0
            condition = cfg.get("condition") or {}
            while i < max_it and self._eval_condition(condition, context):
                yield i, i
                i += 1

    async def _run_code_node(
        self,
        step: dict,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome":
        """Execute a custom-code node and expose its return value downstream."""
        cfg = step.get("config") or {}
        language = cfg.get("language", "python")
        start = time.monotonic()
        res = await run_code(
            language,
            cfg.get("code", ""),
            context,
            cfg.get("timeout_ms"),
            inputs=resolve_inputs(cfg.get("inputs"), context),
        )
        duration = int((time.monotonic() - start) * 1000)
        if res.ok:
            context[step["id"]] = {"request": {}, "response": res.output}
            outcome = StepOutcome(
                step_id=step["id"],
                target="code",
                action=language,
                status=PASSED,
                duration_ms=duration,
                response=res.output,
            )
        else:
            outcome = StepOutcome(
                step_id=step["id"],
                target="code",
                action=language,
                status=ERROR,
                duration_ms=duration,
                error=res.error,
            )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _run_init_node(
        self,
        step: dict,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome":
        """Emit a fixed JSON object (with ${...} refs resolved) as this node's
        response — a starting point downstream steps read via ${id.response.*}."""
        cfg = step.get("config") or {}
        raw = cfg.get("data")
        try:
            if isinstance(raw, str):
                obj = json.loads(raw) if raw.strip() else {}
            elif isinstance(raw, (dict, list)):
                obj = raw
            else:
                obj = {}
            resolved = resolve_value(obj, context)
            context[step["id"]] = {"request": {}, "response": resolved}
            outcome = StepOutcome(
                step_id=step["id"],
                target="init",
                action="init",
                status=PASSED,
                duration_ms=0,
                response=resolved,
            )
        except (json.JSONDecodeError, ValueError, UnresolvedReferenceError) as exc:
            outcome = StepOutcome(
                step_id=step["id"],
                target="init",
                action="init",
                status=ERROR,
                duration_ms=0,
                error=f"init data error: {exc}",
            )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _run_crypto_node(
        self,
        step: dict,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome":
        """Run a payment-crypto operation (real algorithms) and expose the result."""
        cfg = step.get("config") or {}
        op = cfg.get("operation", "")
        params: dict[str, Any] = {}
        for key, value in cfg.items():
            if key == "operation":
                continue
            if isinstance(value, str):
                try:
                    params[key] = str(resolve_value(value, context))
                except UnresolvedReferenceError:
                    params[key] = value
            else:
                params[key] = value
        start = time.monotonic()
        res = run_crypto(op, params)
        duration = int((time.monotonic() - start) * 1000)
        if "error" in res:
            outcome = StepOutcome(
                step_id=step["id"],
                target="crypto",
                action=op,
                status=ERROR,
                duration_ms=duration,
                error=res["error"],
            )
        else:
            context[step["id"]] = {"request": {"operation": op}, "response": res}
            outcome = StepOutcome(
                step_id=step["id"],
                target="crypto",
                action=op,
                status=PASSED,
                duration_ms=duration,
                response=res,
            )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _run_call_node(
        self,
        step: dict,
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome":
        """Run another scenario as a sub-flow and expose its per-step outputs.

        The sub-scenario definition is taken from the attached ``subflows`` map.
        It inherits the parent's variables and tables; ``config.variables`` rows
        (resolved against the parent context) override/add to them. The result is
        ``response = {sub_step_id: response}`` so the caller can read
        ``${call_id.response.<sub_step_id>.<field>}``.
        """
        cfg = step.get("config") or {}
        sub_id = cfg.get("scenario_id", "")
        sub = self._subflows.get(sub_id)
        start = time.monotonic()

        def _fail(msg: str) -> "StepOutcome":
            return StepOutcome(
                step_id=step["id"],
                target="call",
                action=sub_id or "?",
                status=ERROR,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=msg,
            )

        if self._depth >= MAX_CALL_DEPTH:
            outcome = _fail(f"max sub-scenario call depth ({MAX_CALL_DEPTH}) exceeded")
            await self._emit(outcome, scenario_id, phase)
            return outcome
        if not sub:
            outcome = _fail(f"sub-scenario '{sub_id}' is not available")
            await self._emit(outcome, scenario_id, phase)
            return outcome

        # parent vars + resolved overrides feed the sub-flow's ${vars.*}
        merged_vars = dict(context.get("vars") or {})
        for row in cfg.get("variables") or []:
            name = (row or {}).get("name")
            if not name:
                continue
            try:
                merged_vars[name] = resolve_value(row.get("value", ""), context)
            except UnresolvedReferenceError:
                merged_vars[name] = None

        sub_scenario = {
            **sub,
            "variables": merged_vars,
            "tables": context.get("tables") or {},
            "subflows": self._subflows,
        }
        nested = ScenarioRunner(
            NullSink(),
            self.run_id,
            secrets=self._secrets,
            depth=self._depth + 1,
            generators=context.get(GEN_KEY),
        )
        sub_result = await nested.run(sub_scenario, execute_step, phase)
        duration = int((time.monotonic() - start) * 1000)

        response = {o.step_id: o.response for o in sub_result.steps}
        status = PASSED if sub_result.status == PASSED else FAILED
        context[step["id"]] = {
            "request": {"scenario_id": sub_id},
            "response": response,
        }
        outcome = StepOutcome(
            step_id=step["id"],
            target="call",
            action=sub.get("name", sub_id),
            status=status,
            duration_ms=duration,
            response=response,
            error=None if status == PASSED else "sub-scenario failed",
        )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _run_http_node(
        self,
        step: dict,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> "StepOutcome":
        """Perform an HTTP request node and expose its response downstream."""
        cfg = step.get("config") or {}
        start = time.monotonic()
        res = await run_http(cfg, context)
        duration = int((time.monotonic() - start) * 1000)
        context[step["id"]] = {"request": res.request, "response": res.response}
        outcome = StepOutcome(
            step_id=step["id"],
            target="http",
            action=str(cfg.get("method", "GET")).upper(),
            status=PASSED if res.ok else FAILED,
            duration_ms=duration,
            request=res.request,
            response=res.response,
            error=res.error,
        )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _record_control(
        self,
        step: dict,
        status: str,
        result: "ScenarioResult",
        scenario_id: str,
        phase: int,
        branch: str | None = None,
    ) -> None:
        kind = step.get("kind", "action")
        outcome = StepOutcome(
            step_id=step["id"],
            target=kind,
            action=kind,
            status=status,
            duration_ms=0,
            response={"branch": branch} if branch else None,
        )
        result.steps.append(outcome)
        await self._emit(outcome, scenario_id, phase)

    @staticmethod
    def _relay_payload(request: Any) -> dict:
        """Shape a flow's inbound request as an outbound adapter payload.

        The ISO 8583 flow view (``{mti, de}``) becomes ``{mti, values}``; a
        host-command view (``{header, command, data}``) forwards command+data
        (the upstream leg mints its own correlation header); anything else is
        forwarded as-is."""
        if not isinstance(request, dict):
            return {}
        if "de" in request or "mti" in request:
            return {"mti": request.get("mti"), "values": dict(request.get("de") or {})}
        if "command" in request or "header" in request:
            return {"command": request.get("command"), "data": request.get("data", "")}
        return dict(request)

    async def _run_relay_node(
        self,
        step: dict,
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> StepOutcome:
        """Forward the inbound request to the relay's upstream connection.

        Unlike ``_run_step`` the payload is the (already-decoded) wire request,
        so it is deliberately NOT run through ``${...}`` resolution — wire data
        must never be interpolated. The upstream's answer lands in the context
        under this node's id, so ``${<id>.response}`` works downstream."""
        step_id = step["id"]
        cfg = step.get("config") or {}
        target = step.get("target") or cfg.get("connection") or ""
        payload = self._relay_payload(context.get("request"))
        started_at_ms = int(time.time() * 1000)
        if not target:
            outcome = StepOutcome(
                step_id=step_id,
                target="relay",
                action="forward",
                status=ERROR,
                duration_ms=0,
                error="relay node has no upstream connection — set one in its config",
                started_at_ms=started_at_ms,
            )
            await self._emit(outcome, scenario_id, phase)
            return outcome
        try:
            sr: StepResult = await execute_step(target, "relay", payload, [])
        except Exception as exc:  # noqa: BLE001 — adapter blew up
            outcome = StepOutcome(
                step_id=step_id,
                target=target,
                action="relay",
                status=ERROR,
                duration_ms=0,
                request=payload,
                error=f"{type(exc).__name__}: {exc}",
                started_at_ms=started_at_ms,
                trace=[
                    {"level": "info", "msg": f"relay → {target}"},
                    {"level": "error", "msg": f"{type(exc).__name__}: {exc}"},
                ],
            )
            await self._emit(outcome, scenario_id, phase)
            return outcome

        # expose the upstream's answer for later nodes (${<id>.response})
        context[step_id] = {
            "request": sr.request_payload,
            "response": sr.response_payload,
        }
        status = PASSED if sr.success else FAILED
        outcome = StepOutcome(
            step_id=step_id,
            target=target,
            action="relay",
            status=status,
            duration_ms=sr.duration_ms,
            request=sr.request_payload,
            response=sr.response_payload,
            error=sr.error,
            started_at_ms=started_at_ms,
            raw_log=sr.raw_log or "",
            trace=_build_trace(target, "relay", sr, status, None),
        )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _run_step(
        self,
        step: dict,
        execute_step: StepExecutor,
        context: dict[str, Any],
        scenario_id: str,
        phase: int,
    ) -> StepOutcome:
        step_id = step["id"]
        target = step["target"]
        action = step["action"]
        # A per-step environment override repoints ``target`` at a derived
        # ``{base}@{env}`` adapter (see orchestrator ``_attach_step_environments``).
        # Dispatch still uses the real ``target``; the report shows the clean base
        # plus the override name as a separate field.
        cfg = step.get("config") or {}
        override_env = cfg.get("_environment_override")
        display_target = target.rsplit("@", 1)[0] if override_env else target

        try:
            payload = resolve_value(step.get("payload", {}), context)
        except UnresolvedReferenceError as exc:
            outcome = StepOutcome(
                step_id=step_id,
                target=display_target,
                action=action,
                status=ERROR,
                duration_ms=0,
                error=f"unresolved reference: {exc}",
                environment_override=override_env,
            )
            await self._emit(outcome, scenario_id, phase)
            return outcome

        # normalise assertion rules to plain dicts for the adapter engine
        assertion_rules = [
            a if isinstance(a, dict) else a.__dict__ for a in step.get("assertions", [])
        ]

        started_at_ms = int(time.time() * 1000)
        try:
            # the executor (engine) holds the adapter instance and evaluates the
            # scenario's assertions against the response via adapter.assert_response
            sr: StepResult = await execute_step(target, action, payload, assertion_rules)
        except Exception as exc:  # adapter blew up
            outcome = StepOutcome(
                step_id=step_id,
                target=display_target,
                action=action,
                status=ERROR,
                duration_ms=0,
                request=payload,
                error=f"{type(exc).__name__}: {exc}",
                started_at_ms=started_at_ms,
                environment_override=override_env,
                trace=[
                    {"level": "info", "msg": f"dispatch {display_target} · {action}"},
                    *(
                        [{"level": "info", "msg": f"environment override · {override_env}"}]
                        if override_env
                        else []
                    ),
                    {"level": "error", "msg": f"{type(exc).__name__}: {exc}"},
                ],
            )
            await self._emit(outcome, scenario_id, phase)
            return outcome

        all_passed = sr.success and all(a.passed for a in sr.assertions)
        # expose response for later steps' variable references
        context[step_id] = {
            "request": sr.request_payload,
            "response": sr.response_payload,
        }

        status = PASSED if all_passed else FAILED
        outcome = StepOutcome(
            step_id=step_id,
            target=display_target,
            action=action,
            status=status,
            duration_ms=sr.duration_ms,
            request=sr.request_payload,
            response=sr.response_payload,
            assertions=[a.__dict__ for a in sr.assertions],
            error=sr.error,
            started_at_ms=started_at_ms,
            raw_log=sr.raw_log or "",
            trace=_build_trace(display_target, action, sr, status, override_env),
            environment_override=override_env,
        )
        await self._emit(outcome, scenario_id, phase)
        return outcome

    async def _emit(self, outcome: StepOutcome, scenario_id: str, phase: int) -> None:
        # Replace the outcome's outward-facing fields with redacted copies so
        # secrets never reach the event stream or the stored summary; the run
        # context (used for ${...} resolution) keeps the live values.
        if self._secrets:
            outcome.request = self._redact(outcome.request)
            outcome.response = self._redact(outcome.response)
            outcome.error = self._redact(outcome.error)
            outcome.assertions = self._redact(outcome.assertions)
            outcome.raw_log = self._redact(outcome.raw_log)
            outcome.trace = self._redact(outcome.trace)
        await self.sink.publish(
            RunEvent(
                type=STEP_RESULT,
                run_id=self.run_id,
                data={
                    "scenario": scenario_id,
                    "phase": phase,
                    "step": outcome.step_id,
                    "target": outcome.target,
                    "action": outcome.action,
                    "status": outcome.status,
                    "duration_ms": outcome.duration_ms,
                    "assertions": outcome.assertions,
                    "error": outcome.error,
                    "environment_override": outcome.environment_override,
                },
            )
        )


class ScenarioRunner(GraphExecutor):
    """Scenario driver over the shared :class:`GraphExecutor` core.

    Seeds the run context (vars/tables/generators, one bound transaction at the
    top level), walks the graph (or the linear step list for edge-less
    scenarios), and accumulates a pass/fail verdict into a :class:`ScenarioResult`.
    """

    async def run(
        self,
        scenario: dict,
        execute_step: StepExecutor,
        phase: int,
    ) -> ScenarioResult:
        scenario_id = scenario.get("id", scenario.get("name", "?"))
        result = ScenarioResult(
            scenario_id=scenario_id,
            name=scenario.get("name", scenario_id),
            status=PASSED,
        )
        stop_on_failure = scenario.get("stop_on_failure", True)
        self._subflows = scenario.get("subflows") or {}
        # context for variable resolution: step_id -> {request, response}.
        # Scoped variables (global/project/set/scenario, already merged by the
        # launcher) are exposed as ${vars.NAME} and to code nodes as context["vars"].
        # Load runs share one seeded generator context across the shard; ordinary
        # runs build a per-scenario one so ${pool.card}/${pool.terminal} resolve
        # from the scenario's (possibly registry-resolved) inline cards/terminals.
        context: dict[str, Any] = {
            "vars": scenario.get("variables") or {},
            "tables": scenario.get("tables") or {},
            GEN_KEY: self._generators
            or GeneratorContext(
                seed=scenario_id,
                terminals=scenario.get("terminals"),
                cards=scenario.get("cards"),
            ),
        }
        # Begin a new transaction so every ${card.*} reference in this scenario
        # describes the same card, while successive transactions (load runs reuse
        # one shared generator) draw successive cards. Only at the top level: a
        # sub-scenario `call` shares the parent's bound card, so it must not
        # rotate it mid-transaction.
        if self._depth == 0:
            context[GEN_KEY].new_transaction()

        if scenario.get("edges"):
            await self._run_graph(
                scenario,
                execute_step,
                context,
                scenario_id,
                phase,
                result,
                stop_on_failure,
            )
            return result

        for step in scenario.get("steps", []):
            if self._debug_hook:
                await self._debug_hook(step["id"], context)
            outcome = await self._execute_node(
                step,
                execute_step,
                context,
                scenario_id,
                phase,
            )
            if outcome is None:
                # A control-flow node (if/switch/loop/merge/wait) needs edges to
                # drive branching; in an edge-less scenario there's nothing to
                # branch, but we still record it (never silently skipped).
                await self._record_control(step, PASSED, result, scenario_id, phase)
                continue
            result.steps.append(outcome)
            if outcome.status in (FAILED, ERROR):
                result.status = FAILED
                if stop_on_failure:
                    break
        return result
