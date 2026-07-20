# PayProbe Platform Architecture

End-to-end payment test platform: **author → simulate → orchestrate → load →
verdict**. This is the mid-level tour of how the pieces fit; the *reasoning*
behind the shape — what was decided, what was rejected and why — lives in
[ATLAS.md](../ATLAS.md), with formal decisions in [the ADRs](../adr/).

## System diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                        Angular 22 Portal  (:8080 via nginx)         │
│  constructor · networks canvas · maps · playground · run monitor    │
│  load console · inspector · reports · trends · vault · settings     │
└───────────────────────────────┬────────────────────────────────────┘
                                │ REST + WebSocket/SSE  (nginx proxy)
     ┌──────────┬───────────────┼──────────────┬─────────────┬───────────┐
     ▼          ▼               ▼              ▼             ▼           ▼
┌─────────┐┌───────────┐┌──────────────┐┌──────────┐┌───────────┐┌──────────┐
│  auth   ││ scenario- ││ orchestrator ││   mcp-   ││ assistant ││ insight- │
│ service ││  service  ││              ││  server  ││           ││ service  │
│  :8300  ││   :8000   ││    :8100     ││  :8200   ││   :8400   ││  :8500   │
└─────────┘└─────┬─────┘└──────┬───────┘└──────────┘└───────────┘└──────────┘
                 │             │
          ┌──────▼─────────────▼───────┐     ┌────────────────────────────┐
          │   PostgreSQL │ Redis       │     │ 3-node NATS JetStream      │
          │  scenarios   │ run streams │     │ cluster (external broker,  │
          │  runs        │ load bus    │     │ chaos-tested from outside) │
          │  configs     │ fleet bus   │     └────────────────────────────┘
          └──────┬─────────────┬───────┘
                 │             │
        ┌────────▼─────────────▼──────────┐
        │   Worker engine (asyncio+uvloop) │   + opt-in fleet:
        │  scenarios · participant flows   │   worker.load_worker (load)
        │  simulators · load drivers       │   worker.flow_host (listeners)
        └────────────────┬────────────────┘
                         │ adapter calls
        ┌────────────────▼────────────────┐
        │ tcp_iso8583 · http · grpc ·     │
        │ nats · hsm (payShield) ·        │
        │ db_probe · insight · mock_*     │
        └────────────────┬────────────────┘
                         │
        ┌────────────────▼────────────────┐
        │ Simulated or real targets:      │
        │ switch · issuer · HSM · gateway │
        │ broker · database …             │
        └─────────────────────────────────┘
```

Prometheus and Grafana ride alongside (`--profile observability`); nginx
routes `/api/scenarios/*` → scenario-service and `/api/orch/*` → orchestrator
(REST + `ws`).

## The three graph levels (ADR-0004)

One `Step`/`Edge` substrate, three artifact types, three meanings:

| Level | Artifact | Runs how | Edge means | Verdict |
|---|---|---|---|---|
| 1 | **Scenario** | run-to-completion in the worker | "then" (control flow) | pass/fail + assertions |
| 2 | **Participant flow** | long-lived listener (trigger→logic→reply) | "then" | none — it *serves* |
| 3 | **Network** | started/stopped as a unit | "sends traffic to" (wiring) | health (live/total/ready) |

A **network** wires participants, simulators and traffic drivers on a canvas:
start order is a topological sort of the wiring, ports are planned before
anything binds (collisions are a 409 *before* any listener starts), partial
starts roll back completely. NATS participants are port-less — they claim
`(server, subject, queue_group)` instead of a port (ADR-0006).

## Component responsibilities

### Portal (`packages/portal`, Angular 22)
Standalone components, signals, OnPush, the `pp-*` design system. Key pages:
Dashboard (KPIs + two health panels), Scenario Constructor (visual editor +
"✨ Ask AI"), Networks canvas, the three map surfaces (**Network Control**,
**Live Network Map**, **Network Replay**/Chronoscope), Run Monitor, Load
console, Playground (ad-hoc execution), ISO 8583 Inspector, Reports/Trends,
Resilience, Secrets vault, Users (RBAC), Settings, Docs.

### Auth service (`packages/auth-service`, :8300)
JWT auth with a **fail-closed** gate: outside dev, no valid credential ⇒ 401,
and if no auth is configured at all the service answers 503 rather than
serving openly. Issues user tokens; services mint short-lived HS256 service
JWTs from the shared secret. RBAC (admin/user/viewer) drives portal
navigation; a last-admin guard prevents lockout.

### Scenario service (`packages/scenario-service`, :8000)
The config registry: scenarios (versioned), connections, message formats,
starter flows, environments + the per-environment override matrix, tables
(card/terminal pools, BIN ranges, keys), networks, participant flows,
projects, secrets (SecretBox-encrypted, masked everywhere). Also hosts the
in-process half of the assistant tool layer (see below).

### Orchestrator (`packages/orchestrator`, :8100)
Runtime truth and lifecycle: runs (durable `RUN_DB`, JUnit/HTML reports),
listeners, simulators, network runs, distributed load coordination, chaos
storms, resilience certification, schedules, network trace, playground,
diagnostics doctor. All config resolution (connections, groups, environments,
fleet endpoints) happens here — the worker receives finished payloads.

### Worker engine (`packages/worker`)
Single-event-loop execution (asyncio + uvloop): scenario runs, participant
flows (same graph executor), all adapters and simulators, load drivers.
Also ships the opt-in fleet roles — `load_worker` (load generation shards)
and `flow_host` (hosts listeners across machines), coordinated over Redis
with heartbeats, claim-from-queue, and endpoint advertising (ADR-0001).

### Report service (`packages/report_service`)
A shared **library** (not a running service) the orchestrator imports:
metric aggregation, HTML reports, trends/flakiness, gates + provenance +
sign-off snapshots (ADR-0003).

### MCP server (`packages/mcp-server`, :8200)
FastMCP proxy over the whole platform — tools are registered from a metadata
table with behaviour hints (read-only / destructive / idempotent), plus
prompts and read-only resources. HTTP (streamable) and stdio transports; a
generated catalog page in the portal documents every tool. This is what lets
Claude (or any MCP client) configure and drive the platform end to end.

### Assistant (`packages/payprobe-assistant`, :8400)
The standalone multi-turn config assistant (SSE streaming, sessions). The
tool layer lives ONCE in `payprobe_common/agent_toolkit.py` against a
`Backend` protocol — REST backend here, stores backend in scenario-service —
so both stay in parity. Every write is journalled and reversible
(session-wide undo); guardrails live in the dispatch layer, not the prompt.
The LLM key is managed in Settings → AI assistant (env `ASSIST_LLM_*` is the
prod override).

### Insight service (`packages/insight-service`, :8500)
Advisory ML (ADR-0005): failure categorization, explanations, outcome
prediction. Read-only and advise-only by design — it never gates a run; the
heuristics baseline is the honest yardstick the learned layer must beat.

## Execution paths

**Functional run:** portal/API → orchestrator resolves environment,
connections, test data → worker executes the step graph (assertions decide
pass/fail) → events stream over the durable Redis backbone (see
[streaming](streaming.md)) → report with gates/provenance, optional Go/No-Go
sign-off.

**Load run:** profile (steady/ramp/spike/soak) → shards claimed by
Redis-coordinated load workers (in-process fallback for small runs) → live
fleet-merged TPS/p95/p99, hot retune mid-run, backpressure visibility, drain
window → run-vs-run comparison. Design targets: 20K+ TPS / 100K connections
across a fleet (labeled as targets on purpose).

**Resilience certification:** chaos storm (outage/brownout/flapping on a
simulator's fault dial) + load run, observed and scored into a graded
certificate (availability, absorption, recovery, latency bands).

**Ad-hoc (Playground, ADR-0007):** execute any addressable element — a
connection×environment, a running simulator, a participant, a crypto function
— by reference; secrets resolve server-side and never round-trip; history +
re-fire + save-as-scenario promote explorations into authored tests.

## Design decisions (the short list)

- **Single asyncio event loop** (uvloop): payment traffic is IO-bound; one
  loop with bounded concurrency beats thread pools for tens of thousands of
  concurrent connections.
- **PostgreSQL + Redis Streams, not Kafka:** durable config/run storage plus
  a replayable event backbone; reconnect-resume for live views. Kafka's
  complexity buys nothing at this scale.
- **External NATS JetStream cluster as infrastructure** (ADR-0006): the
  broker is a *target and transport*, like Postgres/Redis — never
  PayProbe-managed, chaos-tested from the outside.
- **Adapter pattern:** protocol complexity hides behind a uniform interface;
  a new target system is a new adapter, not an engine change. gRPC is
  descriptor-driven (no stub codegen) with reflection discovery.
- **The connection is the shape; the environment matrix is the values.** One
  `port`, direction on `mode`; multiplicity is a *group* concern
  (round-robin/failover/weighted/sticky).
- **Fail-closed auth everywhere**, secrets encrypted at rest and masked in
  every API, code steps sandboxed (netns).
- **Verdicts are evidence-grade:** reports carry explicit gates and
  provenance (what ran against what, runtime-resolved); sign-off snapshots
  are immutable and tamper-evident.
- **Assistants get reversibility, not confirmation dialogs:** every write
  journalled with its `before` state; whole-session undo.

## License

PolyForm Noncommercial 1.0.0 — source-available; free for noncommercial use;
commercial licensing by contact. See [LICENSE](../../LICENSE) and the README's
license section for the reasoning.
