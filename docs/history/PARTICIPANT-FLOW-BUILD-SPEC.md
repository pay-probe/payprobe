# Participant Flow — Build Spec

Status: **Phase 1 in progress.** Author: design session 2026-06-23.
Naming chosen by David: **Participant flow**.

---

## Build status (live)

**▶ Phases 1–6 backend COMPLETE + ALL portal editors done (incl. the visual
flow canvas).** Remaining: cross-hop correlated trace, and hot-path performance
(compile-to-responder + TPS benchmark).

**Portal editors built (all verify clean — only the known repo-wide rxjs/@angular
sandbox cascade, zero new real errors):** participants manage page (list +
start/stop + delete; "New flow"/row-click open the canvas); **the flow editor now
REUSES the scenario editor component** (`/flow-editor/:id` → `ConstructorComponent`
with route `data.mode = "flow"`) — same canvas/drag/wire/pan-zoom/inspector, gated
flow-mode: loads/saves via `ParticipantsService`, palette gains trigger/reply/state
(`FLOW_NODE_KINDS`, hidden from the scenario palette), a "listens on" trigger-
connection input, run-only toolbar hidden, back→/participants. (The earlier
from-scratch canvas was removed at the user's request.) **Topologies** page;
**Participant Groups** page; **Connection endpoints + selection policy** in the
connection form. Nav: Topologies + Participant Flows under Load Testing;
Participant Groups under Configure.

> Reuse verified at COMPILE level only (ng build NG counts identical to baseline;
> scenario editor not broken). **Must `npm run build` + click-test both editors.**

**Inbound/outbound connection wiring (fix):** the Connection form now has a
**Direction** select (outbound/inbound) + **listen port** (inbound) — so you can
create the listening connection a flow binds to. The portal Connection model +
`connections.service.save` carry `mode`/`listen_port` (backend already had them;
`_participant_listen_config` reads them). The flow's "**listens on**" toolbar
control is now a **dropdown of inbound connections**. The outbound Call-connection
picker filters to **outbound** connections only, and the orchestrator repoints the
picked `config.connection` onto the node's target at launch (worker dispatch path
already tested in `test_flow_responder` Phase 2). Setup: make an *inbound*
connection (mode=inbound + listen port) for the trigger, *outbound* connections
for Call nodes; map `${request.*}` into the call payload.

**Flow-specific steps + components added:** palette is tailored in flow mode —
the scenario **catalog** (Terminal/RestPay/HSM/Core Banking/DB Probe/TCP-ISO 8583/
EMV Tools/EMV Crypto) and Starter Flows are **hidden**; instead a flow **"⇄
Outbound → Call connection"** node is offered (`addFlowCall` → a `tcp_iso8583`
action so the existing Connection picker + payload editor apply). The orchestrator
repoints the picked `config.connection` onto the node's target at launch so the
worker dispatches to it. Control palette: trigger/reply/state shown; `call`/`init`
hidden; flow kinds hidden from the scenario palette. The inspector `@switch (step.kind)` gained **trigger** (entry
note + `${request.*}` hints), **reply** (JSON action editor — set/echo/generate/
drop), and **state** (JSON ops editor — set/incr/append) cases. Edits use stable
per-node string buffers (no cursor reformat) parsed with validation on save
(`flowNodeJson`/`setFlowNodeJson`). Build histogram returned to exact baseline
after these changes (zero new compile errors).

| Slice | State |
|---|---|
| **P5 Topology** — `Topology` model/store/`/topologies` CRUD; orchestrator `/topologies/{id}/start` launches each participant's instances together (rollback on partial failure) + `/topology-runs` list + `DELETE /topology-runs/{id}` stop | ✅ backend done — start 2 instances → both listening → stop → all gone; **scenario-service 224 · orchestrator 155**. Portal page deferred. |
| **P5x provisioning gate + metrics** — `CreateRunRequest.requires_topology` → `create_run` returns **424** until that topology is live; per-flow metrics on `/participants` (by_mti / by_response_code) + `GET /participants/{pid}` detail (stats + recent log) | ✅ done — gate + metrics tested. Cross-hop correlated trace still deferred. |
| **P6 shared simulated state** — `state` node kind (`set`/`incr`/`append`) + `${state.*}` reads; `FlowRunner.run_flow(..., state=)`; `FlowResponder` holds a per-instance `_state` dict persisted across messages | ✅ done — stateful issuer counts approvals across 3 separate connections (**worker 181**). Enables balances / sequence counters / sign-on flags in stand-ins. |
| **P6 auth on new endpoints** | ✅ already enforced by construction — orchestrator `@app.*` routes inherit the global `Depends(require_auth)`; scenario-service endpoints are on the `router` with `dependencies=[Depends(require_auth)]`; new paths aren't in `PUBLIC_PATHS`. Locked in with a test (`/participants`, `/participants/start`, `/topology-runs`, `/topologies/*/start` → 401 unauth in prod). **orchestrator 156**. |

> Note: each backend suite is green **in isolation** (worker 178 · orchestrator 155 · scenario-service 224). Running worker `test_load.py` + scenario-service together trips a **pre-existing** sys.path collision (both define a top-level `api` package; `api.load_coordinator` gets shadowed) — unrelated to this feature. Run suites per-package.
| **P4 Participant groups** — `ParticipantGroup` model/store/`/participant-groups` CRUD; worker `adapters/group.py` (`GroupAdapter` selects a member connection per dispatch, reusing `EndpointSelector`); registry recognizes `adapter:"group"`; orchestrator `_attach_groups` inlines member configs into the run env | ✅ backend done — **live round-robin + sticky-by-PAN routing** proven; **worker 178 · scenario-service 222 · orchestrator 152**. Portal groups page deferred. |
| **P2 Outbound calls** — `FlowResponder` dispatches `action` nodes to downstream connections via `AdapterRegistry`; orchestrator resolves a flow's downstream connections on start; response mapped into the reply via `${node.response.*}` | ✅ done — switch→issuer bridge proven (worker+orch **319 passed**). Flow editor authors action nodes in the JSON graph already. |
| **P3 Endpoints + selection policy** — `Connection.endpoints[]` + `selection{policy,key}`; worker `adapters/selection.py` (`EndpointSelector`: round_robin/weighted/random/failover/sticky) + `adapters/routing.py` (`RoutingAdapter` wraps per-endpoint children) + registry wiring | ✅ backend done — selector + **live round-robin across two responders** proven; **worker 176 · scenario-service 219 · orchestrator 150**. Portal connection-editor endpoints list deferred (like the JSON flow editor). |
| P1.1 Refactor — extract `GraphExecutor` core from `ScenarioRunner` | ✅ done, suites green |
| P1.2 scenario-service — inbound connection `mode`/`listen_port`, `ParticipantFlow` model/store/`/participant-flows` CRUD | ✅ done |
| P1.3 worker — `trigger`/`reply` kinds, `engine/flow_runner.py` (`FlowRunner`), `adapters/tcp/flow_responder.py` (`FlowResponder`) | ✅ done |
| P1.4 orchestrator — `/participants` start/stop/list lifecycle | ✅ done |
| P1.5 backend tests | ✅ done — +12 tests; **worker 168 · orchestrator 149 · scenario-service 219**, zero regressions |
| P1.6 portal — `participants/` service + manage page + editor + route + nav | ✅ done (graph authored as JSON for now) |

Backend proven end-to-end: define a flow → start it as a listening service →
it answers real ISO 8583 traffic by walking the graph → stop it. Portal:
`/participants` page (under Load Testing) lists flows, starts/stops listeners
with live status, and edits the flow graph.

**Deferred to a Phase 1.x enhancement:** the flow editor authors the graph as
JSON (nodes+edges) today. Reusing the visual `constructor/editor` canvas for
flows (drag trigger/reply/logic nodes) is the natural follow-up.

Note: the cowork sandbox can't fully build the Angular portal (`node_modules/rxjs`
type declarations are missing there, cascading implicit-any across *every* file);
the new files add only that same cascade and zero real errors — verify with a real
`npm run build`.



A Participant flow is the reactive complement to a scenario: instead of *driving*
a system (send + assert), it *stands in* for a participant — it **listens** on an
inbound connection, runs a graph (trigger → logic → outgoing calls → reply), and
**replies** on the wire. It is the visual evolution of the existing rules-driven
`TcpResponder` simulator, built on the **same graph engine** as the scenario
editor.

---

## 0. Guiding decisions (already settled)

- **One graph engine, two modes.** Reuse `worker/engine/runner.py` node execution
  (`action`, `code`, `crypto`, `http`, `call`, `if`, `switch`, `loop`, `delay`).
  Add two node kinds: `trigger` (entry, "on message received") and `reply`
  (exit, "send response on the inbound connection"). Outbound calls reuse the
  existing `action` node against a connection.
- **The connection is the seam.** A scenario step's *outbound* connection and a
  participant flow's *inbound* (listening) connection are two ends of one wire.
  Wiring real↔simulated is done purely through **environment connection values**
  (the per-env override matrix already built) — the flow definition never changes.
- **Single source of per-env values = the connection override matrix** (already
  shipped: `Connection.environment_overrides`).
- **Multiplicity = one selection policy** (`round-robin | weighted | random |
  failover | sticky`) used at two levels: endpoints inside a connection, and
  members inside a participant group.
- **Runs as a service**, not a one-shot run — registered/started/stopped like the
  current simulators (`orchestrator/api/simulator_store.py`).
- **Hot-path decision (Phase 6):** the flow either replaces the rules responder
  or compiles down to it for high-TPS paths. Keep flows interpretable until a
  benchmark forces the compile step.

---

## 1. What it reuses (do not rebuild)

| Need | Existing piece |
|---|---|
| Graph node execution | `packages/worker/engine/runner.py` (`ScenarioRunner`, `_run_step`, `_run_code_node`, `_run_crypto_node`, control nodes) |
| Listen / frame / reply on TCP | `packages/worker/adapters/tcp/responder.py` (`TcpResponder.start/stop/_handle/_frame`) |
| Outbound dispatch | `packages/worker/adapters/registry.py` (`AdapterRegistry`), `adapters/tcp/adapter.py` |
| Saved-simulator lifecycle/registry | `packages/orchestrator/api/simulator_store.py` (`SimulatorStore`) + portal `/simulators` |
| Graph editor UI | `packages/portal/src/app/constructor/editor/` (canvas, node palette, inspector) |
| Connection / environment model | `scenario-service/api/connection_store.py`, `environment_store.py`; orchestrator `_attach_connections(env, scs, env_name)` |
| Distributed run on a worker fleet | `packages/load_worker`, `load_coordinator` (Redis-coordinated) |
| Cross-step trace | execution-trace viewer (`StepOutcome.trace`, run-report Trace tab) |

---

## 2. Phased plan

Each phase is independently shippable and demoable. Build in order; later phases
depend on earlier ones.

### Phase 1 — Inbound connections + minimal Participant flow (trigger → logic → reply)

**Goal:** a connection can listen; a flow with no outbound calls answers incoming
messages through the graph engine. This is the smallest end-to-end proof and a
visual replacement for a rules responder.

**scenario-service**
- `api/connection_store.py`: add `mode: Literal["outbound","inbound"] = "outbound"`
  and `listen_port: int | None = None` to `ConnectionDraft`.
- New `models/participant_flow.py`: `ParticipantFlow { id, name, description,
  trigger: {connection: str}, nodes: list[Node], edges: list[Edge], variables }`.
  Reuse `models/scenario.py` `Step`/`Edge`; extend `NodeKind` with `"trigger"`,
  `"reply"`.
- New `api/participant_flow_store.py` (mirror `flow_store.py`): file-backed CRUD.
- `api/main.py`: wire `/participant-flows` CRUD endpoints + seed dir.

**worker**
- **Refactor first — extract the node-run core (per decision #1).** Lift the
  generic node-execution loop out of `ScenarioRunner` in `engine/runner.py` into a
  reusable core (e.g. `engine/graph_exec.py` or a `NodeExecutor` mixin) that owns
  node dispatch by kind, edge/port traversal, `${...}` resolution, and trace
  emission — with **no** scenario-only assumptions (verdict, `test_class`,
  pass/fail assertions). `ScenarioRunner` keeps its batch driver (run start →
  steps → verdict) and now calls the shared core. This is a pure refactor: the
  existing scenario suites must stay green with no behaviour change before any
  flow code is added.
- New `engine/flow_runner.py`: a **second driver** over the same core. Entry =
  `trigger` node (injects the parsed request into context as `{ request: ... }`);
  exit = `reply` node (builds the response payload from context). `assertions` on
  a flow node act as **guards** (match conditions), not verdicts. Per-message
  context isolation.
- New `adapters/tcp/flow_responder.py` (or extend `responder.py`): same
  `start/stop/_handle/_frame` as `TcpResponder`, but `_handle` parses the inbound
  bytes via the inbound adapter, runs `flow_runner` over the flow graph, and
  frames + writes the bytes produced by the `reply` node.

**orchestrator**
- `api/participant_store.py` (parallel to `simulator_store.py`): durable registry
  of participant-flow runtimes; running instances kept in-memory keyed by id;
  `enabled` auto-start on boot.
- Endpoints mirroring `/simulators`: `POST /participants/start`, `/stop`,
  `GET /participants` (status: bound port, message count, last reply).

**portal**
- New `src/app/participants/` : a flow **editor** (clone `constructor/editor`,
  add `trigger`/`reply` palette nodes, hide assert/verdict semantics) + a
  **manage** page (list, start/stop, live status) modeled on `/simulators`.
- Nav entry under the existing Simulators area.

**tests**
- worker: flow_runner executes trigger→logic→reply; flow_responder answers a
  framed request end-to-end.
- scenario-service: participant-flow CRUD round-trip.
- orchestrator: start/stop a participant; status reflects bound port.

**Acceptance:** start a participant flow on a port; a scenario step (or `nc`)
sends an ISO 8583 0200; the flow matches/branches and replies a 0210 with the
fields its graph set. No outbound hop yet.

**Risks:** node-kind plumbing in the editor; per-message concurrency in the
responder handler.

---

### Phase 2 — Outbound calls inside a flow (the bridge/proxy)

**Goal:** a flow can call a downstream connection and use the response to build
its reply. Enables stand-ins that forward, transform, or bridge protocols.

- worker `flow_runner.py`: support `action` nodes (target = a connection) that
  dispatch via `AdapterRegistry`, await the response, and expose it in context
  for downstream nodes — reuse `WorkerEngine._execute_step` semantics. Add
  per-hop timeout + no-response handling.
- Protocol bridging: inbound adapter ≠ outbound adapter (ISO in → REST out);
  field mapping handled by `transform`/`code` nodes already in the engine.
- Correlation: thread the inbound correlation key through to the reply.
- portal: enable `action` (call) nodes in the flow editor with a connection
  picker (reuse the scenario step connection picker).
- tests: flow that calls a downstream stub and maps its response into the reply;
  timeout path.

**Acceptance:** a "switch" flow receives a 0200, calls a downstream "issuer"
stub, and returns its response code to the caller.

---

### Phase 3 — Connection multiplicity: endpoints + selection policy

**Goal:** one connection, several addresses, with a policy (HA + load-spread).

- scenario-service `connection_store.py`: add
  `endpoints: list[{host, port, weight}]` and
  `selection: {policy: Literal["round_robin","weighted","random","failover","sticky"], key: str | None}`.
  `host`/`port` remain the single-endpoint shorthand. Per-env override of
  endpoints flows through the existing matrix.
- worker `adapters/registry.py` / adapter: pick an endpoint per dispatch by
  policy; rotation state **per-worker** (independent); `failover` advances on
  connection error / failed health; `sticky` routes by `key` (e.g. field 11 /
  terminal id).
- orchestrator: no change to merge logic beyond passing endpoints through.
- portal: connection editor — an endpoints list + policy selector.
- tests: round-robin rotation, failover-on-error, sticky-by-key, weighted split.

**Acceptance:** a connection with 3 endpoints round-robins under load and fails
over when one endpoint refuses.

---

### Phase 4 — Participant groups (fleets) + routing

**Goal:** group N connection instances under one logical participant with a policy;
a scenario step or a flow outbound can target the group.

- scenario-service: new `models/participant_group.py` +
  `api/participant_group_store.py`: `{ id, name, adapter_type, members: list[str],
  selection: {policy, key} }`; `/participant-groups` CRUD.
- resolution: extend orchestrator `_attach_connections` / a new
  `_attach_groups(env, scs, env_name)` so a step/flow target that names a group
  resolves to a member by policy (round-robin/weighted) or routing key
  (sticky/keyed, e.g. by BIN). Per-env membership/weights overridable.
- portal: a groups management page; the step/flow connection picker can target a
  group name.
- tests: group resolution per policy; keyed routing by BIN; per-env membership.

**Acceptance:** a step targets `issuers` (a group of 3); transactions distribute
by policy; a BIN-keyed group always routes a given BIN to the same member.

---

### Phase 5 — Topology / Harness: run several flows together

**Goal:** declare and run a whole network of participant flows as a unit.

- scenario-service: new `models/topology.py` + store:
  `Topology { id, name, participants: list[{flow_id, instances, listen_connection}],
  notes }`. Wiring between flows is implicit: a flow's outbound connection value
  (host:port) is supplied per environment to point at another flow's
  `listen_connection`.
- orchestrator: `POST /topologies/{id}/start|stop`: allocate/validate ports
  (from environment connection values), start flows in dependency order (a flow
  must listen before another calls it), health-gate, and run instances across the
  worker fleet (reuse `load_worker`/`load_coordinator` Redis coordination).
- pre-run provisioning: a scenario run / schedule can declare
  `requires_topology` so the run blocks (HTTP 424) until the topology is healthy
  — reuse the existing provisioning-pre-run pattern.
- observability: a single correlated trace across hops, surfaced in the
  execution-trace viewer (tag each hop with flow + instance + correlation id).
- portal: a topology editor/manager (list flows + instance counts + status) and a
  scenario "requires topology" control.
- tests: start a 3-flow topology; verify start order, health, and a scenario
  driving the chain end-to-end with a cross-hop trace.

**Acceptance:** start a `driver → acquirer → switch → issuers×3` topology in the
`sim` environment; run a scenario; see one trace correlated across every hop.

---

### Phase 6 — Hardening: state, performance, security, observability

- **Shared simulated state:** an optional per-flow state store (balances,
  sequence counters, sign-on state), isolated per running instance, with a
  documented concurrency model. Expose `${state.*}` to flow nodes.
- **Hot-path performance:** benchmark the interpreter at target TPS
  (20K/100K threads in the load subsystem). If it can't keep up, add a
  flow→responder **compile** step for hot, branch-light flows; keep full
  interpretation for complex ones.
- **Security:** flows run code nodes inside the existing code-node netns sandbox;
  auth on participant/topology endpoints (reuse fail-closed JWT gate); secrets in
  flow config via `SecretBox`.
- **Observability/metrics:** per-flow message count, latency, error rate, reply
  distribution; surfaced on the manage page and Prometheus/Grafana like the load
  subsystem.

---

## 3. New / changed data models (reference)

```
# scenario-service/api/connection_store.py  (ConnectionDraft, additive)
mode: "outbound" | "inbound" = "outbound"
listen_port: int | None = None
endpoints: list[{ host: str, port: int, weight: int = 1 }] = []        # Phase 3
selection: { policy: str, key: str | None } | None = None              # Phase 3
# environment_overrides: dict[str, dict]  — already shipped

# scenario-service/models/participant_flow.py                          # Phase 1
ParticipantFlow { id, name, description,
  trigger: { connection: str },
  nodes: list[Node],   # Node = scenario Step + kinds "trigger","reply"
  edges: list[Edge],
  variables: dict }

# scenario-service/models/participant_group.py                         # Phase 4
ParticipantGroup { id, name, adapter_type,
  members: list[str], selection: { policy, key } }

# scenario-service/models/topology.py                                  # Phase 5
Topology { id, name,
  participants: list[{ flow_id, instances: int, listen_connection: str }],
  notes }
```

---

## 4. Endpoints added (reference)

```
scenario-service
  GET/PUT/DELETE  /participant-flows[/{id}]                     # P1
  GET/PUT/DELETE  /participant-groups[/{id}]                    # P4
  GET/PUT/DELETE  /topologies[/{id}]                            # P5
orchestrator
  POST  /participants/start            { flow_id }              # P1
  POST  /participants/stop             { id }                   # P1
  GET   /participants                  -> running status        # P1
  POST  /topologies/{id}/start | /stop                          # P5
```

---

## 5. Open decisions to confirm at build time

1. **Reuse vs fork the graph model — DECIDED (2026-06-23).** Reuse at the
   **engine + node/edge + editor** level; keep the **artifact** separate.
   - Do NOT fork `ScenarioRunner`'s node executors, `${...}` resolution, edge/port
     routing, debug/trace plumbing, or the `constructor/editor` canvas. `trigger`
     and `reply` are new *kinds the one engine understands*; the flow editor is the
     same canvas configured differently.
   - Do NOT overload `Scenario`. `ParticipantFlow` is its own artifact (declares a
     trigger + reply; has no `test_class` / verdict / pass-fail assertions).
   - Share `Edge` directly. Reuse `Step`'s shape, **repurposing** its `assertions`
     field on a flow node as a *guard / match condition* (does this rule apply to
     this message?) rather than a verdict assertion.
   - Optional later: extract a shared `GraphNode`/`GraphEdge` base that both
     `Scenario` and `ParticipantFlow` compose — only if flows grow fields scenarios
     don't need. For Phase 1, reuse `Step` with documented repurposing.
   - Note: reusing the interpreter does NOT foreclose decision #2 (compiling the
     hot path) — that's an execution concern, orthogonal to model reuse.
2. **Replace vs compile the rules responder.** Keep both during transition;
   decide the hot path in Phase 6 from a benchmark, not up front.
3. **Where ports are owned.** Environment supplies listen ports/targets (keeps the
   seam in one place) + a collision validator — confirm vs a central allocator.
4. **State store backing.** In-memory per instance (simple) vs Redis-backed
   (shareable, survives restart) for stateful stand-ins.
5. **"pool" naming.** Keep participant **groups** distinct in name from the
   existing card/terminal **data** pools (`${pool.*}`).

---

## 6. Sequencing & rough effort

- **Phase 1** is the keystone (inbound mode + flow runtime + editor). Largest
  single lift; everything else builds on it.
- **Phase 2** is small once Phase 1 lands (reuse `_execute_step`).
- **Phase 3 & 4** (multiplicity) are independent of flows and could even land
  first to benefit scenarios too — but are most valuable once flows exist.
- **Phase 5** (topology) is the second big lift; needs the worker-fleet lifecycle.
- **Phase 6** is continuous hardening, front-loaded only where a benchmark or a
  stateful stand-in demands it.

Recommended first milestone: **Phase 1 end-to-end** (one inbound flow answering a
scenario), shipped behind the existing Simulators area.
