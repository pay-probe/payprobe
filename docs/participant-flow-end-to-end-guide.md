# Building a Working Full Flow — End to End

How to stand up a complete simulated payment network in PayProbe: inbound
connections → participant flows → a participant group → a topology → a scenario
that drives the whole thing. Grounded in the shipped API (Participant Flow spec
Phases 1–6).

The worked example builds this network:

```
scenario (driver)  →  "switch" flow  →  issuers group (3 issuer flows)  →  reply
```

The **switch** flow receives an ISO 8583 `0200`, routes it to one of three
**issuer** stand-ins via a group policy, and returns the issuer's response. A
**topology** brings all four listeners up together; the **scenario** blocks until
that topology is healthy, then sends traffic.

---

## Mental model (read this first)

Three layers, two sides of every call:

- **Participant flow** — one reactive listener. Binds an *inbound* connection,
  walks a `trigger → logic → reply` graph, answers on the wire. It is the visual
  evolution of the rules-driven `TcpResponder`.
- **Participant group** — N *outbound* member connections treated as one logical
  participant, plus a `selection` policy. This is the **caller side**: which
  endpoint a dispatch goes to.
- **Topology** — a named set of flows (each with an instance count), started and
  stopped as one unit. This is the **callee side**: bringing the network up/down.

A group's members are the connections that reach a topology's listeners. The
connection is the seam — wiring real↔simulated is done purely through connection
values (host/port and per-environment overrides); the flow definition never
changes.

---

## Prerequisites

- scenario-service running (config registry: connections, flows, groups,
  network flows, environments) — default `:8000`.
- orchestrator running (lifecycle: starts/stops listeners and network flows).
- A worker reachable by the orchestrator (the flow runtime — `FlowResponder` +
  `FlowRunner`).
- Auth: all `/participants*` and `/topolog*` endpoints require a JWT. Send
  `Authorization: Bearer <token>` on every call below.

The portal exposes all of this: **Participant Flows** and **Topologies** under
*Load Testing*, **Participant Groups** under *Configure*, and **Connections**
under *Configure*. The steps below show both the portal action and the underlying
API so you can script it.

---

## Step 1 — Create the inbound (listening) connections

Each flow needs a connection in **inbound** mode to listen on. Direction lives on
the connection: `mode: "inbound"` + a `listen_port` (use `0` to pick a free port
at bind time).

In the portal: *Connections → New →* set **Direction = inbound** and a **listen
port**. Create one per listener:

| Connection | mode | listen_port | role |
|---|---|---|---|
| `switch_in` | inbound | 8101 | the switch listens here |
| `issuer_a_in` | inbound | 8201 | issuer A listens here |
| `issuer_b_in` | inbound | 8202 | issuer B listens here |
| `issuer_c_in` | inbound | 8203 | issuer C listens here |

API shape (scenario-service, `PUT /connections/{name}`):

```json
{
  "adapter": "tcp",
  "protocol": "iso8583",
  "mode": "inbound",
  "listen_port": 8101,
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big" }
}
```

---

## Step 2 — Create the outbound connections the switch will call

The group routes across **outbound** connections (`mode: "outbound"`, the default
— the worker dials `host:port`). Point each at the corresponding issuer's listen
port.

| Connection | host | port | targets |
|---|---|---|---|
| `issuer_a_out` | 127.0.0.1 | 8201 | issuer A listener |
| `issuer_b_out` | 127.0.0.1 | 8202 | issuer B listener |
| `issuer_c_out` | 127.0.0.1 | 8203 | issuer C listener |

Keep the host/port abstract and let the **per-environment override matrix** supply
real values per env (`sim`, `staging`, …). A single outbound connection can also
hold several `endpoints[]` with a `selection` policy if one logical issuer has
multiple addresses — that's connection-level multiplicity, independent of groups.

---

## Step 3 — Author the participant flows

In the portal the flow editor *is* the scenario canvas in flow mode
(`/flow-editor/:id`): same drag/wire/inspector, with a flow palette
(`trigger` / `reply` / `state`) and a **"⇄ Outbound → Call connection"** node.
Set the **"listens on"** dropdown to the flow's inbound connection.

**Issuer flow** (`issuer_flow`) — pure responder, no outbound hop:

1. `trigger` node — listens on `issuer_a_in` (the trigger connection is repointed
   per instance by the topology/launch; see note in Step 6). The parsed request is
   injected into context as `${request.*}`.
2. (optional) `state` node — `incr` an approval counter (`${state.approvals}`).
   State is held per running instance, persisted across messages.
3. `reply` node — build the `0210`: echo fields, set DE39 = `"00"`.

**Switch flow** (`switch_flow`) — receives, calls an issuer, returns its answer:

1. `trigger` node — listens on `switch_in`; request available as `${request.*}`.
2. **Outbound Call** node (an `action`) — `config.connection = "issuers"` (the
   group name, created in Step 4). Map the inbound fields into the call payload
   with `${request.*}`. The response comes back as `${node.response.*}`.
3. `reply` node — set DE39 from `${node.response.de.39}`, echo the rest, send the
   `0210` back on `switch_in`.

A flow's saved shape (scenario-service, `PUT /participant-flows/{id}`):

```json
{
  "name": "switch_flow",
  "trigger": { "connection": "switch_in" },
  "nodes": [
    { "id": "t1", "kind": "trigger" },
    { "id": "call1", "kind": "action",
      "config": { "connection": "issuers", "payload": { "mti": "0200" } } },
    { "id": "r1", "kind": "reply",
      "action": { "set": { "de.39": "${call1.response.de.39}" } } }
  ],
  "edges": [
    { "from": "t1", "to": "call1" },
    { "from": "call1", "to": "r1" }
  ],
  "variables": {}
}
```

> Note on `assertions`: on a flow node, a Step's `assertions` field is repurposed
> as a **guard / match condition** (does this rule apply to this message?), not a
> pass/fail verdict.

---

## Step 4 — Create the participant group (the caller-side fleet)

Group the three outbound issuer connections under one logical participant with a
selection policy. Portal: *Configure → Participant Groups → New*.

scenario-service `PUT /participant-groups/{id}`:

```json
{
  "name": "issuers",
  "description": "Three issuer stand-ins",
  "adapter_type": "tcp",
  "members": [
    { "connection": "issuer_a_out", "weight": 2 },
    { "connection": "issuer_b_out", "weight": 1 },
    { "connection": "issuer_c_out", "weight": 1 }
  ],
  "selection": { "policy": "weighted" }
}
```

Policies: `round_robin`, `weighted`, `random`, `failover`, `sticky`. For
`sticky`, set `key` to a request field path so a given value always lands on the
same member — e.g. `"key": "de.2"` to route by BIN/PAN. The worker's
`GroupAdapter` selects a member per dispatch (reusing `EndpointSelector`); the
orchestrator inlines each member's resolved connection config into the run env at
launch (`_attach_groups`), and the registry recognizes `adapter: "group"`.

Because Step 3's Call node sets `config.connection = "issuers"`, the switch
dispatches to the group, and the policy picks the issuer per message.

---

## Step 5 — Create the network flow (the callee-side unit)

Place the flows to run (× instances) as `participant` nodes on the network
canvas and wire who calls whom. Portal: *Simulated Network → Network Flows →
New*. (ADR-0004: network flows replaced topologies; existing topologies were
migrated automatically with the same ids.)

scenario-service `PUT /network-flows/{id}`:

```json
{
  "name": "uk_card_net",
  "description": "switch + 3 issuers",
  "nodes": [
    { "id": "issuers", "kind": "participant",
      "config": { "flow_id": "issuer_flow", "instances": 3 } },
    { "id": "switch", "kind": "participant",
      "config": { "flow_id": "switch_flow", "instances": 1 } }
  ],
  "edges": [ { "source": "switch", "target": "issuers" } ]
}
```

**Wiring replaces manual ordering.** An edge means "source sends traffic to
target", and the start order is derived from it (callees first), so the issuers
are listening before the switch comes up and calls them. With no edges, node
list order is the start order. `instances: 3` starts three independent
listeners of `issuer_flow` — that's the fleet the `issuers` group routes
across. A network flow can also place `scenario` nodes (auto-run traffic
initiators), `simulator` nodes (saved simulators brought up with the network)
and `group` nodes (wiring targets).

---

## Step 6 — Start the network flow

Portal: *Network Flows → Start*. API: orchestrator
`POST /network-flows/{nid}/start`.

What happens (`start_network_flow` → `_launch_participant`): the orchestrator first
**plans a distinct listen port per instance** — instance _i_ of a flow binds
`listen_port + i` (or an ephemeral port when the inbound connection has no fixed
`listen_port`), and the whole plan is checked for `host:port` collisions up front
(**HTTP 409** if two instances would clash, before anything binds). Then, in
wiring order, for each instance it fetches the flow + connections, **repoints
every outbound action node's `target` onto its picked `config.connection`** (so
the switch's Call node dispatches to the group), starts a `FlowResponder` on its
planned port, and registers it. If any instance fails to start, it **rolls back**
everything already started. On success you get a `run_id`, the started
participants (each with its bound `port`), and a `health` block
(`{total, live, ready}`).

Check it's up:

- `GET /participants` — every listener with bound port, message count, last reply.
- `GET /topology-runs` — the live run and its instance count.

Stop the whole network with `DELETE /topology-runs/{run_id}` (stops every
instance together). Stop a single listener with `DELETE /participants/{pid}`.

---

## Step 7 — Drive it with a scenario (with a provisioning gate)

Build a normal scenario whose outbound connection points at the switch's listener
(`switch_in`'s host/port for this environment). To guarantee the network is up
first, set `requires_topology` on the run (the field name is historical — it
takes a network-flow id): `create_run` returns **HTTP 424** until that network
is live, so the run can't fire against a half-built network.

Run it (portal *Run*, or `run_scenario`). The path is:

```
scenario 0200 → switch_in → switch_flow → Call(group "issuers")
              → issuer_x_out → issuer_x_in → issuer_flow → 0210
              → back to switch_flow reply → 0210 to the scenario
```

---

## Step 8 — Observe, then tear down

- `GET /participants/{pid}` — per-flow stats (`by_mti`, `by_response_code`) plus
  the recent decoded request log. Also surfaced per-flow on the portal
  *Participants* page.
- Run report **Trace** tab — wire-level + structured engine log per step with a
  timing waterfall (cross-hop correlated trace is the one piece still deferred).
- `DELETE /topology-runs/{run_id}` — stop the network as a unit.

---

## Endpoint quick reference

```
scenario-service (config registry)
  GET/PUT/DELETE  /connections/{name}
  GET/PUT/DELETE  /participant-flows[/{id}]
  GET/PUT/DELETE  /participant-groups[/{id}]
  GET/PUT/DELETE  /network-flows[/{id}]
  GET             /network-flows/{id}/plan      -> start order (dry run)
  POST            /network-flows/validate

orchestrator (lifecycle)
  POST    /participants/start        { flow_id, port? }
  GET     /participants
  GET     /participants/{pid}        -> stats + recent log
  DELETE  /participants/{pid}
  POST    /network-flows/{nid}/start -> { id (run_id), participants[], simulators[] }
  GET     /topology-runs
  DELETE  /topology-runs/{run_id}
```

## Common pitfalls

- **Wrong direction.** The trigger connection must be `mode: "inbound"` with a
  `listen_port`; Call-node connections must be `outbound`. The portal filters the
  pickers by direction.
- **Topology order.** Callers listed before callees → the caller starts before its
  target is listening. List issuers first, switch last.
- **Group vs pool.** Participant **groups** (member connections) are distinct from
  card/terminal **data pools** (`${pool.*}`). Don't conflate them.
- **Port collisions across instances.** `start_topology` now allocates a distinct
  port per instance (`listen_port + i`, or ephemeral when `listen_port` is unset)
  and rejects a colliding plan with **HTTP 409** before binding. Point a group's
  member endpoints at the resulting `base … base+N-1` range. (Two *different*
  flows pinned to the same fixed port still collide — give them distinct ports.)
- **Run fires too early.** `requires_topology` is a real readiness gate: it
  returns **HTTP 424** unless a run of that topology exists *and every listener is
  still bound* (`health.ready`), not merely that a run record exists.
```
