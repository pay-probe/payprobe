# ADR-0004: Unify Participant Flows and Scenarios via a composable "Network Flow"

**Status:** Accepted (Option B implemented 2026-07-06, incl. Phase 4)
**Date:** 2026-07-06
**Deciders:** PayProbe maintainers (David + reviewers)

## Context

PayProbe has two graph-based authoring artifacts and one flat composition
artifact:

- **Scenario** (`scenario-service/models/scenario.py`) — an *active* graph: run
  once, start → steps → verdict. Rich management surface: projects, sets,
  versions, tags, validation, pools, secret vars.
- **Participant flow** (`models/participant_flow.py`) — a *reactive* graph: a
  long-lived listener, trigger → logic → reply. Flat store, no projects or
  versions.
- **Topology** (`models/topology.py`) — a *list*, not a graph:
  `participants: [{flow_id, instances}]` in manual callee-first order, plus an
  optional `initiator` scenario. No canvas; the wiring between participants is
  implicit in each flow's connections and only becomes visible after the fact in
  `/network-graph`.

The question: can the two flow kinds be combined so that (1) participants are
placeable as node components, (2) participant flows stay shared/reusable but are
managed "the scenario way", and (3) a shared composite flow — like a topology,
but authored as a flow — can be started as a unit?

### How much is already unified

More than it looks. The convergence is deliberate and deep:

- `ParticipantFlow.nodes` **are** `scenario.Step` and its edges are
  `scenario.Edge` — one node schema, one edge schema, one `NodeKind` enum
  (partitioned: `call`/`init` scenario-only; `trigger`/`reply`/`state`/`relay`
  flow-only).
- The worker's `FlowRunner` **subclasses** the scenario `GraphExecutor` — same
  node executors, `${...}` resolution, edge walk, trace emission. The only
  differences are entry (trigger seeds `${request.*}`) and exit (`reply`
  payload).
- The portal uses **one editor** for both (`constructor.component.ts`,
  `flowMode` flag) — palette and toolbar differ, canvas does not.
- Flows already have scenario-grade tooling: test-fire + step-through debug
  (`POST /flows/debug-run`) reuses the scenario `DebugSession`.

So the remaining gaps are *semantic and organisational*, not mechanical:

| Dimension | Scenario | Participant flow |
|-----------|----------|------------------|
| Lifecycle | run-to-completion | long-lived listener |
| Verdict | pass/fail + assertions | none (reply or drop) |
| Executes in | worker, per run | orchestrator process (`FlowResponder`) — ADR-0001 proposes moving to the fleet |
| Storage | projects/sets/versions | flat file store |
| Edge meaning | control flow (temporal) | control flow (temporal) |

And the topology gap: a topology's "edges" are **wiring** (who listens for
whom), a structurally different meaning from a scenario's control-flow edges.

## Decision (proposed)

**Yes — feasible, and most of the cost is already paid.** Do it as a *third
graph level* rather than a literal merge: introduce a **Network Flow** — a
canvas-authored composite whose nodes are participants and scenarios and whose
edges are wiring — and bring participant-flow management up to scenario parity.
Do **not** collapse Scenario and ParticipantFlow into one document type; their
lifecycles (run vs serve) are legitimately different.

Concretely, the three parts of the idea map to:

1. **Participant as a component/step** → new node kinds on the network canvas:
   - `participant` — `config: {flow_id, instances, port?}`; references a shared
     participant flow exactly the way the existing `call` node references a
     shared sub-scenario.
   - `scenario` (initiator) — `config: {scenario_id, environment?, autostart}`;
     generalises `TopologyInitiator` from 0..1 to N drivers.
   - optionally later: `simulator` (saved simulator) and `group` (fleet) nodes.
2. **Shared participant flows, managed the scenario way** → keep the flow
   library as-is (already shared + referenced by id), add versioning and
   project scoping to `ParticipantFlowStore` so flows get the same
   version-pinning and organisation scenarios have.
3. **Shared startable composite ("scenario flow like topology")** → the Network
   Flow document itself: `POST /network-flows/{id}/start` behaves like
   `start_topology` today, but start order is **derived by topological sort of
   the wiring edges** (callees first) instead of the error-prone manual list
   order.

Edges on the network canvas mean "A's outbound connection targets B's
listener". They are resolvable/validatable at save time (the connection each
node's flow binds to is known), and they replace both the manual ordering *and*
give the canvas a live-status overlay for free by reusing the `/network-graph`
lane-layout + animated-edge rendering.

## Options considered

### Option A: Visual editor over the existing Topology (minimal)

Keep the `Topology` model; add a canvas page that renders participants as nodes
and derives edges from `/network-graph`. No model change.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Model churn | None |
| Delivers the idea | Partially — view, not authoring; order still manual |

**Pros:** cheap; zero migration.
**Cons:** edges stay read-only; no initiator-as-node; doesn't advance
unification; second bespoke canvas to maintain.

### Option B: Network Flow composite artifact (recommended)

New `NetworkFlow` model (nodes = `Step` with `participant`/`scenario` kinds,
edges = `Edge` reused as wiring) + store; absorbs Topology via one-shot
migration (`participants[]` → participant nodes, `initiator` → scenario node,
list order → edges); `/topologies` API kept as a thin alias during transition.
Orchestrator start/stop reuses `_launch_participant`, `TOPOLOGY_RUNS`, health,
and the `requires_topology` gate unchanged (gate now names a network-flow id).
Portal adds a third constructor mode (`network`) to the *existing* editor.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| Model churn | Additive; Topology becomes legacy alias |
| Delivers the idea | Fully — all three parts |

**Pros:** reuses Step/Edge/editor/run machinery; ordering bugs eliminated
(topo-sort + cycle detection); initiators become first-class and plural;
natural home for live status overlay; keeps run-vs-serve semantics separate
where they genuinely differ.
**Cons:** third `NodeKind` partition to police in validation; migration touches
MCP tools (`list_topologies`, `start_topology`) and portal topology pages;
wiring edges need their own validator (connection compatibility, port
collisions, cycles).

### Option C: Full single-document merge (`flow_type` discriminator)

One `Flow` document (`scenario | participant | network`), one store, one API.

| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Model churn | High — touches every store, API, MCP tool, portal page |
| Delivers the idea | Same user-visible result as B |

**Pros:** one store, one versioning story.
**Cons:** conflates run and serve lifecycles behind a flag; verdict/assertions
are meaningless for listeners; big-bang migration of scenario projects/sets;
no user-visible gain over B. Defer — B leaves this open as a later
consolidation if wanted.

## Trade-off analysis

The decisive point is that the expensive unification (node schema, executor,
editor) already happened; Option B buys the remaining user-visible value —
participants as components, canvas-authored startable networks — for the price
of one new document type and a mechanical Topology migration. Option A is a
dead end (a viewer, not the feature). Option C pays a large migration for zero
additional capability and blurs a real semantic boundary: a scenario *finishes*,
a participant *serves*; assertions, verdicts, and run reports only make sense
for the former. Keeping them as distinct types that share one graph substrate is
the honest model.

The main genuine risk in B is edge-semantics confusion — the same `Edge` class
meaning "then" in scenarios/flows and "connects to" in network flows. Mitigate
in the editor (different edge styling, no `source_port` choice on wiring edges)
and in validation (network-flow edges validated against connections, not
output ports).

## Consequences

- Easier: authoring a simulated network becomes visual and ordered-by-graph;
  multiple traffic drivers per network; flow version pinning inside a network;
  live topology status lands on the same canvas people author in.
- Harder: `NodeKind` now has three partitions — validation and palette logic
  must stay disciplined; MCP/portal topology surfaces need aliasing through the
  transition.
- Revisit later: Option C consolidation; `participant` node placement *inside a
  scenario* (auto-start dependencies per run — today covered by the
  `requires_topology` gate, which is probably the better semantics anyway);
  interaction with ADR-0001 (worker-fleet execution) — the NetworkFlow model
  must not assume in-process launch, so keep launch details out of the document
  and in the orchestrator runtime, as today.

## Action items (phased)

1. [x] **Phase 0 — flow-store parity:** version history in
   `ParticipantFlowStore` (bounded, file-backed; `?version=` +
   `/participant-flows/{id}/versions`).
2. [x] **Phase 1 — model + migration:** `models/network_flow.py` (reuses
   `Step`/`Edge`; `participant`/`scenario` node kinds added to `NodeKind` and
   fenced out of scenario validation) + `NetworkFlowStore` + validator (shape,
   direction, cycles → warning, dangling refs via `/network-flows/validate`) +
   one-shot topology migration reusing topology ids; `/topologies` untouched
   as the legacy alias.
3. [x] **Phase 2 — orchestrator:** `POST /network-flows/{id}/start` consumes
   the scenario-service `/plan` (topological sort, callees first; node `port`
   override; per-instance port planning), reuses `TOPOLOGY_RUNS`/stop/health;
   N autostart initiators run + cancel; runtime persistence restores network
   flows; `requires_topology` gate accepts network-flow ids.
4. [x] **Phase 3 — portal:** `/network-flows` canvas page (drag nodes, wiring
   edges with callee-first semantics, inspector, validate-on-save, start/stop
   + health) as a dedicated component — the shared scenario editor was left
   untouched deliberately; old Topologies page kept during transition.
5. [x] **Phase 4:** `simulator` node kind (saved simulator started with the
   network if not already running, stopped with it only when this run started
   it) + `group` node kind (passive wiring target, target-only edges enforced);
   MCP network-flow tools (`list/get/save/delete/validate/plan/start/stop`,
   topology tools flagged legacy, portal catalog regenerated); Topologies page
   retired (`/topologies` route redirects to `/network-flows`, nav entry
   removed, component kept unrouted until the API alias is dropped);
   `/topologies` + orchestrator start endpoints marked `deprecated` in
   OpenAPI.
6. [x] **Alias removal (2026-07-06):** the `/topologies` scenario-service API
   and orchestrator `POST /topologies/{tid}/start` were deleted (the
   `TopologyStore` remains only as read-only migration input for a
   pre-migration `topologies.json`); flow rename now repoints network flows
   (`repointed_network_flows`); MCP `list_topologies`/`start_topology`/
   `stop_topology` removed (`stop_network_flow` + `list_topology_runs` cover
   runs); portal legacy component deleted and `TopologiesService` re-backed by
   `/network-flows` for the map pages; legacy runtime-state `"topologies"`
   entries restore through `start_network_flow` (ids match). `/topology-runs`
   endpoints intentionally remain — they are the run API network flows execute
   through.
7. [ ] **Later:** evaluate Option C consolidation once B has bedded in.
