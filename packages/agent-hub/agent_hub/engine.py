"""Workflow engine (ADR-0010 phase 3): a persisted state machine over the DAG.

A run is a row in ``agent_hub_runs`` whose ``node_states`` and ``results`` are
the whole truth: the row is saved after every node that starts or finishes, so
a process that dies mid-run is resumed by :meth:`Engine.reconcile` from the
row alone. Nothing lives only in memory except the per-run asyncio lock.

The human gate is enforced here as well as at publish time (ADR-0010 D3): a
``full``-mode task or a write/execute ``tool`` node outside ``mock`` runs only
when every edge that actually fired into it descends from an ``approval`` that
a human decided ``approved`` in this run. A gate a condition skipped, one
reached on its ``rejected`` edge, or an agent republished as ``full`` after
the workflow was validated therefore never executes a write.

Node semantics:

* ``agent_task``   one heartbeat of the referenced agent (the same code path as
                   ``POST /agents/{name}/wake``), under the node's narrowed mode,
                   with ``input`` rendered from the run context. A plan-mode
                   task that proposed writes leaves a durable plan artifact.
* ``tool``         one ``scoped_dispatch`` under the invoking user's token, the
                   journal kept on the node state.
* ``condition``    :func:`agent_hub.exprs.evaluate`; edges labelled ``when:
                   "true"`` / ``"false"`` route on the value.
* ``approval``     a row in ``agent_hub_approvals``; the run parks as
                   ``waiting`` until a human with one of ``roles`` decides.
                   Unlabelled edges follow ``approved``; ``when: "rejected"``
                   edges take the other path; with none, the run ends
                   ``rejected``.
* ``parallel`` / ``join``  pass-through markers: any node with several
                   predecessors already waits for all of them.

A node becomes ready when every incoming edge is resolved and at least one
fired; if none fired it is ``skipped`` (and its successors resolve through
it). The run is ``done`` when every node is done or skipped, ``failed`` on the
first failed node, ``cancelled`` on request, ``waiting`` while an approval is
pending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from payprobe_common import agent_toolkit as tk
from payprobe_common.crypto import default_box

from .alerts import Alerter, extract_json
from .exprs import ExpressionError, evaluate, render
from .models import END, MODE_RANK, AgentSpec, Edge, Node, WorkflowSpec
from .store import Conflict, NotFound, RegistryStore
from .validate import MOCK_ENVIRONMENTS

log = logging.getLogger(__name__)

ACTIVE = ("running", "waiting")
TERMINAL = ("done", "failed", "cancelled", "rejected")

#: launch one heartbeat (the wake code path); returns the row, which may be a
#: refusal (``paused`` / ``budget_exceeded``) or ``coalesced``
HeartbeatLauncher = Callable[..., Awaitable[dict]]
#: a toolkit backend acting for the run's invoking user
ToolBackend = Callable[[dict], Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_maybe(text: str | None) -> Any:
    """The JSON an agent's final answer carries (bare, fenced, or embedded in
    a markdown report), so ``${node.json.field}`` works on real model output."""
    return extract_json(text)


class Engine:
    def __init__(
        self,
        store: RegistryStore,
        alerts: Alerter,
        *,
        launch_heartbeat: HeartbeatLauncher,
        tool_backend: ToolBackend,
        retry_s: float = 5.0,
    ) -> None:
        self.store = store
        self.alerts = alerts
        self.launch_heartbeat = launch_heartbeat
        self.tool_backend = tool_backend
        self.retry_s = retry_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._ticker: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------------

    def start_ticker(self, interval_s: float = 30.0) -> None:
        """Expire timed-out approvals and nudge active runs periodically."""
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_forever(interval_s))

    async def _tick_forever(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a tick must never kill the ticker
                log.exception("engine tick failed")

    async def tick(self) -> None:
        for ap in await self.store.expire_approvals():
            await self._approval_expired(ap)
        # the restart watchdog, on the tick as well as at startup: a heartbeat
        # orphaned inside its wall clock would otherwise stand until the next
        # restart, and every run waiting on it with it
        for hb in await self.store.reconcile_running():
            self.alerts.emit_for(hb, mode=None)

    def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    async def reconcile(self) -> list[str]:
        """Resume every active run from its row (startup). Running agent tasks
        are re-attached to their heartbeat; deferred nodes retry; then each
        run advances. Returns the run ids touched."""
        touched: list[str] = []
        await self.tick()
        for run in await self.store.active_runs():
            async with self._lock(run["id"]):
                states = run["node_states"]
                for nid, st in states.items():
                    if st.get("status") == "running" and st.get("heartbeat_id"):
                        self._spawn(self._watch_heartbeat(run["id"], nid, st["heartbeat_id"]))
                    elif st.get("status") == "deferred":
                        st["status"] = "pending"
                await self.store.save_run(
                    run["id"], status=run["status"], node_states=states, results=run["results"]
                )
            await self.advance(run["id"])
            touched.append(run["id"])
        return touched

    # -- public operations ------------------------------------------------------------

    async def start(
        self,
        workflow: str,
        version: int,
        spec: WorkflowSpec,
        spec_sha256: str,
        *,
        inputs: dict,
        caller: dict,
    ) -> dict:
        run = await self.store.create_run(
            {
                "id": uuid.uuid4().hex,
                "workflow": workflow,
                "version": version,
                "spec_sha256": spec_sha256,
                "inputs": inputs,
                "node_states": {n.id: {"status": "pending"} for n in spec.nodes},
                "results": {},
                "invoked_by": str(caller.get("sub") or "anonymous"),
                # what the heartbeat launcher needs to tell a human from a
                # service: sub, roles, and the dev/test marker (never svc/static)
                "principal": {
                    "sub": caller.get("sub"),
                    "roles": caller.get("roles") or [],
                    **({"dev": True} if caller.get("dev") else {}),
                },
            }
        )
        await self.advance(run["id"])
        return await self.store.get_run(run["id"])

    async def cancel(self, run_id: str) -> dict:
        await self.store.request_run_cancel(run_id)
        async with self._lock(run_id):
            run = await self.store.get_run(run_id)
            if run["status"] not in ACTIVE:
                return run
            return await self._cancel_locked(run)

    async def decide(self, ap_id: str, decision: str, by: str, note: str = "") -> dict:
        """Record a human decision on a pending approval and move the run on."""
        ap = await self.store.decide_approval(ap_id, decision, by, note)
        async with self._lock(ap["run_id"]):
            run = await self.store.get_run(ap["run_id"])
            if run["status"] not in ACTIVE:
                return ap
            states, results = run["node_states"], run["results"]
            st = states.setdefault(ap["node_id"], {})
            if st.get("approval_id") not in (None, ap["id"]):
                return ap  # a stale approval for a node that has moved on
            self._finish(st, None, "done")
            results[ap["node_id"]] = {"decision": decision, "by": by, "note": note or None}
            status = run["status"]
            error = None
            if decision == "rejected":
                spec = await self._spec(run)
                takes_rejection = any(
                    e.from_ == ap["node_id"] and (e.when or "").lower() == "rejected"
                    for e in spec.edges
                )
                if not takes_rejection:
                    status, error = "rejected", f"approval '{ap['node_id']}' rejected by {by}"
            await self.store.save_run(
                run["id"], status=status, node_states=states, results=results, error=error
            )
        await self.advance(ap["run_id"])
        return ap

    # -- the state machine ------------------------------------------------------------

    async def advance(self, run_id: str) -> dict:
        """Run every node that is ready, until nothing more can move."""
        async with self._lock(run_id):
            run = await self.store.get_run(run_id)
            if run["status"] not in ACTIVE:
                return run
            if run.get("cancel_requested"):
                return await self._cancel_locked(run)
            spec = await self._spec(run)
            states, results = run["node_states"], run["results"]
            by_id = {n.id: n for n in spec.nodes}
            incoming: dict[str, list[Edge]] = {n.id: [] for n in spec.nodes}
            for e in spec.edges:
                if e.to != END and e.to in incoming:
                    incoming[e.to].append(e)

            moved = True
            while moved:
                moved = False
                for node in spec.nodes:
                    st = states.setdefault(node.id, {"status": "pending"})
                    if st["status"] != "pending":
                        continue
                    fired, resolved = False, True
                    for e in incoming[node.id]:
                        ps = states.get(e.from_, {}).get("status", "pending")
                        if ps in ("pending", "deferred", "running", "waiting"):
                            resolved = False
                            break
                        if ps == "done" and self._edge_fires(e, by_id[e.from_], results):
                            fired = True
                    if not resolved:
                        continue
                    if incoming[node.id] and not fired:
                        self._finish(st, None, "skipped")
                        moved = True
                        continue
                    await self._start_node(run, spec, node, states, results)
                    moved = True
                    # persist before the next node: a tool node has already
                    # written and journalled, an agent task's heartbeat is
                    # running; a crash here must not replay either
                    status, error = self._run_status(states)
                    await self.store.save_run(
                        run_id, status=status, node_states=states, results=results, error=error
                    )

            status, error = self._run_status(states)
            if status == "failed":
                # one failed node ends the run: stop what is still running in
                # parallel and close any gate a human might still decide
                await self._stop_siblings(run, states)
            saved = await self.store.save_run(
                run_id, status=status, node_states=states, results=results, error=error
            )
            if status == "failed":
                self.alerts.emit("run.failed", self._run_payload(saved))
            return saved

    async def _stop_siblings(self, run: dict, states: dict) -> None:
        for st in states.values():
            if st.get("status") == "running" and st.get("heartbeat_id"):
                try:
                    await self.store.request_cancel(st["heartbeat_id"])
                except (Conflict, NotFound):
                    pass
            if st.get("status") in ("running", "waiting", "deferred"):
                self._finish(st, None, "cancelled")
        await self.store.close_pending_approvals(run["id"], "cancelled")

    def _run_status(self, states: dict) -> tuple[str, str | None]:
        for nid, st in states.items():
            if st.get("status") == "failed":
                return "failed", f"node '{nid}': {st.get('error') or 'failed'}"
        if all(st.get("status") in ("done", "skipped") for st in states.values()):
            return "done", None
        if any(st.get("status") == "waiting" for st in states.values()):
            return "waiting", None
        return "running", None

    def _approved_on_path(
        self, node_id: str, spec: WorkflowSpec, states: dict, results: dict
    ) -> bool:
        """Run-time D3: did every edge that fired into ``node_id`` descend
        from an approval a human decided ``approved`` in this run? Mirrors
        :func:`agent_hub.validate.approval_gated` over the executed graph."""
        by_id = {n.id: n for n in spec.nodes}
        preds: dict[str, list[Edge]] = {n.id: [] for n in spec.nodes}
        for e in spec.edges:
            if e.to != END and e.from_ in preds and e.to in preds:
                preds[e.to].append(e)
        memo: dict[str, bool] = {}

        def walk(n: str, stack: frozenset[str]) -> bool:
            if n in memo:
                return memo[n]
            fired = [
                e
                for e in preds[n]
                if states.get(e.from_, {}).get("status") == "done"
                and self._edge_fires(e, by_id[e.from_], results)
            ]
            ok = bool(fired)
            for e in fired:
                src = by_id[e.from_]
                if src.type == "approval" and (
                    (results.get(src.id) or {}).get("decision") == "approved"
                ):
                    continue
                if e.from_ in stack or not walk(e.from_, stack | {n}):
                    ok = False
                    break
            memo[n] = ok
            return ok

        return walk(node_id, frozenset())

    @staticmethod
    def _edge_fires(edge: Edge, source: Node, results: dict) -> bool:
        res = results.get(source.id) or {}
        if source.type == "condition":
            branch = "true" if res.get("value") else "false"
            return (edge.when or "true").lower() == branch
        if source.type == "approval":
            return (edge.when or "approved").lower() == str(res.get("decision") or "")
        return True

    async def _start_node(
        self, run: dict, spec: WorkflowSpec, node: Node, states: dict, results: dict
    ) -> None:
        st = states[node.id]
        st["started_at"] = _now()
        ctx = {"inputs": run["inputs"], **results}
        try:
            if node.type in ("parallel", "join"):
                self._finish(st, None, "done")
                results[node.id] = {}
            elif node.type == "condition":
                value = evaluate(node.expr or "", ctx)
                results[node.id] = {"value": value}
                self._finish(st, None, "done")
            elif node.type == "approval":
                await self._request_approval(run, spec, node, st, results)
            elif node.type == "tool":
                await self._run_tool(run, spec, node, st, states, results, ctx)
            elif node.type == "agent_task":
                await self._launch_task(run, spec, node, st, states, results, ctx)
        except ExpressionError as exc:
            self._finish(st, str(exc), "failed")
        except Exception as exc:  # noqa: BLE001 - a node failure is data, never a crash
            log.exception("run %s node %s failed", run["id"], node.id)
            self._finish(st, f"{type(exc).__name__}: {exc}", "failed")

    async def _request_approval(
        self, run: dict, spec: WorkflowSpec, node: Node, st: dict, results: dict
    ) -> None:
        ap = await self.store.create_approval(
            {
                "id": uuid.uuid4().hex,
                "run_id": run["id"],
                "node_id": node.id,
                "workflow": run["workflow"],
                "roles": node.roles,
                "timeout_s": node.timeout_s,
                "context": {
                    "inputs": run["inputs"],
                    "results": results,
                    "plans": [
                        r["plan_id"]
                        for r in results.values()
                        if isinstance(r, dict) and r.get("plan_id")
                    ],
                },
            }
        )
        st["status"] = "waiting"
        st["approval_id"] = ap["id"]
        self.alerts.emit(
            "approval.requested",
            {
                "run_id": run["id"],
                "workflow": run["workflow"],
                "version": run["version"],
                "node_id": node.id,
                "approval_id": ap["id"],
                "roles": node.roles,
                "expires_at": ap.get("expires_at"),
                "invoked_by": run["invoked_by"],
            },
        )

    async def _run_tool(
        self,
        run: dict,
        spec: WorkflowSpec,
        node: Node,
        st: dict,
        states: dict,
        results: dict,
        ctx: dict,
    ) -> None:
        assert node.tool is not None
        tool = tk.REGISTRY.get(node.tool)
        if tool is not None and tool.tier != "read":
            if (await self.store.paused())["paused"]:
                self._finish(st, "agents are paused", "failed")
                return
            if node.environment not in MOCK_ENVIRONMENTS and not self._approved_on_path(
                node.id, spec, states, results
            ):
                self._finish(
                    st,
                    f"tool '{node.tool}' is tier '{tool.tier}' and no approved gate is on "
                    "the executed path to it (ADR-0010 D3)",
                    "failed",
                )
                return
        # a node labelled with an environment may only write to that one; the
        # label is what exempted it from the gate when it says mock
        envs = (node.environment,) if node.environment else ("*",)
        scope = tk.ToolScope(
            allow=frozenset({node.tool}), mode="full", projects=("*",), environments=envs
        )
        tctx = tk.ToolContext(backend=self.tool_backend(run))
        args = render(node.args, ctx)
        if not isinstance(args, dict):
            raise ExpressionError(f"node '{node.id}': args must render to an object")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, lambda: tk.scoped_dispatch(tctx, scope, node.tool, args)
        )
        st["journal"] = default_box.encrypt_doc(tctx.journal.dump())
        results[node.id] = res if isinstance(res, dict) else {"ok": True, "data": res}
        if isinstance(res, dict) and not res.get("ok", True):
            self._finish(st, str(res.get("error") or "tool call failed"), "failed")
        else:
            self._finish(st, None, "done")

    async def _launch_task(
        self,
        run: dict,
        wspec: WorkflowSpec,
        node: Node,
        st: dict,
        states: dict,
        results: dict,
        ctx: dict,
    ) -> None:
        assert node.agent is not None
        hit = await self.store.resolve("agent", node.agent)
        if hit is None:
            self._finish(st, f"cannot resolve agent '{node.agent}'", "failed")
            return
        name, version, raw = hit
        spec = AgentSpec.model_validate(raw)
        mode = node.mode or spec.mode
        if MODE_RANK[mode] > MODE_RANK[spec.mode]:  # the validator forbids this; belt and braces
            mode = spec.mode
        update: dict[str, Any] = {}
        if mode != spec.mode:
            update["mode"] = mode
        if node.environment:
            # the node's environment label narrows the agent's write scope to
            # that environment (it is also what exempts a mock task from the gate)
            ws = spec.write_scope
            envs = (
                [node.environment]
                if ("*" in ws.environments or node.environment in ws.environments)
                else []
            )
            update["write_scope"] = ws.model_copy(update={"environments": envs})
        if update:
            spec = spec.model_copy(update=update)
        gated = False
        if mode == "full" and node.environment not in MOCK_ENVIRONMENTS:
            # run-time D3: the agent's mode is what it is *now* (it may have
            # been republished since the workflow was validated) and the gate
            # must sit on the path that actually fired
            gated = self._approved_on_path(node.id, wspec, states, results)
            if not gated:
                self._finish(
                    st,
                    f"agent '{name}' would run in full mode and no approved gate is on "
                    "the executed path to it (ADR-0010 D3)",
                    "failed",
                )
                return
        ver = await self.store.get_version("agent", name, version)
        rendered = render(node.input, ctx)
        input_text = (
            rendered
            if isinstance(rendered, str)
            else json.dumps(rendered, indent=2, default=str) if rendered else ""
        )
        run_id, node_id = run["id"], node.id
        row = await self.launch_heartbeat(
            name=name,
            version=version,
            spec=spec,
            spec_sha256=ver["spec_sha256"],
            caller=run["principal"],
            input_text=input_text,
            wake="event",
            on_done=lambda hb: self._heartbeat_done(run_id, node_id, hb),
            subject=f"wfrun:{run_id}",
            gated=gated,
        )
        st["mode"] = mode
        st["agent"] = f"{name}@{version}"
        if row.get("coalesced"):
            # the agent is busy with another wake: try again shortly
            st["status"] = "deferred"
            st["retry_at"] = time.time() + self.retry_s
            self._spawn(self._retry_later(run_id, node_id))
            return
        st["heartbeat_id"] = row["id"]
        if row["status"] == "running":
            st["status"] = "running"
        else:  # refused before running (paused / budget): recorded, never ran
            self._finish(st, row.get("error") or row["status"], "failed")

    async def _retry_later(self, run_id: str, node_id: str) -> None:
        await asyncio.sleep(self.retry_s)
        async with self._lock(run_id):
            run = await self.store.get_run(run_id)
            st = run["node_states"].get(node_id) or {}
            if run["status"] in ACTIVE and st.get("status") == "deferred":
                st["status"] = "pending"
                st.pop("retry_at", None)
                await self.store.save_run(
                    run_id,
                    status=run["status"],
                    node_states=run["node_states"],
                    results=run["results"],
                )
        await self.advance(run_id)

    async def _watch_heartbeat(self, run_id: str, node_id: str, hb_id: str) -> None:
        """After a restart the runner task is gone; poll the row until the
        heartbeat finishes or the watchdog fails it."""
        while True:
            try:
                hb = await self.store.get_heartbeat(hb_id)
            except NotFound:
                hb = {"id": hb_id, "status": "failed", "error": "heartbeat row vanished"}
            if hb["status"] != "running":
                await self._heartbeat_done(run_id, node_id, hb)
                return
            await asyncio.sleep(self.retry_s)

    async def _heartbeat_done(self, run_id: str, node_id: str, hb: dict) -> None:
        async with self._lock(run_id):
            run = await self.store.get_run(run_id)
            if run["status"] not in ACTIVE:
                return
            st = run["node_states"].get(node_id) or {}
            if st.get("status") != "running" or st.get("heartbeat_id") != hb["id"]:
                return  # stale callback (cancelled, or re-attached elsewhere)
            results = run["results"]
            out: dict[str, Any] = {
                "heartbeat_id": hb["id"],
                "status": hb["status"],
                "result": hb.get("result"),
                "json": _json_maybe(hb.get("result")),
                "proposed": hb.get("proposed") or [],
            }
            if hb.get("proposed"):
                plan = await self.store.create_plan(
                    {
                        "id": uuid.uuid4().hex,
                        "run_id": run_id,
                        "node_id": node_id,
                        "heartbeat_id": hb["id"],
                        "agent": hb["agent"],
                        "version": hb["version"],
                        "spec_sha256": hb["spec_sha256"],
                        "proposed": hb["proposed"],
                        "result": hb.get("result"),
                        "authored_by": hb.get("invoked_by", ""),
                    }
                )
                out["plan_id"] = plan["id"]
                st["plan_id"] = plan["id"]
            results[node_id] = out
            if hb["status"] == "done":
                self._finish(st, None, "done")
            else:
                self._finish(st, hb.get("error") or hb["status"], "failed")
            await self.store.save_run(
                run_id, status=run["status"], node_states=run["node_states"], results=results
            )
        await self.advance(run_id)

    async def _approval_expired(self, ap: dict) -> None:
        async with self._lock(ap["run_id"]):
            try:
                run = await self.store.get_run(ap["run_id"])
            except NotFound:
                return
            if run["status"] not in ACTIVE:
                return
            st = run["node_states"].get(ap["node_id"]) or {}
            if st.get("approval_id") != ap["id"]:
                return
            self._finish(st, "approval timed out", "failed")
            saved = await self.store.save_run(
                run["id"],
                status="failed",
                node_states=run["node_states"],
                results=run["results"],
                error=f"node '{ap['node_id']}': approval timed out",
            )
            self.alerts.emit("run.failed", self._run_payload(saved))

    async def _cancel_locked(self, run: dict) -> dict:
        states = run["node_states"]
        for st in states.values():
            if st.get("status") == "running" and st.get("heartbeat_id"):
                try:
                    await self.store.request_cancel(st["heartbeat_id"])
                except (Conflict, NotFound):
                    pass
            if st.get("status") in ("running", "waiting", "deferred", "pending"):
                self._finish(st, None, "cancelled")
        await self.store.close_pending_approvals(run["id"], "cancelled")
        return await self.store.save_run(
            run["id"],
            status="cancelled",
            node_states=states,
            results=run["results"],
            error="cancelled by request",
        )

    # -- helpers -----------------------------------------------------------------------

    async def _spec(self, run: dict) -> WorkflowSpec:
        ver = await self.store.get_version("workflow", run["workflow"], run["version"])
        return WorkflowSpec.model_validate(ver["spec"])

    @staticmethod
    def _finish(st: dict | None, error: str | None, status: str) -> None:
        if st is None:
            return
        st["status"] = status
        st["finished_at"] = _now()
        if error:
            st["error"] = error

    @staticmethod
    def _run_payload(run: dict) -> dict:
        return {
            "run_id": run["id"],
            "workflow": run["workflow"],
            "version": run["version"],
            "status": run["status"],
            "error": run.get("error"),
            "invoked_by": run.get("invoked_by"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
        }

    def _lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = self._locks[run_id] = asyncio.Lock()
        return lock

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


__all__ = ["ACTIVE", "TERMINAL", "Engine"]
