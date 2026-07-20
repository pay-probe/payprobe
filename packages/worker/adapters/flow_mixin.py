"""FlowTraceMixin — the participant-flow runtime + correlated trace machinery,
factored out so it can be mixed into *any* protocol responder.

A participant flow listens on a connection, runs each inbound request through a
node graph (``trigger`` → logic → ``reply``), and answers with the reply node's
payload. The wire (framing, decode/encode, metrics, chaos, lifecycle) belongs to
the host responder — :class:`~worker.adapters.tcp.responder.TcpResponder` for
ISO 8583 / host-command, :class:`~worker.adapters.http.responder.HttpResponder`
for REST. This mixin supplies the *protocol-independent* half: walk the flow,
dispatch the flow's outbound ``action`` calls to downstream connections, and
record a per-transaction **correlated trace** the orchestrator stitches into an
end-to-end Network Trace.

Both hosts expose the same ``_resolve_async(parsed) -> action`` hook (called once
per decoded request), so the mixin simply *is* that hook. The handful of things
that genuinely differ per protocol — how to read the correlation id, what a
compact request view looks like, the inbound label — are isolated in small
overridable hooks. The defaults here are the ISO 8583 behaviour (DE 11 / DE 39 /
MTI), so :class:`~worker.adapters.tcp.flow_responder.FlowResponder` needs no
overrides; :class:`~worker.adapters.http.flow_responder.HttpFlowResponder`
overrides them for method/path/status.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .registry import AdapterRegistry
from ..engine.flow_runner import FlowRunner
from ..engine.events import NullSink


class FlowTraceMixin:
    #: Inbound adapter family tag stamped on every trace record so the UI can
    #: render each hop in its native shape. Overridden by HTTP/other hosts.
    TRACE_KIND = "tcp"

    def _init_flow(
        self,
        flow: dict,
        *,
        downstream: dict | None = None,
        sink=None,
        run_id: str = "flow",
    ) -> None:
        """Wire the flow runtime onto the host responder. Call from ``__init__``
        AFTER the host responder's own ``__init__`` (which sets ``self.config``)."""
        #: The participant-flow definition (nodes/edges) run per message.
        self.flow = flow or {}
        self._sink = sink or NullSink()
        self._run_id = run_id
        #: Shared simulated state for this listening instance — ``state`` nodes
        #: mutate it; persists across messages (balances, counters, sign-on…).
        self._state: dict = {}
        #: Downstream adapters this flow's outbound ``action`` nodes call, keyed
        #: by connection name (worker-shaped config). Lazily connected.
        self._downstream_cfg = downstream or {}
        self._registry = AdapterRegistry({"adapters": self._downstream_cfg})
        #: Per-message correlated trace ring buffer (see ``traces``). Generous so
        #: a Network Trace drill-in still finds a recently-listed transaction
        #: under load. Override via ``trace_buffer`` on the responder config.
        self._traces: list[dict] = []
        self._max_traces = int((self.config or {}).get("trace_buffer", 10000))
        #: Capture gate — when False, hops are handled but NOT recorded (the
        #: Network Trace "pause capture" control flips this; listeners untouched).
        self._capture = True

    # -- downstream dispatch -------------------------------------------------

    async def _execute_step(self, target: str, action: str, payload: dict, assertions: list):
        """Dispatch one outbound call to a downstream connection (mirrors
        ``WorkerEngine._execute_step``)."""
        adapter = await self._registry.get(target)
        sr = await adapter.execute(action, payload)
        if assertions:
            obj = dict(sr.response_payload or {})
            obj.setdefault("duration_ms", sr.duration_ms)
            sr.assertions = adapter.assert_response(obj, assertions)
        return sr

    def _downstream_kind(self, target: str) -> str:
        """Adapter family of a downstream target, for the call's trace tag."""
        cfg = self._downstream_cfg.get(target) or {}
        a = str(cfg.get("adapter") or "").lower()
        if a in ("payshield", "hsm_client"):
            return "hsm"
        if a.startswith("db_probe"):
            return "db"
        if a in ("grpc", "http", "nats"):
            return a
        if a == "group":
            return "group"
        return str(cfg.get("protocol") or a or "tcp")

    # -- correlation / trace -------------------------------------------------

    def _record_trace(self, rec: dict) -> None:
        if not self._capture:
            return
        self._traces.append(rec)
        overflow = len(self._traces) - self._max_traces
        if overflow > 0:
            del self._traces[:overflow]

    def traces(self, correlation_id: str | None = None) -> list[dict]:
        """Recorded hops, optionally filtered to one correlation id."""
        if correlation_id is None:
            return list(self._traces)
        return [t for t in self._traces if t.get("correlation_id") == correlation_id]

    # -- per-protocol hooks (ISO 8583 defaults; HTTP host overrides) ---------

    def _correlation_field(self) -> str:
        """Request field carrying the correlation id (DE 11 by default)."""
        return str((self.config.get("correlation") or {}).get("field", "11"))

    def _correlation_id(self, parsed: dict) -> str:
        """Pull the correlation id from the request, or mint one when absent so
        every hop of this transaction can still be stitched together."""
        field = self._correlation_field()
        de = parsed.get("de") or {}
        val = de.get(field) or de.get(str(field)) or parsed.get("correlation")
        return str(val) if val else uuid.uuid4().hex[:12]

    def _inject_correlation(self, payload: Any, cid: str) -> None:
        """Best-effort: stamp the correlation id onto an outbound call payload so
        the downstream listener records the same id (ISO DE form)."""
        if not isinstance(payload, dict):
            return
        field = self._correlation_field()
        de = payload.get("de")
        if de is None:
            de = payload["de"] = {}
        if isinstance(de, dict):
            de.setdefault(field, cid)

    def _trace_request(self, parsed: dict) -> dict:
        """Compact inbound view for the trace. ISO form: MTI + DE map. Handles
        both the raw decode (``fields``: {de:{value}}) and the flow view
        (``de``: {de:value})."""
        fields = parsed.get("fields") or {}
        de = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in fields.items()}
        if not de and isinstance(parsed.get("de"), dict):
            de = dict(parsed["de"])
        return {"mti": parsed.get("mti") or parsed.get("command"), "de": de}

    def _trace_label(self, parsed: dict) -> str:
        """Short inbound label for the index (MTI for ISO)."""
        return parsed.get("mti") or parsed.get("command") or "?"

    def _trace_response_summary(self, action: dict) -> str:
        """Short reply summary (DE 39 for ISO, HTTP status for REST). Both host
        responders define ``_response_code`` accordingly."""
        return self._response_code(action)

    # -- the shared resolve + trace -----------------------------------------

    async def _resolve_async(self, parsed: dict) -> dict:
        """Run the flow for this request; the reply node's payload is the action.

        Records a correlated trace entry for this hop and propagates the
        correlation id to any downstream calls the flow makes."""
        cid = self._correlation_id(parsed)
        calls: list[dict] = []

        async def _exec(target: str, action: str, payload: dict, assertions: list):
            self._inject_correlation(payload, cid)
            started = time.time()
            try:
                sr = await self._execute_step(target, action, payload, assertions)
            except Exception as exc:  # noqa: BLE001 — record WHERE/WHY then re-raise
                calls.append(
                    {
                        "target": target,
                        "action": action,
                        "ok": False,
                        "kind": self._downstream_kind(target),
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": int((time.time() - started) * 1000),
                        "request": payload,
                        "response": None,
                        "correlation_id": cid,
                    }
                )
                raise
            calls.append(
                {
                    "target": target,
                    "action": action,
                    "kind": self._downstream_kind(target),
                    "ok": bool(getattr(sr, "success", True)),
                    "error": getattr(sr, "error", None),
                    "duration_ms": getattr(sr, "duration_ms", None),
                    "request": getattr(sr, "request_payload", payload),
                    "response": getattr(sr, "response_payload", None),
                    "correlation_id": cid,
                }
            )
            return sr

        runner = FlowRunner(self._sink, run_id=self._run_id)
        flow_error: str | None = None
        status = "ok"
        action: Any = None
        try:
            result = await runner.run_flow(self.flow, parsed, execute_step=_exec, state=self._state)
            action = result.reply
            status = result.status
        except Exception as exc:  # noqa: BLE001 — a node error must still be traced
            flow_error = f"{type(exc).__name__}: {exc}"
            status = "error"
        if not isinstance(action, dict):
            action = dict(self.default)  # no/failed reply -> the configured default

        self._record_trace(
            {
                "correlation_id": cid,
                "flow_id": self._run_id,
                "flow_name": getattr(self, "_flow_name", None),
                "kind": self.TRACE_KIND,
                "port": self.port,
                "ts": time.time(),
                "mti": self._trace_label(parsed),
                "request": self._trace_request(parsed),
                "response_code": self._trace_response_summary(action),
                "status": status,
                "error": flow_error,
                "ok": flow_error is None and all(c.get("ok", True) for c in calls),
                "calls": calls,
            }
        )
        return action
