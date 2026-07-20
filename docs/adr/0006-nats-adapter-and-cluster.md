# ADR-0006: NATS support — messaging adapter family + external JetStream cluster

**Status:** Implemented (with truthful scope — see action items)
**Date:** 2026-07-14
**Deciders:** PayProbe maintainers (David + reviewers)

> **Implementation note (2026-07-14).** All six phases landed. Backend + tests
> are green per-package (`nats-py` is an optional dep — its absence *skips*, not
> fails, exactly like `aiohttp`/`grpcio`). Portal surfaces (simulator preset,
> adapter-catalog spec, typed connection editor) were written to the existing
> patterns but **built only on the host** — the Angular app can't be compiled in
> the AI sandbox, so those are not claimed as verified. TLS to the cluster and
> wildcard-subject overlap detection remain deferred as recorded below.

## Context

Every transport PayProbe can exercise today is point-to-point: the universal
TCP adapter (ISO 8583 / header-echo), the generic `http` adapter, and the
descriptor-driven `grpc` adapter (`packages/worker/adapters/registry.py`).
Modern payment stacks increasingly put a message broker between participants —
event-driven authorization pipelines, ISO 20022 rails, webhook fan-out,
clearing-file notification buses. NATS (core pub/sub, request/reply, queue
groups, JetStream persistence) is a common choice for exactly these systems,
and PayProbe currently cannot act as either side of a NATS conversation. A
team testing a NATS-based switch or event pipeline has no way to model it as a
digital twin.

Unlike TCP/HTTP/gRPC, NATS is **broker-mediated**: nobody dials anybody.
Producers and consumers both connect *out* to a NATS server and rendezvous on
subjects. That breaks two assumptions baked into the current runtime:

- **Listeners bind ports.** `TcpResponder`/`HttpResponder` bind a
  `(host, port)`; the network-flow planner allocates ports per instance and
  409s on collisions *before* anything starts (invariant #4, enforced in
  `orchestrator/api/main.py` topology-run planning). A NATS "listener" binds
  nothing — it subscribes to a subject. The uniqueness resource is
  `(server_url, subject, queue_group)`, not `(host, port)`.
- **The broker is infrastructure, not a participant.** TcpResponder *is* the
  server; a NATS responder needs a server to exist first. That server should
  itself be chaos-testable (kill a node, watch queue-group members fail over)
  — which argues for a real multi-node cluster in `infra/`, treated the way
  Redis/Postgres are: compose services, not PayProbe-managed simulators.

### Forces

- **Digital-twin fidelity.** A single-node NATS container cannot exercise the
  behaviors payment teams actually certify against: node loss, JetStream
  replica failover, queue-group rebalancing. The resilience/chaos subsystem
  (chaos storms, resilience certification) becomes far more valuable against a
  real 3-node cluster.
- **Invariants must hold across a new transport.** Port planning (#4),
  stop-ownership (#5), single-`port` connections (#7), secrets at rest (#8),
  and the edge-semantics partition of `NodeKind` (#1) all need a defined NATS
  interpretation, not an exemption.
- **Optional dependency discipline.** `registry.py` registers adapters lazily
  so a missing dep degrades to "adapter unavailable", never an import crash
  (grpcio, aiohttp precedent). The NATS client (`nats-py`) must follow the
  same pattern, and its absence in a test environment is environmental, not a
  regression (see "Running tests" in CLAUDE.md).
- **Payload realism.** Payment messages over NATS are usually JSON events, but
  ISO 8583 bytes over a subject is a real deployment shape. The adapter should
  reuse the existing Message Format registry / ISO 8583 codecs rather than
  inventing a NATS-only encoding story.
- **One tool layer** (invariant #3): any assistant/MCP exposure is one handler
  in `payprobe_common/agent_toolkit.py` + one primitive per backend, and the
  MCP portal catalog must be regenerated after registry changes.

## Decision

Add NATS as a **first-class adapter family** — outbound client adapter,
rules-driven responder, and flow responder — talking to an **external 3-node
NATS cluster with JetStream** that lives in `infra/` alongside Redis and
Postgres. PayProbe never embeds or manages the broker; it connects to it, and
chaos-tests it from the outside.

Concretely:

1. **Infra:** `nats-1`/`nats-2`/`nats-3` services in
   `infra/docker/docker-compose.yml` (official `nats:2.x` image), full-mesh
   cluster routes, JetStream enabled with per-node file-store volumes, client
   ports 4222/4223/4224 mapped, monitoring port 8222 per node for
   healthchecks (`/healthz`). Own compose section under the data layer; same
   `restart` anchor and healthcheck conventions as Redis.
2. **Outbound:** `NatsAdapter(BaseAdapter)` in
   `packages/worker/adapters/nats/adapter.py`, registered lazily under key
   `"nats"`. Actions: `publish`, `request` (request/reply with timeout),
   `js_publish` (JetStream, ack-checked). `health_check()` = connect + RTT
   ping, never raises. Payload codecs: `json` (default), `bytes`
   (base64/hex), `iso8583` (reuse the existing dialect codecs via
   `message_format_id`).
3. **Inbound:** `NatsResponder` — same duck-typed surface as
   `TcpResponder`/`HttpResponder` (`start`/`stop`/`stats`/`peers`/
   `set_chaos`/`reset_stats`) but `start()` subscribes to configured subjects
   (optionally in a queue group) instead of binding a port. Rules match on
   subject + decoded payload; `respond` publishes to the reply inbox (or a
   configured subject). Chaos surface (drop / delay / error) works
   identically, so the chaos dial and storms apply unchanged.
4. **Flows:** `NatsFlowResponder(FlowTraceMixin, NatsResponder)` with
   `TRACE_KIND = "nats"`, dispatched from `_build_flow_responder` in
   `orchestrator/api/main.py` by connection adapter — exactly the
   `HttpFlowResponder` precedent. Trigger nodes match on subject; reply nodes
   answer the request inbox. Network Trace hops record subject + queue group
   where TCP records MTI/DE 39.
5. **Connections:** `adapter: "nats"` connections use the existing single
   `port` field as the *broker client port* (invariant #7 intact — one port,
   direction still expressed by `mode`). `host` is the broker address;
   NATS-specific keys (`servers[]` for multi-URL, `subject`, `queue_group`,
   `codec`, `request_timeout_sec`, `jetstream` block, `auth`) ride on
   `ConnectionDraft`'s `extra="allow"` and the per-environment override
   matrix. Credentials/tokens/nkeys are SecretBox-encrypted and masked
   (invariant #8) like every other connection secret.
6. **Planning:** the network-flow planner treats NATS participants as
   **port-less** — they claim `(server_url, subject, queue_group)` instead.
   Same subject + same queue group across instances is *legal fan-out*
   (that's what queue groups are for); same subject with **no** queue group
   claimed twice in one plan is a 409 at plan time, before anything
   subscribes — the NATS reading of invariant #4.
7. **Stop-ownership** (invariant #5): a run drains only the subscriptions it
   created, deletes only JetStream consumers it created, and **never**
   deletes streams it found pre-existing or touches the cluster itself.

## Options considered

### Option A — First-class adapter family + external JetStream cluster (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium — one new adapter dir + responder pair + planner extension; broker is stock containers |
| Fidelity | High — real clustering, real failover, chaos-testable broker |
| Ops cost | Three small containers + volumes; healthchecked like Redis |
| Team familiarity | Follows the exact `http` adapter + `HttpFlowResponder` precedent |

**Pros:** matches every existing pattern (lazy registry, responder duck type,
FlowTraceMixin, connection matrix); the cluster doubles as a chaos target;
JetStream unlocks durable-consumer test scenarios payment teams actually need.
**Cons:** planner must learn a second claim type (subjects); three more
containers in dev compose; `nats-py` joins the optional-deps list.

### Option B — Embedded per-participant NATS server (PayProbe-managed broker)

Make the broker a saved simulator: starting a NATS participant boots a NATS
server process the way `TcpResponder` binds a port.

**Pros:** no compose changes; port planning works unmodified.
**Cons:** inverts reality — in production the broker is shared infrastructure,
not owned by one participant; single-node only, so no failover or JetStream
replication to test; PayProbe would own broker lifecycle, violating the
stop-ownership spirit (who stops a broker two networks share?); embedding a Go
server binary in the Python worker is a packaging headache. **Rejected.**

### Option C — Bridge through existing adapters (relay/HTTP gateway)

No native support; reach NATS via a sidecar bridge (e.g. an HTTP→NATS
translator container) driven by the existing `http` adapter, or a `relay`
node.

**Pros:** zero new adapter code.
**Cons:** cannot model queue groups, inboxes, JetStream acks, or subject
wildcards; traces show the bridge, not the broker conversation; every user
must operate a translator that PayProbe exists to make unnecessary.
**Rejected.**

### Option D — Core-NATS-only or single-node cluster

Same adapter work as Option A but a single `nats` container, or a cluster
without JetStream.

**Pros:** lighter; JetStream adds stream/consumer lifecycle surface.
**Cons:** removes exactly the behaviors worth certifying (node loss,
replicated-stream failover, at-least-once redelivery). Since the adapter code
is identical, the saving is three lines of compose vs. losing the resilience
story. **Rejected** — though phase 1 below is functionally usable against a
single node, so a laptop-constrained dev can run a trimmed override file.

### Deferred (out of scope, recorded on purpose)

**NATS as PayProbe's internal coordination bus.** Redis currently carries
fleet heartbeats, load coordination, trace streams, and run control. NATS
could do this, but swapping a working internal bus is unrelated to letting
users *test* NATS systems, and would churn ADR-0001 machinery for no user
value. Not planned; revisit only if Redis becomes a scaling problem.

## Trade-off analysis

The real decision is **who owns the broker**. Option B makes PayProbe the
broker's parent (simple planning, false world); Option A makes the broker
environment (true world, planner must learn subjects). PayProbe is becoming a
digital twin of a payment network — the twin has to include the messaging
fabric as *fabric*, or NATS-based DUT topologies can't be mirrored. The
planner extension is bounded: subject claims are a dict keyed by
`(url, subject, queue_group)` checked in the same pre-start loop that checks
`(host, port)` today.

JetStream-from-day-one (A over D) is justified by asymmetry: enabling it costs
one config flag + volumes now, while retrofitting durable-consumer semantics
into an adapter designed around core NATS costs a redesign of the responder's
ack path later.

## Consequences

- **Easier:** testing event-driven payment systems end-to-end; broker-failure
  resilience certification (chaos storm kills `nats-2`, scorecard shows
  absorption/recovery); modeling webhook/event fan-out with queue groups.
- **Harder:** the planner has two claim vocabularies (ports and subjects) —
  validators and error messages must keep them distinct; diagnostics
  (`/diagnostics`) should learn a NATS layer (cluster reachable → JetStream
  enabled → subjects claimed).
- **New invariant interpretation to document in CLAUDE.md** once accepted:
  NATS participants are port-less; subject claims replace port claims;
  invariant #4's wording gains "(or subject)".
- **Revisit later:** wildcard-subject overlap detection at plan time (`pay.>`
  vs `pay.auth.*` collide semantically but not textually — phase 1 checks
  exact matches only, documented limitation); TLS to the cluster (aligns with
  the ADR-0002 TLS deferral); Grafana panels from NATS `/varz`.

## Action items (phased — implementation handover)

Each phase lands green before the next starts; tests per package with
`PYTHONPATH=packages` from `packages/` as usual. `nats-py` joins the optional
test deps (`pip install nats-py --break-system-packages`); its absence must
skip, not fail, collection (guard imports like `aiohttp`/`grpcio`).

1. [x] **Phase 0 — cluster infra.** `nats-1/2/3` in
   `infra/docker/docker-compose.yml`: `nats:2.11-alpine`, `--cluster_name
   payprobe --cluster nats://0.0.0.0:6222 --routes
   nats://nats-1:6222,nats://nats-2:6222,nats://nats-3:6222 --jetstream
   --store_dir /data -m 8222`, one volume per node, healthcheck on
   `:8222/healthz`, client ports 4222/4223/4224 published. Optional trimmed
   single-node service in `docker-compose.dev.yml`.
2. [x] **Phase 1 — outbound adapter.** `worker/adapters/nats/adapter.py`
   (`publish` / `request` / `js_publish`, codec layer incl. `iso8583` via
   Message Format registry, `${vars.*}`/`${card.*}` interpolation untouched —
   it happens upstream), lazy registration in `registry.py` under `"nats"`,
   connection shape + env-override support, `test_connection` support. Tests:
   adapter unit tests against a mocked client + one opt-in integration test
   gated on a reachable broker.
3. [x] **Phase 2 — responder + simulator preset.** `NatsResponder`
   (subjects/queue group, rules, chaos, stats/peers), `kind: "nats"` branch in
   `_responder_for`, portal saved-simulator preset ("NATS responder"),
   Prometheus gauges reuse (`SIMULATOR_*` labeled series).
4. [x] **Phase 3 — flows + networks.** `NatsFlowResponder` +
   `_build_flow_responder` dispatch; trigger/reply semantics over inboxes;
   subject-claim planning + 409 in the topology-run planner; stop-ownership
   (drain own subscriptions, delete own JS consumers only); Network Trace hop
   fields (subject, queue_group, inbox); fleet placement note — NATS
   participants need no fixed listen port, so fleet endpoint discovery
   records the subject claim instead.
5. [x] **Phase 4 — JetStream + load.** Stream/consumer declaration on the
   connection (`jetstream: {stream, subjects[], durable, ack_wait}`),
   created-if-missing and ownership-tracked; load engine target support
   (request/reply TPS through queue groups); resilience-run recipe: storm
   kills one node, certificate scores recovery.
6. [x] **Phase 5 — surfaces + docs.** `adapter-catalog.ts` spec entry (portal
   builds on host only — say so, don't claim verification); connection editor
   typed fields; diagnostics NATS layer; MCP/assistant exposure if warranted
   (one agent_toolkit handler + backend primitives, then
   `gen_catalog.py`); update CLAUDE.md invariant #4 wording, ATLAS roadmap,
   and this ADR's status to Implemented-with-truthful-scope.
