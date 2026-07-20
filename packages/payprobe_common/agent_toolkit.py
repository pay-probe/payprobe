"""The general assistant's tool layer — registry, guardrails, journal — ONCE.

Both assistant frontends drive the same declarative tool surface:

* **scenario-service** (in-process ``/agent/chat``) plugs in a *stores* backend
  that touches ``app.state`` directly;
* the standalone **payprobe-assistant** service plugs in a *REST* backend that
  calls the PayProbe services over HTTP.

Historically each service carried its own copy of this file and they drifted
(the environments and networks tools each had to be added twice). This module
is the single source of truth: tool names, tiers, JSON schemas, guardrails,
response shapes and the reversible change journal live here; a backend only
implements the ~30 primitive resource operations in :class:`Backend`.

Design notes
------------
* **Tiers.** ``read`` tools never mutate; ``write`` tools do and are journalled.
  ``execute`` tools (ADR-0007) fire ad-hoc traffic at live targets — they are
  NOT journalled because there is nothing to restore (traffic, not config; the
  reversibility invariant applies to configuration writes). A caller can expose
  only the ``read`` tier (advisor mode) via :func:`tools_for`.
* **Guardrails live here, not in the prompt.** The model proposes; this layer is
  the bouncer. Builtins can't be destroyed and a connection still referenced by
  a scenario can't be deleted — enforced regardless of what the model asks.
* **Every write is reversible — from data, not closures.** Each write records
  the *prior state* (``before``) of what it touched. Restoring is a pure
  function of (resource, key, before), so the journal is JSON-serializable,
  persistable (Redis) and replayable by any replica — see :func:`restore_journal`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class GuardrailError(Exception):
    """A write the safety layer refuses regardless of the model's intent."""


class ToolError(Exception):
    """A tool failed for an ordinary reason (not found, invalid args, …)."""


def _without(d: dict, *drop: str) -> dict:
    return {k: v for k, v in (d or {}).items() if k not in drop}


# -- backend interface ---------------------------------------------------------

class Backend(Protocol):
    """The primitive resource operations a deployment supplies.

    Conventions every implementation follows:

    * ``get_*`` returns a plain dict, or ``None`` when the resource doesn't
      exist (backends map their native not-found — a store ``None`` or an
      HTTP 404 — to ``None``).
    * ``put_*``/``create_*`` raise :class:`ToolError` for invalid documents.
    * ``delete_starter_flow`` raises :class:`GuardrailError` for builtins.
    * ``validate_*`` returns ``{"valid": bool, "issues": [...]}`` where an
      issue is a dict with ``severity``/``message`` (or a bare string).
    * All values are JSON-able dicts/lists — never model instances.
    """

    # connections
    def list_connections(self) -> list[dict]: ...
    def get_connection(self, name: str) -> dict | None: ...
    def put_connection(self, name: str, config: dict) -> dict: ...
    def delete_connection(self, name: str) -> None: ...
    # environments (read-only surface)
    def list_environments(self) -> list[dict]: ...
    def get_environment(self, name: str) -> dict | None: ...
    # catalog / formats
    def list_catalog(self) -> list[dict]: ...
    def list_formats(self, protocol: str | None = None) -> list[dict]: ...
    def get_format(self, fid: str) -> dict | None: ...
    # tables
    def list_tables(self) -> list[dict]: ...
    def get_table(self, name: str) -> dict | None: ...
    def put_table(self, name: str, draft: dict) -> dict: ...
    def delete_table(self, name: str) -> None: ...
    # global variables
    def get_global_variables(self) -> dict: ...
    def set_global_variables(self, variables: dict, secrets: list) -> dict: ...
    # starter flows
    def list_starter_flows(self) -> list[dict]: ...
    def get_starter_flow(self, fid: str) -> dict | None: ...
    def create_starter_flow(self, draft: dict, fid: str | None = None) -> dict: ...
    def delete_starter_flow(self, fid: str) -> None: ...
    # scenarios
    def list_scenarios(self, project_id: str | None = None) -> list[dict]: ...
    def get_scenario(self, sid: str) -> dict | None: ...
    def create_scenario(self, draft: dict, project_id: str | None,
                        comment: str) -> dict: ...
    def update_scenario(self, sid: str, draft: dict, comment: str) -> dict: ...
    def delete_scenario(self, sid: str) -> None: ...
    def validate_scenario(self, draft: dict) -> dict: ...
    # networks (network flows, ADR-0004)
    def list_networks(self) -> list[dict]: ...
    def get_network(self, nid: str) -> dict | None: ...
    def put_network(self, nid: str, draft: dict) -> dict: ...
    def delete_network(self, nid: str) -> None: ...
    def validate_network(self, draft: dict) -> dict: ...
    def plan_network(self, nid: str) -> dict | None: ...
    # runtime visibility (orchestrator; read-only — the assistant never
    # starts/stops anything, that stays with the operator)
    def platform_status(self) -> dict: ...
    def list_runs(self) -> list[dict]: ...
    def list_network_runs(self) -> list[dict]: ...
    def list_running_participants(self) -> list[dict]: ...
    def list_running_simulators(self) -> list[dict]: ...
    def list_load_runs(self) -> list[dict]: ...
    # model insights (insight-service, ADR-0005; read-only + advisory —
    # get_* maps a 404 to None like every other resource)
    def get_run_insights(self, run_id: str) -> dict | None: ...
    def list_insight_predictions(
        self, environment: str | None = None) -> dict: ...
    # playground (orchestrator, ADR-0007): ad-hoc execution BY REFERENCE —
    # the orchestrator resolves the target server-side (connection ⊕ override
    # matrix; secrets never round-trip) and echoes masked payloads.
    def playground_targets(self) -> dict: ...
    def playground_execute(self, target: dict, action: str, payload: dict,
                           message_format_id: str | None = None,
                           label: str | None = None) -> dict: ...


# -- change journal (serializable; restore is derived from `before`) ----------

@dataclass
class ChangeRecord:
    seq: int
    tool: str
    resource: str          # "connection", "table", "scenario", "network_flow", …
    key: str               # the name/id touched
    summary: str           # human-readable ("created connection 'switch'")
    before: Any            # prior state (None if the resource didn't exist)
    after: Any = None      # state right after the write (None if deleted);
                           # display-only — restore derives from `before` alone

    def to_dict(self) -> dict:
        return {"seq": self.seq, "tool": self.tool, "resource": self.resource,
                "key": self.key, "summary": self.summary,
                "before": self.before, "after": self.after}


@dataclass
class ChangeJournal:
    """Records reversible writes for one assistant session, as plain data."""

    records: list[ChangeRecord] = field(default_factory=list)
    _seq: int = 0

    def record(self, tool: str, resource: str, key: str, summary: str,
               before: Any) -> ChangeRecord:
        self._seq += 1
        rec = ChangeRecord(self._seq, tool, resource, key, summary, before)
        self.records.append(rec)
        return rec

    def entries(self) -> list[dict]:
        """Journal view for the UI (no ``before`` payload)."""
        return [{"seq": r.seq, "tool": r.tool, "resource": r.resource,
                 "key": r.key, "summary": r.summary} for r in self.records]

    def dump(self) -> list[dict]:
        """Full serializable records (incl. ``before``) for persistence."""
        return [r.to_dict() for r in self.records]

    def revert(self, ctx: "ToolContext") -> int:
        """Undo every recorded change, newest first. Returns the count."""
        n = restore_journal(ctx, self.dump())
        self.records.clear()
        return n


def _quiet(fn: Callable, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — restore is best-effort per record
        pass


def restore_one(ctx: "ToolContext", rec: dict) -> None:
    """Restore a single record so the resource matches its ``before`` state.

    Pure function of the record: if ``before`` is None the resource didn't exist
    (so delete it); otherwise re-create/replace it with the prior value. Works
    for both create/update and delete records."""
    resource, key, before = rec["resource"], rec["key"], rec.get("before")
    b = ctx.backend

    if resource == "variables":  # global vars always exist; restore the snapshot
        b.set_global_variables(before.get("variables", {}),
                               before.get("secrets", []))
    elif resource == "connection":
        if before is None:
            _quiet(b.delete_connection, key)
        else:
            b.put_connection(key, _without(before, "name"))
    elif resource == "table":
        if before is None:
            _quiet(b.delete_table, key)
        else:
            b.put_table(key, _without(before, "name"))
    elif resource == "starter_flow":
        if before is None:
            _quiet(b.delete_starter_flow, key)
        else:
            b.create_starter_flow(
                _without(before, "id", "builtin", "updated_at"), fid=key)
    elif resource == "scenario":
        if before is None:                       # was created → delete it
            _quiet(b.delete_scenario, key)
        else:                                    # was updated → restore prior doc
            b.update_scenario(
                key,
                _without(before, "id", "version", "created_at", "updated_at",
                         "project_id"),
                "revert via assistant")
    elif resource == "network_flow":
        if before is None:
            _quiet(b.delete_network, key)
        else:
            b.put_network(key, _without(before, "id", "updated_at"))


def restore_journal(ctx: "ToolContext", records: list[dict]) -> int:
    """Replay ``records`` newest-first to undo them. Returns the count restored.
    Tolerant: a record that can't be restored is skipped, not fatal."""
    n = 0
    for rec in reversed(records):
        try:
            restore_one(ctx, rec)
            n += 1
        except Exception:  # noqa: BLE001 - one bad record shouldn't block the rest
            continue
    return n


# -- tool context ---------------------------------------------------------------

@dataclass
class ToolContext:
    """What the handlers act on: a :class:`Backend` plus per-session state.

    ``journal`` accumulates undoable writes. ``referenced_connections`` is the
    set of connection names a scenario currently targets — supplied by the
    caller so the delete guardrail can protect in-use connections."""

    backend: Any
    journal: ChangeJournal = field(default_factory=ChangeJournal)
    referenced_connections: set[str] = field(default_factory=set)


# -- tool spec + registry ---------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    tier: str              # "read" | "write"
    description: str
    parameters: dict       # JSON schema (object)
    handler: Callable[[Any, dict], Any]


REGISTRY: dict[str, ToolSpec] = {}


def _tool(name: str, tier: str, description: str, parameters: dict):
    def deco(fn: Callable[[Any, dict], Any]) -> Callable:
        REGISTRY[name] = ToolSpec(name, tier, description, parameters, fn)
        return fn
    return deco


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": True}


_STR = {"type": "string"}


#: every tier: read (never mutates), write (journalled config change),
#: execute (fires ad-hoc traffic; not journalled — nothing to restore).
ALL_TIERS: tuple[str, ...] = ("read", "write", "execute")


def tools_for(tiers: tuple[str, ...] = ALL_TIERS) -> list[ToolSpec]:
    """The tools available for a set of tiers (e.g. ``("read",)`` = advisor)."""
    return [t for t in REGISTRY.values() if t.tier in tiers]


def schemas_for(tiers: tuple[str, ...] = ALL_TIERS) -> list[dict]:
    """Provider-agnostic tool schemas to hand a tool-calling model."""
    return [{"name": t.name, "description": t.description,
             "parameters": t.parameters} for t in tools_for(tiers)]


# How to read a resource's current state, for the display-only `after`
# snapshot on journal records. Restore NEVER uses `after` (invariant: restore
# is a pure function of `before`), so a missing getter only degrades the diff.
_AFTER_GETTERS: dict[str, Callable[["Backend", str], Any]] = {
    "connection": lambda b, k: b.get_connection(k),
    "table": lambda b, k: b.get_table(k),
    "starter_flow": lambda b, k: b.get_starter_flow(k),
    "scenario": lambda b, k: b.get_scenario(k),
    "network_flow": lambda b, k: b.get_network(k),
    "variables": lambda b, k: b.get_global_variables(),
}


def _capture_after(ctx: ToolContext, first_new: int) -> None:
    """Fill ``after`` on journal records appended by the write that just ran."""
    for rec in ctx.journal.records[first_new:]:
        getter = _AFTER_GETTERS.get(rec.resource)
        if getter is None:
            continue
        try:
            rec.after = getter(ctx.backend, rec.key)
        except Exception:  # noqa: BLE001 — display-only, never fail the write
            rec.after = None


def dispatch(ctx: ToolContext, name: str, args: dict | None = None,
             tiers: tuple[str, ...] | None = None) -> dict:
    """Execute one tool call. Always returns a JSON-able envelope; never raises
    for ordinary failures so the agent loop can feed the result back to the
    model and let it recover.

    ``tiers`` (when given) is enforced at EXECUTION time, not just at
    schema-visibility time — a tool outside the allowed tiers is refused even
    if the model somehow calls it (advisor mode defense-in-depth; plan mode
    shows write schemas but must never run them)."""
    args = args or {}
    spec = REGISTRY.get(name)
    if spec is None:
        return {"tool": name, "ok": False,
                "error": f"unknown tool '{name}'", "guardrail": False}
    if tiers is not None and spec.tier not in tiers:
        return {"tool": name, "ok": False, "guardrail": True,
                "error": f"tool '{name}' ({spec.tier}) is not executable in "
                         "this mode — describe the change in your plan "
                         "instead of performing it"}
    n_records = len(ctx.journal.records)
    try:
        result = spec.handler(ctx, args)
        if spec.tier == "write":
            _capture_after(ctx, n_records)
        return {"tool": name, "ok": True, "tier": spec.tier, "result": result}
    except GuardrailError as exc:
        return {"tool": name, "ok": False, "error": str(exc), "guardrail": True}
    except (ToolError, ValueError, KeyError, RuntimeError) as exc:
        return {"tool": name, "ok": False, "error": str(exc), "guardrail": False}


def _issue_errors(check: dict) -> list[str]:
    """Error-severity messages from a ``validate_*`` result (dicts or strings)."""
    out: list[str] = []
    for i in check.get("issues", []):
        if isinstance(i, dict):
            if i.get("severity", "error") == "error":
                out.append(str(i.get("message", i)))
        else:
            out.append(str(i))
    return out


# =============================================================================
# READ tools
# =============================================================================

@_tool("list_connections", "read",
       "List saved adapter connections (named endpoints a scenario targets).",
       _obj({}))
def _list_connections(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_connections()


@_tool("get_connection", "read", "Get one connection by name.",
       _obj({"name": _STR}, ["name"]))
def _get_connection(ctx: ToolContext, args: dict) -> Any:
    conn = ctx.backend.get_connection(args["name"])
    if conn is None:
        raise ToolError(f"no connection '{args['name']}'")
    return conn


@_tool("list_environments", "read",
       "List run environments (adapter-target profiles a run executes "
       "against, e.g. 'mock'). These are NOT connections — an environment is "
       "the named target you pass as environment_name when running a scenario.",
       _obj({}))
def _list_environments(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_environments()


@_tool("get_environment", "read",
       "Get one run environment profile (its adapters + config) by name.",
       _obj({"name": _STR}, ["name"]))
def _get_environment(ctx: ToolContext, args: dict) -> Any:
    env = ctx.backend.get_environment(args["name"])
    if env is None:
        raise ToolError(f"no environment '{args['name']}'")
    return env


@_tool("list_catalog", "read",
       "List the step catalog: adapter targets with their actions/params.",
       _obj({}))
def _list_catalog(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_catalog()


@_tool("list_formats", "read",
       "List registered ISO 8583 / ISO 20022 message formats.",
       _obj({"protocol": _STR}))
def _list_formats(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_formats(args.get("protocol"))


@_tool("get_format", "read", "Get one message format by id.",
       _obj({"fid": _STR}, ["fid"]))
def _get_format(ctx: ToolContext, args: dict) -> Any:
    fmt = ctx.backend.get_format(args["fid"])
    if fmt is None:
        raise ToolError(f"no message format '{args['fid']}'")
    return fmt


@_tool("list_tables", "read", "List saved data tables.", _obj({}))
def _list_tables(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_tables()


@_tool("list_starter_flows", "read",
       "List saved starter flows (editor palette building blocks).", _obj({}))
def _list_starter_flows(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_starter_flows()


@_tool("get_global_variables", "read",
       "Get global variables (and which names are secret).", _obj({}))
def _get_global_variables(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.get_global_variables()


@_tool("list_scenarios", "read",
       "List saved scenarios (summaries). Optionally filter by project.",
       _obj({"project_id": _STR}))
def _list_scenarios(ctx: ToolContext, args: dict) -> Any:
    return [_scenario_summary(s) for s in ctx.backend.list_scenarios(args.get("project_id"))]


@_tool("get_scenario", "read", "Get a full scenario document by id.",
       _obj({"scenario_id": _STR}, ["scenario_id"]))
def _get_scenario(ctx: ToolContext, args: dict) -> Any:
    sc = ctx.backend.get_scenario(args["scenario_id"])
    if sc is None:
        raise ToolError(f"no scenario '{args['scenario_id']}'")
    return sc


@_tool("validate_scenario", "read",
       "Validate a scenario draft (structure + references) without saving. "
       "`scenario` is a ScenarioDraft (name, steps[{id,target,action,...}], …).",
       _obj({"scenario": {"type": "object"}}, ["scenario"]))
def _validate_scenario(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.validate_scenario(args.get("scenario") or {})


# -- networks (canvas-authored composites of the simulated network) ------------

def _network_summary(d: dict | None) -> dict:
    d = d or {}
    kinds: dict[str, int] = {}
    for n in d.get("nodes") or []:
        kinds[n.get("kind", "?")] = kinds.get(n.get("kind", "?"), 0) + 1
    return {"id": d.get("id"), "name": d.get("name"),
            "description": d.get("description", ""),
            "nodes": kinds, "edges": len(d.get("edges") or []),
            "updated_at": d.get("updated_at")}


@_tool("list_networks", "read",
       "List saved networks (network flows) — canvas-authored composites of "
       "the simulated payment network: participant flows × instances, "
       "simulators, participant groups and traffic-driver scenarios, wired "
       "with 'sends traffic to' edges. NOT participant flows themselves.",
       _obj({}))
def _list_networks(ctx: ToolContext, args: dict) -> Any:
    return [_network_summary(d) for d in ctx.backend.list_networks()]


@_tool("get_network", "read",
       "Get one network (network flow) by id: nodes, wiring edges, layout.",
       _obj({"network_id": _STR}, ["network_id"]))
def _get_network(ctx: ToolContext, args: dict) -> Any:
    net = ctx.backend.get_network(args["network_id"])
    if net is None:
        raise ToolError(f"no network '{args['network_id']}'")
    return net


@_tool("plan_network", "read",
       "The resolved launch plan of a network: participants in callees-first "
       "start order (topological sort of the wiring), plus simulators (started "
       "first) and initiators (fired last). Dry-run — nothing starts.",
       _obj({"network_id": _STR}, ["network_id"]))
def _plan_network(ctx: ToolContext, args: dict) -> Any:
    plan = ctx.backend.plan_network(args["network_id"])
    if plan is None:
        raise ToolError(f"no network '{args['network_id']}'")
    return plan


@_tool("validate_network", "read",
       "Validate a network draft (node configs, wiring direction, cycles, "
       "dangling flow/group references) without saving. `network` is a "
       "NetworkFlowDraft: {name, nodes:[{id, kind: participant|scenario|"
       "simulator|group, config}], edges:[{source,target}]}.",
       _obj({"network": {"type": "object"}}, ["network"]))
def _validate_network(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.validate_network(args.get("network") or {})


# -- runtime visibility (read-only over the orchestrator) ----------------------

@_tool("platform_status", "read",
       "Live platform status: which PayProbe services are up (scenario-service, "
       "orchestrator, Redis, auth, MCP, assistant, …) plus headline counts. THE "
       "tool for 'what is running / is the platform healthy' questions.",
       _obj({}))
def _platform_status(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.platform_status()


@_tool("list_runs", "read",
       "Recent scenario-run history (newest first): id, label, status, "
       "pass/fail verdict. Read-only — this does NOT start runs.",
       _obj({"limit": {"type": "integer"}}))
def _list_runs(ctx: ToolContext, args: dict) -> Any:
    rows = ctx.backend.list_runs()
    limit = int(args.get("limit") or 20)
    return rows[:limit]


@_tool("list_network_runs", "read",
       "Live network runs (started networks / simulated payment networks): "
       "per-run readiness health (live/total/ready) and every instance's bound "
       "endpoint. THE tool for 'which networks are running right now'.",
       _obj({}))
def _list_network_runs(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_network_runs()


@_tool("list_running_participants", "read",
       "Running participant-flow listeners: bound endpoint, owner (standalone "
       "vs a network run), protocol, message count.",
       _obj({}))
def _list_running_participants(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_running_participants()


@_tool("list_running_simulators", "read",
       "Live simulators (payShield HSM, VISA, CyberSource, …): port, protocol, "
       "received count, chaos state.",
       _obj({}))
def _list_running_simulators(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_running_simulators()


@_tool("list_load_runs", "read",
       "Load/stress-run history (newest first) with headline metrics "
       "(tps / p95 / success rate / duration).",
       _obj({"limit": {"type": "integer"}}))
def _list_load_runs(ctx: ToolContext, args: dict) -> Any:
    rows = ctx.backend.list_load_runs()
    limit = int(args.get("limit") or 20)
    return rows[:limit]


# -- model insights (insight-service, ADR-0005 — advisory, read-only) ----------

@_tool("get_run_insights", "read",
       "ADVISORY model insights for one run's failures: each failed step "
       "categorized (heuristic taxonomy + learned clusters; `novel` marks "
       "shapes no rule anticipated) with an evidence-pack `explanation` — the "
       "failure's place in the scenario's history, prior identical-signature "
       "failures, whether the scenario later recovered, and a suggested fix. "
       "THE tool for 'why did this run fail?'. A model OPINION: use it to "
       "triage and explain, never as a verdict — gates stay deterministic.",
       _obj({"run_id": _STR}, ["run_id"]))
def _get_run_insights(ctx: ToolContext, args: dict) -> Any:
    out = ctx.backend.get_run_insights(args["run_id"])
    if out is None:
        raise ToolError(f"no run '{args['run_id']}' (or no insights for it)")
    return out


@_tool("list_insight_predictions", "read",
       "ADVISORY per-scenario outcome predictions, riskiest first: "
       "`p_fail_next` / `p_flaky` estimated from run history, with "
       "`n_history` and `top_factors` so thin evidence is visible. Good for "
       "'what should we re-run first / what is likely to break tonight'; "
       "never a reason to skip a run.",
       _obj({"environment": _STR}))
def _list_insight_predictions(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.list_insight_predictions(
        args.get("environment") or None)


# =============================================================================
# WRITE tools (journalled, guard-railed)
# =============================================================================

@_tool("upsert_connection", "write",
       "Create or update an adapter connection. `config` holds the connection "
       "fields (adapter, protocol, host, port, …).",
       _obj({"name": _STR, "config": {"type": "object"}}, ["name", "config"]))
def _upsert_connection(ctx: ToolContext, args: dict) -> Any:
    name = args["name"]
    before = ctx.backend.get_connection(name)
    saved = ctx.backend.put_connection(name, args.get("config") or {})
    verb = "updated" if before else "created"
    ctx.journal.record("upsert_connection", "connection", name,
                       f"{verb} connection '{name}'", before)
    return {"saved": saved, "action": verb}


@_tool("delete_connection", "write",
       "Delete an adapter connection. Refused if a scenario still references it.",
       _obj({"name": _STR}, ["name"]))
def _delete_connection(ctx: ToolContext, args: dict) -> Any:
    name = args["name"]
    if name in ctx.referenced_connections:
        raise GuardrailError(
            f"connection '{name}' is still referenced by a scenario — "
            "repoint or remove those steps first")
    before = ctx.backend.get_connection(name)
    if before is None:
        raise ToolError(f"no connection '{name}'")
    ctx.backend.delete_connection(name)
    ctx.journal.record("delete_connection", "connection", name,
                       f"deleted connection '{name}'", before)
    return {"deleted": name}


@_tool("save_table", "write",
       "Create or replace a data table. `draft` is a DataTableDraft (rows, …).",
       _obj({"name": _STR, "draft": {"type": "object"}}, ["name", "draft"]))
def _save_table(ctx: ToolContext, args: dict) -> Any:
    name = args["name"]
    before = ctx.backend.get_table(name)
    saved = ctx.backend.put_table(name, args.get("draft") or {})
    verb = "updated" if before else "created"
    ctx.journal.record("save_table", "table", name, f"{verb} table '{name}'", before)
    return {"saved": saved, "action": verb}


@_tool("delete_table", "write", "Delete a data table.",
       _obj({"name": _STR}, ["name"]))
def _delete_table(ctx: ToolContext, args: dict) -> Any:
    name = args["name"]
    before = ctx.backend.get_table(name)
    if before is None:
        raise ToolError(f"no table '{name}'")
    ctx.backend.delete_table(name)
    ctx.journal.record("delete_table", "table", name, f"deleted table '{name}'", before)
    return {"deleted": name}


@_tool("set_global_variables", "write",
       "Replace global variables. `secrets` lists names whose values are secret.",
       _obj({"variables": {"type": "object"},
             "secrets": {"type": "array", "items": _STR}}, ["variables"]))
def _set_global_variables(ctx: ToolContext, args: dict) -> Any:
    before = ctx.backend.get_global_variables()
    saved = ctx.backend.set_global_variables(
        args.get("variables") or {}, args.get("secrets") or [])
    ctx.journal.record("set_global_variables", "variables", "global",
                       "replaced global variables", before)
    return saved


@_tool("create_starter_flow", "write",
       "Create a starter flow. `draft` is a StarterFlowDraft (label, steps, …).",
       _obj({"draft": {"type": "object"}}, ["draft"]))
def _create_starter_flow(ctx: ToolContext, args: dict) -> Any:
    created = ctx.backend.create_starter_flow(args.get("draft") or {})
    fid = created.get("id")
    ctx.journal.record("create_starter_flow", "starter_flow", fid,
                       f"created starter flow '{fid}'", None)
    return {"saved": created}


@_tool("delete_starter_flow", "write",
       "Delete a starter flow. Builtins are protected.",
       _obj({"fid": _STR}, ["fid"]))
def _delete_starter_flow(ctx: ToolContext, args: dict) -> Any:
    fid = args["fid"]
    before = ctx.backend.get_starter_flow(fid)
    if before is None:
        raise ToolError(f"no starter flow '{fid}'")
    ctx.backend.delete_starter_flow(fid)   # GuardrailError for builtins
    ctx.journal.record("delete_starter_flow", "starter_flow", fid,
                       f"deleted starter flow '{fid}'", before)
    return {"deleted": fid}


# -- scenarios -------------------------------------------------------------------

def _scenario_summary(d: dict | None) -> dict:
    d = d or {}
    steps = d.get("steps")
    return {"id": d.get("id"), "name": d.get("name"),
            "version": d.get("version"),
            "steps": len(steps) if steps is not None else d.get("step_count", 0),
            "project_id": d.get("project_id")}


def _validated_scenario(ctx: ToolContext, args: dict) -> dict:
    draft = args.get("scenario") or {}
    result = ctx.backend.validate_scenario(draft)
    if not result.get("valid", True):
        issues = "; ".join(str(i) for i in result.get("issues", []))
        raise ToolError(f"scenario is invalid: {issues}")
    return draft


@_tool("create_scenario", "write",
       "Create and save a new scenario. `scenario` is a ScenarioDraft; it is "
       "validated first and rejected if invalid. Optionally place it in "
       "`project_id`.",
       _obj({"scenario": {"type": "object"}, "project_id": _STR, "comment": _STR},
            ["scenario"]))
def _create_scenario(ctx: ToolContext, args: dict) -> Any:
    draft = _validated_scenario(ctx, args)
    created = ctx.backend.create_scenario(
        draft, args.get("project_id"),
        args.get("comment") or "created via assistant")
    ctx.journal.record("create_scenario", "scenario", created.get("id"),
                       f"created scenario '{created.get('name')}'", None)
    return _scenario_summary(created)


@_tool("update_scenario", "write",
       "Replace an existing scenario with a new (validated) draft.",
       _obj({"scenario_id": _STR, "scenario": {"type": "object"}, "comment": _STR},
            ["scenario_id", "scenario"]))
def _update_scenario(ctx: ToolContext, args: dict) -> Any:
    sid = args["scenario_id"]
    before = ctx.backend.get_scenario(sid)
    if before is None:
        raise ToolError(f"no scenario '{sid}'")
    draft = _validated_scenario(ctx, args)
    updated = ctx.backend.update_scenario(
        sid, draft, args.get("comment") or "updated via assistant")
    ctx.journal.record("update_scenario", "scenario", sid,
                       f"updated scenario '{updated.get('name')}'", before)
    return _scenario_summary(updated)


# -- networks ---------------------------------------------------------------------

@_tool("save_network", "write",
       "Create or replace a network (network flow). `network` is a "
       "NetworkFlowDraft; it is validated (incl. participant-flow/group "
       "references) before saving. Edges mean 'sends traffic to' — callees "
       "start first. Start/stop stays with the operator (portal or API).",
       _obj({"network_id": _STR, "network": {"type": "object"}},
            ["network_id", "network"]))
def _save_network(ctx: ToolContext, args: dict) -> Any:
    nid = args["network_id"]
    net = args.get("network") or {}
    errors = _issue_errors(ctx.backend.validate_network(net))
    if errors:
        raise ToolError("network is invalid: " + "; ".join(errors))
    before = ctx.backend.get_network(nid)
    saved = ctx.backend.put_network(nid, net)
    verb = "updated" if before else "created"
    ctx.journal.record("save_network", "network_flow", nid,
                       f"{verb} network '{saved.get('name')}'", before)
    return {"saved": _network_summary(saved), "action": verb}


@_tool("delete_network", "write",
       "Delete a network (network flow). Does not touch the participant "
       "flows / scenarios / simulators it referenced.",
       _obj({"network_id": _STR}, ["network_id"]))
def _delete_network(ctx: ToolContext, args: dict) -> Any:
    nid = args["network_id"]
    before = ctx.backend.get_network(nid)
    if before is None:
        raise ToolError(f"no network '{nid}'")
    ctx.backend.delete_network(nid)
    ctx.journal.record("delete_network", "network_flow", nid,
                       f"deleted network '{nid}'", before)
    return {"deleted": nid}


# =============================================================================
# EXECUTE tools (playground, ADR-0007 — ad-hoc traffic; never journalled)
# =============================================================================

@_tool("playground_targets", "read",
       "Everything callable ad-hoc RIGHT NOW, merged from the live registries: "
       "registered connections (× their override-matrix environments), running "
       "simulators, network participants (incl. port-less NATS subjects), "
       "participant groups, and local function families (crypto ops). Also "
       "carries `samples`: ready-made example interactions per element family "
       "(samples.wire[protocol-or-adapter] for wire targets, "
       "samples.functions.crypto per operation) — start from one instead of "
       "hand-building a payload. Read this before playground_execute to pick "
       "a target by reference.",
       _obj({}))
def _playground_targets(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.playground_targets()


@_tool("playground_execute", "execute",
       "Fire ONE ad-hoc message/command at a target BY REFERENCE — no scenario "
       "authoring needed. `target` is {kind: connection|group|simulator|"
       "participant|raw|function, id, environment?, host?, port?, protocol?}. "
       "The platform resolves the reference server-side (connection ⊕ override "
       "matrix; secrets never leave the server) and delegates to the worker "
       "engine; echoed request/response payloads come back MASKED. "
       "`message_format_id` pins the ISO 8583 dialect. This sends REAL traffic "
       "to the resolved endpoint (and inflates a running simulator's stats — "
       "hits are tagged); it never starts or stops anything.",
       _obj({"target": {"type": "object"}, "action": _STR,
             "payload": {"type": "object"}, "message_format_id": _STR,
             "label": _STR},
            ["target", "action"]))
def _playground_execute(ctx: ToolContext, args: dict) -> Any:
    return ctx.backend.playground_execute(
        args.get("target") or {}, args["action"], args.get("payload") or {},
        args.get("message_format_id"), args.get("label"))
