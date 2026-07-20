# ADR-0001: Run participant-flow topologies on the worker fleet

**Status:** Accepted — fully implemented 2026-07-06 (items 1–8; the
"topology" of the title is now a *network flow*, see ADR-0004)
**Date:** 2026-06-25
**Deciders:** PayProbe maintainers (David + reviewers)

## Context

A *topology* starts a set of participant flows (each × N instances) as one unit.
Today `orchestrator.api.main.start_topology` launches every instance as a
`FlowResponder` **inside the orchestrator process** (`_launch_participant`):

- All listeners share the orchestrator's CPU, memory, and event loop. A topology
  is therefore single-host — it does not scale horizontally and cannot back the
  20K/100K-TPS / 100K-connection targets the load subsystem aims for.
- An orchestrator restart/redeploy drops every listener. There is no HA: one
  process is a single point of failure for the whole simulated network.
- The recently added per-instance port planner allocates `listen_port + i` on
  one host; across many hosts we instead need each instance to advertise wherever
  it actually bound so callers (groups / connections) can reach it.

We already operate a horizontally-scaled fleet for **load generation**:

- `worker/engine/load` `LoadBus` (Redis pub/sub, with an in-memory fallback for
  dev/tests).
- `orchestrator/api/load_coordinator.py` shards work across workers, tails their
  metric streams, and merges them.
- `orchestrator/api/worker_provisioner.py` + `GET /load-workers` track external
  `python -m worker.load_worker` processes via heartbeats.
- `orchestrator/api/run_control.py` already does Redis-backed cross-replica
  cancel/stop.

Crucially, `load_worker` *generates* outbound load; it does **not** host
inbound listeners. Hosting participant flows on the fleet is a new worker
capability, not just a new caller of an existing one.

### Forces

- Reuse the proven bus/heartbeat/coordinator machinery rather than build a second
  clustering stack.
- Listeners are long-lived and addressable (callers must find them); load shards
  are fire-and-forget. The fleet abstraction must grow an **endpoint registry**.
- Keep the single-host path working for dev/tests (the in-memory bus fallback).
- Don't block on this: cross-hop correlated trace (shipped separately) and the
  per-instance port fix already make the single-host path solid.

## Decision

Add a **participant-host role to the worker** and a **topology coordinator** in
the orchestrator that places instances on the fleet over the existing `LoadBus`,
collects each instance's advertised `host:port` into a registry, and exposes that
registry so groups/connections resolve to live fleet endpoints. Fall back to
in-process hosting when no external fleet/Redis is configured (current behaviour),
so dev and tests are unchanged.

## Options Considered

### Option A: Status quo — in-process hosting only

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (already built) |
| Cost | Low |
| Scalability | Poor — one host, one event loop |
| Team familiarity | High |

**Pros:** zero new moving parts; great for dev, demos, CI; already tested.
**Cons:** no horizontal scale or HA; orchestrator restart kills the network;
can't meet the TPS/connection targets.

### Option B: Distribute over the existing LoadBus + a topology coordinator

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium–High |
| Cost | Medium (reuses Redis/fleet already operated) |
| Scalability | Good — add workers to add capacity/HA |
| Team familiarity | Medium (same patterns as load) |

**Pros:** reuses bus, heartbeats, provisioner, and run-control; one operational
model for the fleet; in-memory fallback preserves the single-host path.
**Cons:** needs a new worker role (host listeners), an endpoint registry, and
liveness-driven group resolution; listener lifecycle is more involved than
fire-and-forget load shards.

### Option C: Externalize each flow as a platform deployment (k8s)

| Dimension | Assessment |
|-----------|------------|
| Complexity | High (control-plane + manifests per flow) |
| Cost | High (k8s coupling) |
| Scalability | Excellent |
| Team familiarity | Low–Medium |

**Pros:** offloads scheduling/HA/health to the platform; battle-tested scaling.
**Cons:** couples PayProbe to k8s; slow start/stop (image pulls, scheduling) ill-
suited to "start a topology, run a scenario, tear down"; heavy for dev/CI; still
needs an endpoint registry for discovery.

## Trade-off Analysis

Option A can't meet the goals that motivated this ADR. Option C buys the most
scale but at the cost of platform coupling and start/stop latency that fights the
ephemeral "spin up a network for a test run" workflow — and it still doesn't
remove the discovery problem. Option B reuses machinery we already run in
production for load, keeps the single-host fallback intact, and contains the new
work to two well-scoped pieces (a worker host role + an endpoint registry). The
main risk — listener lifecycle/liveness is harder than load shards — is mitigated
by reusing `worker_provisioner` heartbeats and `run_control` for cross-replica
stop.

The decisive factor: discovery. Whatever we pick, callers must resolve a logical
participant to a live address. Building that registry once (Option B) is reusable;
Option C makes us build it *and* adopt k8s.

## Consequences

**Easier**
- Horizontal scale and HA by adding workers; orchestrator restart no longer drops
  the network (state lives in the registry/bus).
- One operational story for the fleet (load + topologies share bus/heartbeats).
- The `requires_topology` readiness gate becomes fleet-wide: ready = all instances
  registered *and* heartbeating.

**Harder**
- New failure modes: a worker dies mid-topology → re-place its instances and
  update the registry; partial-placement rollback now spans hosts.
- Group/connection resolution must consult the live registry instead of a static
  `listen_port + i` range.
- Secrets/config for hosted flows must reach workers (reuse `SecretBox`).

**Revisit**
- Whether the in-memory single-host path stays the default for dev/CI (recommend
  yes) and how tests assert fleet placement without real Redis.
- Port/address allocation policy across hosts (ephemeral + advertise vs. assigned
  ranges per worker).

## Action Items

1. [x] Worker: `worker.flow_host` (2026-07-06) — claims placements from the
       shared bus, instantiates the right listener (FlowResponder /
       HttpFlowResponder / transparent TcpProxy — the coordinator embeds the
       resolved config + downstream, so a host needs nothing but the bus),
       heartbeats presence with `role: flow_host`, honours the fleet-panel
       drain, and refreshes its instances' advertisements each beat.
2. [x] Bus: the existing `LoadBus` classes grew the flow-hosting primitives —
       a global placement queue (`push_placements`/`claim_placement`), a
       per-run **endpoint registry** (`advertise_instance`/`list_instances`,
       TTL-pruned via `INSTANCE_TTL_S`), and a network-wide stop flag
       (`signal_network_stop`). Failed starts advertise `status: "error"` so
       the coordinator fails closed instead of timing out.
3. [x] Orchestrator: `flow_fleet.FlowFleet` places instances, waits for every
       advertisement (error/timeout → network-wide stop + `PlacementError`,
       surfaced as HTTP 502 — partial networks never linger), and keeps a
       periodically refreshed registry snapshot for sync consumers. Fleet mode
       is opt-in: `NETWORK_FLEET=1` *and* ≥1 flow host heartbeating; runs are
       tagged `fleet: true` in `TOPOLOGY_RUNS`.
4. [x] Discovery (2026-07-06): two layers, both keyed by the planned listen
       port (deterministic — the port planner guarantees one instance per
       host:port). *In-network*: fleet placement runs callee-first **waves**;
       each wave feeds the endpoints advertised so far into the next
       placement's outbound refs (`_rewrite_fleet_refs`: downstream adapters,
       group members, relay upstreams) so a caller on host B reaches its
       callee on host A. *Run-build*: `_attach_fleet_endpoints` (after
       `_attach_groups`/`_attach_step_environments`) re-points a scenario
       run's env adapters (host/port, gRPC `target`, HTTP `base_url`) at the
       advertised hosts. Limitation: ephemeral-port listeners (no planned
       port) aren't matched — use fixed ports for cross-host wiring.
5. [x] Readiness: `_run_health` / `_run_endpoints` read the registry snapshot
       for fleet runs (expected `instance_ids` vs freshly advertised `up`
       instances), so `requires_topology` (HTTP 424) is fleet-wide and a dead
       host degrades the run within `INSTANCE_TTL_S`.
6. [x] Lifecycle (2026-07-06): `FlowFleet.reconcile` — the registry poller
       detects an instance whose advertisement expired (host death) and pushes
       its stored placement back onto the queue for any surviving host, with a
       per-instance cooldown (`REPLACE_COOLDOWN_S`); hosts skip placements
       already advertised `up`, so a slow-but-alive host is never duplicated.
       Note: a re-placed ephemeral-port instance may bind a new port — envs
       built before the move keep the old endpoint until re-run.
7. [x] Fallback: in-process hosting remains the default (no flag / no hosts →
       exactly the old path); placement, rollback, drain, and fallback are
       covered by in-memory-bus tests (`worker/tests/test_flow_host.py`,
       `orchestrator/tests/test_network_fleet.py`).
8. [x] Observability (2026-07-06): advertisements carry a per-instance
       `received` count, and **fleet trace aggregation** — hosts ship newly
       recorded hops to a per-run bus stream with each heartbeat (ts-watermarked,
       tagged with instance + worker); `/participants/traces` and
       `/participants/trace/{cid}` merge fleet records with local listeners, the
       global capture toggle propagates over the bus (`set_flow_capture`,
       applied on host heartbeats; placements carry the initial posture), and
       the Network Trace "Clear" also drops the shipped fleet streams.

## Notes

- Out of scope here: the flow→responder **compile** step for hot, branch-light
  paths (a separate execution-performance decision; orthogonal to placement).
- Depends on / complements: the per-instance port planner and the fleet-wide
  readiness gate (shipped), and the cross-hop correlated trace (shipped) which
  this ADR would aggregate across hosts.
