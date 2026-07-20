# PayProbe Atlas — the architecture, with its reasoning

This is the handover document: not what the code does (the code says that),
but what we **decided and learned** building it — why the system has the shape
it has, which alternatives were rejected and why, and what should happen next.
Written 2026-07-07 as a knowledge transfer from the AI collaboration that
built most of the platform between 2026-06-19 and 2026-07-06. Companion to
[`CLAUDE.md`](../CLAUDE.md) (the short rulebook).

---

## 1. The thesis

PayProbe started as a scenario tester (send ISO 8583, assert on the response)
and grew, decision by decision, into something more specific and more
valuable: a **digital twin of a payment network**. The end state it is
converging on: model your acquirers, switches, issuers, HSMs and gateways as a
wired network; drive realistic traffic mixes at production rates; inject
faults; and walk away with an evidence-grade verdict — a resilience
certificate, a compliance percentage, a signed Go/No-Go.

That thesis explains most architectural choices below. When in doubt about a
feature's design, ask: _does this make the simulated network more faithful,
or the verdict more trustworthy?_ Features that do neither have historically
been cut (see §10).

## 2. The three graph levels (ADR-0004)

One `Step`/`Edge` substrate, three artifact types, three meanings:

| Level | Artifact             | Runs how                                  | Edge means                  | Verdict                   |
| ----- | -------------------- | ----------------------------------------- | --------------------------- | ------------------------- |
| 1     | **Scenario**         | run-to-completion in the worker           | "then" (control flow)       | pass/fail + assertions    |
| 2     | **Participant flow** | long-lived listener (trigger→logic→reply) | "then"                      | none — it _serves_        |
| 3     | **Network**          | started/stopped as a unit                 | "sends traffic to" (wiring) | health (live/total/ready) |

The deliberate part: **these are three document types, not one**. A full merge
("Option C", one `Flow` doc with a discriminator) was analysed and rejected in
ADR-0004 — run-to-completion and serve-forever are legitimately different
lifecycles; assertions and verdicts are meaningless for listeners. The
expensive unification (node schema, `GraphExecutor`, the visual editor) was
already shared; merging the documents would have bought churn, not capability.
Option C remains open for re-evaluation _only if_ the three stores/APIs start
duplicating logic — so far they don't.

Networks absorbed the older flat "Topology" (a manual callee-first list).
Wiring edges replaced manual ordering: start order is a topological sort,
`A → group → B` collapses to "A depends on B", cycles degrade to a warning
with list-order fallback. Migration reused topology ids so `requires_topology`
gates and bookmarks survived; the `/topologies` API and page were then removed
entirely — the run API (`/topology-runs`) intentionally survives under its old
name because it _is_ the run machinery networks execute through.

Node kinds on the network canvas and their contracts: `participant`
(flow_id × instances, optional base port), `scenario` (traffic initiator,
autostart, fired last, cancelled on stop, never an edge target), `simulator`
(saved simulator, started first, target-only), `group` (passive wiring target
that may fan out to participants — the only target-ish kind allowed outgoing
edges, because a fleet really does forward traffic).

**Auto-wiring** exists because the wiring is derivable: each flow's outbound
nodes name connections/groups, and connections carry ports —
`/network-flows/infer-wiring` resolves them against the other nodes' listener
ports. The Wire tool is an override, not a requirement. This was the single
biggest UX lesson of the project: _don't ask the user for what the system
already knows._

## 3. Execution homes

Three places code executes, and the boundary is principled:

- **Worker** (`packages/worker`): the engine. Scenarios (`GraphExecutor` →
  `ScenarioRunner`), participant flows (`FlowRunner` subclasses the same
  executor), all adapters and simulators, load drivers. The worker is
  deliberately _dumb about config_ — the orchestrator resolves everything
  (connections, groups, environments) and hands it finished payloads.
- **Orchestrator**: lifecycle and truth about runtime. Owns runs, listeners
  (`PARTICIPANTS`), simulators (`SIMULATORS`), network runs (`TOPOLOGY_RUNS`),
  load coordination, reports, diagnostics. All resolution logic
  (`_attach_connections/_groups/_step_environments/_fleet_endpoints`,
  `_prepare_flow_launch`) lives here so both the in-process and fleet paths
  share it.
- **Fleet** (ADR-0001, fully implemented): `worker.load_worker` generates
  load; `worker.flow_host` hosts listeners. Both coordinate through one
  bus (`InMemoryLoadBus` / `RedisLoadBus`) with the same patterns:
  heartbeat presence with TTL, claim-from-queue, advertise-into-registry.
  Fleet listener placement runs in callee-first **waves** so each wave's
  advertised endpoints re-point the next wave's outbound configs — that's
  what makes a switch on host B reach issuers on host A. Worker death is
  handled by re-placement with a cooldown; a re-placed placement is skipped
  by hosts if the instance is already advertised "up" (no duplicates from
  slow-but-alive hosts). Fleet mode is opt-in (`NETWORK_FLEET=1` + live
  hosts) precisely so dev/CI keep the zero-dependency in-process path.

Why the bus and not k8s per flow: analysed in ADR-0001 — k8s buys scale but
fights the ephemeral "spin up a network for one test run" workflow, couples
the product to a platform, and _still_ needs the endpoint registry. The
registry was the real work; it is reusable regardless of substrate.

## 4. The connection model — a hard-won story

This area burned the most iterations; the current model is the third design.

- A connection is **the shape** (adapter, protocol, framing); the
  **per-environment override matrix** is the single source of _values_
  (host/port per env). An earlier per-step connection×environment matrix and
  a per-step environment-override UI were built and then removed as
  misaligned.
- **Single `port`, both directions.** `listen_port` existed, caused split-
  brain bugs, and was folded into `port` (legacy docs self-heal on read).
  Direction lives on `mode: inbound|outbound`.
- **Connection-level `endpoints[]` multiplicity was built and then deleted.**
  It overlapped confusingly with participant groups. The rule that survived:
  _multiplicity is a group concern._ Groups are typed to one adapter family
  (validated + stamped), carry the selection policy (round_robin / weighted /
  failover / sticky-by-field), and are inlined into worker configs at
  launch. `selection.py` survives as the shared selector.
- Relay/proxy nodes are **dual-mode**: a terminal relay = transparent
  `TcpProxy` (byte-exact tap, decode or raw); a relay with a successor runs
  in-graph and exposes `${relay_id.response}`. This distinction fixed real
  bugs ("run HSM load through the network").
- payShield connections are `header_echo` on the wire — listeners and proxies
  force that protocol or they mis-decode host commands.

## 5. Simulators, chaos, and the verdict machinery

Simulators are worker classes registered in one from-config registry:
rules-driven `TcpResponder` (universal ISO 8583 / header-echo),
`PayShieldSimulator` (NC/A0/BU/CW/CY/CA/EC/M6/M8 with real crypto under a test
LMK), `VisaSimulator` (functional Base I: auth/reversal/network-mgmt/clearing

- opt-in CVV2/PVV/ARQC), `HttpResponder` → `CyberSourceSimulator` (REST
  decision ladder). "Spec-exact" VISA is explicitly Phase 2 — functional
  coverage first was a scoping decision, not an oversight.

On top: the **chaos dial** (live fault injection per simulator: latency,
drops, malformed frames) and timed **storms** (outage/brownout/flapping);
then **resilience certification** — a storm + load-run observed and scored
(availability, absorption, recovery, latency bands → gates → grade) into a
certificate at `/resilience`. Separately, functional runs feed **two-mode
reports** (improvement vs Go/No-Go) with explicit gates, provenance (what ran
against what — runtime-resolved endpoints are recorded for this), and an
immutable, tamper-evident sign-off snapshot (ADR-0003). MCP `certify_run`
exposes it.

The **trace** system stitches one transaction across every hop by correlation
id (DE 11 by default): per-listener ring buffers, `/participants/traces`
index, wire-level + engine-log per step with a timing waterfall on run
reports. Fleet hosts ship _new_ hops to a per-run bus stream with their
heartbeat (ts-watermarked), and the orchestrator merges them — so the Network
Trace page is fleet-transparent. Capture is off by default; the global toggle
propagates to fleet hosts over the bus.

## 6. The assistant

Two assistants, deliberately distinct: the scenario editor's one-shot
"✨ Ask AI" (NL → flow/scenario draft, catalog-grounded), and the **general
config assistant** — a multi-turn tool-calling agent (portal slide-over,
streaming SSE, sessions).

Its architecture is the repo's best pattern and worth copying:

- **One toolkit** (`payprobe_common/agent_toolkit.py`): 34 tools (23 read / 11 write) declared
  once — name, read/write tier, JSON schema, handler — against a `Backend`
  protocol of primitive resource ops. Two backends: stores (in-process in
  scenario-service) and REST (standalone `payprobe-assistant`). Both
  pre-unification test suites pass unchanged against the shared core; that
  parity proof is the standard for future refactors here.
- **Reversibility as the safety model.** The user chose autonomous writes
  with no per-change confirmation; the counterweight is the serializable
  change journal — every write records `before`, restore is pure data,
  sessions persist in Redis, and Undo rolls the whole session back newest-
  first. Advisor mode = read tier only.
- **Guardrails in the dispatch layer**, never the prompt.
- **Runtime visibility is read-only**: platform status, runs, network runs,
  listeners, simulators, load history. The assistant configures; the operator
  runs. That line is intentional — don't give the model start/stop.

Known assistant history worth remembering: tools missing a domain cause the
model to _misuse the nearest tool and confidently report nonsense_ (happened
with environments, then networks, then runtime state). The fix is always a
real tool with a description that disambiguates, added once in the toolkit.

## 7. Security model

Fail-closed JWT gate on every service (`PAYPROBE_ENV=dev|test` opens it for
local work); auth-service issues user tokens, services mint short-lived
HS256 service JWTs from the shared `AUTH_JWT_SECRET` (MCP server, assistant,
scenario-service→orchestrator bridge all use the same scheme). RBAC in the
portal (admin-only pages, nav filtering, last-admin lockout guard). Secrets:
`SecretBox` (`payprobe_common.crypto`, one implementation — the historical
scenario-service mirror is now a re-export shim; `enc:v1:` format) encrypts
secret-named fields at rest;
the Secrets vault page inventories without revealing; code-node execution is
sandboxed in a netns.

**Closed 2026-07-07:** the standalone assistant now runs the same fail-closed
caller gate as every other service (`assistant_service/auth.py`); it only ever
lacked it historically — do not remove it when touching main.py.

## 8. The portal

Angular 22 (upgraded from 20 on 2026-07-18), standalone components, signals, OnPush; the `pp-*` design system
(tokens, shared/ui) with a two-level sidenav. Conventions that matter: pages
own a `*.service.ts` with signal state; file-backed manage pages follow the
two-pane list+editor pattern; the network canvas and the scenario constructor
are **separate components on one Step/Edge model** — a deliberate choice
(threading a third mode through the 3k-line constructor risked the two
existing modes; the canvas's needs diverged anyway). Three topology-map pages
exist (map 1 controllable, map 2 big live diagram, map 3 "Chronoscope"
time-travel replay); map 2's containers and map 1's cards deep-link to the
canvas. Retiring map 1/2 in favour of canvas overlays is a queued idea, not
done.

The portal cannot be built in AI sandboxes (node_modules symlink issue) —
every portal change in this collaboration carried a "needs local
`npm run build`" caveat, and that debt exists **right now** for the canvas,
assistant panel and map changes.

## 9. Test culture

~1,275 Python tests across six suites, all offline (fakes for providers,
`InMemoryLoadBus` for fleet behaviour, `fakeredis` for the Redis
serialization paths, `FakeBackend` for the assistant's REST surface).
Patterns: real in-memory stores over mocks; scripted LLM callers; regression
tests are written from user-visible symptoms (e.g. "running 0/0 but 2 sims
up" became `test_simulator_only_network_health`). Environmental failure modes
and collection rules are in CLAUDE.md — trust them before debugging "broken"
crypto tests. CI (.github/workflows/ci.yml) runs every suite — worker, report-service, orchestrator, scenario-service, mcp-server, payprobe-assistant, the showcase guard test — plus portal build, Playwright golden-path smoke, and a docker-compose mock-integration run; `payprobe_common` is installed editable where imported.

## 10. Things we built and removed (so you don't rebuild them)

- Connection `endpoints[]` + routing adapter (overlapped groups).
- Per-step environment-override UI (matrix replaced it).
- The `/topologies` CRUD API, Topologies page, and MCP topology tools
  (absorbed by networks; run API kept).
- A connection×environment matrix at the step level (misaligned abstraction).
- `TestPay` sample adapter → renamed RestPay → folded into the generic `http`
  adapter (aliases removed).

The pattern: overlapping abstractions get cut in favour of the one with the
clearer owner. When two features answer the same question, one of them is
debt.

## 11. Roadmap — with the why

**Now (hygiene):**

1. Commit the current uncommitted batch; rebuild the two service images
   (their Docker build contexts changed with the toolkit unification).
2. `npm run build` the portal + click through canvas/assistant/maps — the
   accumulated unverified-UI debt.
3. Real-Redis fleet smoke: two `flow_host`s, `NETWORK_FLEET=1`, kill one
   host mid-run, watch re-placement + fleet trace stitching.

**Next (safety + polish):** 4. ~~Assistant caller-JWT gate~~ — DONE 2026-07-07 (same fail-closed gate as
the other services; `PAYPROBE_ENV=dev` opens it locally). 5. Standalone-assistant cutover — **flip DONE 2026-07-07**: portal
`assistantApiBase` now defaults to the standalone (dev `:8400`, prod
`/api/assistant`); a Settings → Endpoints override can still point back at
the shim during transition. Provider config (revised 2026-07-13): the
standalone is still the LLM egress boundary — only it calls providers —
but the key is managed in ONE place again: `ASSIST_LLM_*` env wins when
set (prod override); otherwise the service pulls the Settings →
AI assistant config from scenario-service over the service-gated
`GET /assist/config/material` (service JWT with `svc` claim, 403 for user
tokens — the same pattern as `/test-data/keys/{name}/material`, so
user-facing reads stay masked), cached ~15 s so Settings edits apply
without a restart. `ASSIST_SETTINGS_LLM=0` disables the fallback. **Shim retirement RE-SCOPED 2026-07-07 (same
lesson as §12 — check what a deletion actually costs before doing it):**
the routes stay as a _deprecated, test-anchored shim_. Deleting them
looked like ~150 lines of cleanup; pulling the thread showed the three
agent suites drive the StoresBackend + the sync→async scenario-store
bridge + session persistence through those routes — the in-process half
of the two-backend parity architecture §6 values. The tool layer is
already unified, so the drift risk the retirement was meant to kill is
gone; the routes are thin dispatch. Delete them only if the StoresBackend
path is itself ever dropped. Don't add features there (note is on the
routes); the standalone is the live surface and carries its own
endpoint-level suite (chat/revert/SSE/auth). 6. ~~Unify `crypto.py`~~ — DONE 2026-07-07 (scenario-service re-exports
`payprobe_common.crypto`). 7. Small paydowns: txn-type → starter-flow dropdown in the test console;
~~Grafana worker-RSS trend panel~~ — already existed (verified
2026-07-07: worker `read_rss_bytes` → heartbeat `rss_bytes` →
`payprobe_load_worker_rss_bytes{run_id,worker_id}` in load_coordinator →
two panels in `payprobe-load.json`, "Soak — worker RSS" + `deriv()` leak
rate; this list was stale); ~~crypto-key-by-name~~ — DONE 2026-07-07:
`${key.NAME}` anywhere in a scenario resolves to registry key material at
run build (orchestrator `_attach_test_data`, same best-effort pattern as
pools; unresolved tokens stay literal and fail loudly at the crypto step).
Material transits only the service-gated
`/test-data/keys/{name}/material` (403 for user JWTs — the vault's "reads
mask" holds for people; static bearer / `svc` JWT / open dev pass);
~~`/peers` outbound leg~~ — DONE 2026-07-07 for the in-process engine:
`worker/adapters/socket_registry.py` (register on connect / unregister on
drop+close, wired into TcpAdapter `_open`/`_on_disconnect`/`disconnect`),
merged into /peers `outbound` as `source: "engine"`. Sockets inside
separate load-worker processes are deliberately NOT reported — a
fleet-scale socket inventory (up to 100K conns) is a different problem.

**Then (product):** 8. ~~Showcase network~~ — DONE 2026-07-07: `scripts/showcase.py`
(`make showcase`) builds+starts driver → switch → 3 issuers + payShield via
the public APIs; `docs/getting-started/showcase.md`. `--certify` drives a
load run + payShield chaos storm and scores a resilience certificate — the
full thesis (spin up, stress, verdict) in one command. 9. Proxy-tap stage 2 (intercept/stub/capture/TLS) — ADR-0002's deferred half;
unlocks testing _against_ real endpoints with selective stubbing. 10. Spec-exact VISA (Phase 2) and further schemes (Mastercard is the obvious
next), leaning on the pack/certification machinery. 11. **Consolidate the topology-map surfaces — see §12.**

Also (proposed 2026-07-20): **Payment-provider integration** (ADR-0009,
proposed — not built). The mechanism analysis for integrating commercial PSPs
(Stripe, Adyen, PayPal first; the full MCP-landscape table is in the ADR).
Decision proposed: three planes on existing machinery — **provider packs**
over the generic `http` adapter for driving real sandboxes (one bounded code
change: an `oauth2_client_credentials` auth strategy in the shared http
runner, PayPal's requirement), **provider simulators** (`HttpResponder`
subclasses per the CyberSource precedent, plus a new webhook-emission option
so the simulated PSP calls the merchant participant back, chaos included),
and one **generic `mcp` client adapter family** (streamable HTTP + stdio) for
fixture provisioning and provider-side assertions as scenario steps — MCP as
control plane, explicitly rejected as transport. Guardrail: pack presets
pointing at real providers carry `external: true` and the load engine refuses
them (invariant #6 applied to other people's infrastructure). Per-provider
SDK adapters rejected (the TestPay lesson, §10); proxy-tap record-and-replay
noted as a post-ADR-0008 complement. Phasing: auth strategy + guardrail →
Stripe end-to-end → Adyen + PayPal → mcp adapter → webhook emission →
surfaces/docs.

Also (added 2026-07-16): **Playground — ad-hoc execution by reference**
(ADR-0007) — backend landed (phases 1–3): `GET /playground/targets` (read-only
merge of connections × override-matrix environments, running simulators,
participants incl. port-less NATS subjects + fleet instances, groups, crypto
functions), `POST /playground/execute` (server-side target resolve so
connection secrets never round-trip — the one genuine architectural gap the
ADR identified; execution fully delegated to `WorkerEngine._execute_step` /
`run_crypto`, echoes masked with the CaptureBuffer redaction rules), per-user
in-memory history + re-fire + save-as-scenario (the promote-to-authored-test
on-ramp), `playground_hits` contamination counter on running simulators, and
the assistant/MCP pair `playground_targets`/`playground_execute` — the latter
introduced a third toolkit tier, **`execute`** (fires traffic; never
journalled — invariant #2 is about config writes; advisor never sees it, plan
mode is execution-refused). Samples (added 2026-07-16): `/playground/targets`
now carries `samples` — ready-made example interactions per element family
(`samples.wire[protocol-or-adapter]`: iso8583/visa/header_echo/payshield/
http/cybersource/nats/grpc) plus one runnable parameter set per crypto
operation (`samples.functions.crypto`, coverage + fires-green enforced by
tests); pure data in `orchestrator/api/playground_samples.py`, surfaced as
one-click chips in the portal composer. Still owed: a host `npm run build` + click-through of the portal
`/playground` page (page + sample chips written 2026-07-16, unverified —
sandboxes cannot build the portal); deferred by design: trace-hop origin tagging (needs in-band correlation) and folding
`/hsm/command` into the playground (deprecate-don't-break, MCP tools
reference it).

Also (added 2026-07-14): **NATS messaging support** — ADR-0006 landed all six
phases. NATS is now a first-class adapter family (`packages/worker/adapters/nats/`):
outbound `NatsAdapter` (publish / request / JetStream publish, json/bytes/iso8583
codecs), rules-driven `NatsResponder`, and `NatsFlowResponder` — talking to a real
external 3-node JetStream cluster in `infra/docker/docker-compose.yml` (the broker
is infrastructure like redis/postgres, chaos-tested from the outside, never
PayProbe-managed). The key architectural change: NATS participants are **port-less**
— the network-flow planner learned a second claim vocabulary, `(server, subject,
queue_group)`, alongside `(host, port)` (see invariant #4). JetStream stream/consumer
declaration is created-if-missing and ownership-tracked (teardown removes only its
own consumers). `nats-py` is an optional dep (absence skips, like aiohttp/grpcio).
Still owed: a host `npm run build` + click-through of the portal pieces (simulator
preset, adapter-catalog entry, typed connection editor — unverifiable in AI
sandboxes); deferred by design are TLS to the cluster (aligns with ADR-0002's TLS
deferral) and wildcard-subject overlap detection (`pay.>` vs `pay.auth.*` — phase 1
checks exact subject matches only).

Also (added 2026-07-12): **insight-service follow-through** — the advisory ML
service (ADR-0005, `packages/insight-service`, :8500) shipped with its core
(failure categorization / explanation / outcome prediction, self-scoring
baselines) and platform wiring; portal surfacing, MCP tools ("Model insights
(advisory)" group), the shared `agent_toolkit` read tools (both backends),
and the in-service self-training loop (`INSIGHT_TRAIN_INTERVAL_SEC`, nightly
in compose) all landed the same day. Still owed: a host `npm run build` +
click-through of the portal pieces (unverifiable in AI sandboxes), and the
honest gate — if `error`/`unknown` stays under ~15% of failures the learned
layer never earns its keep and the heuristics win; that outcome is a
success, not a failure.

**Eventually (re-evaluate, don't assume):** 12. ADR-0004 Option C (single flow document) — only if the three stores start
duplicating logic. 13. k8s substrate for the fleet — only if bus+hosts hit real limits; the
endpoint registry carries over either way.

## 12. The topology-map surfaces (roadmap #11) — consolidation

There are **three "map" pages plus the canvas**, and the shared "Topology Map
1/2/3" naming made them _look_ like duplicates. They are not. **First-pass
lesson (2026-07-07): an attempt to merge Map 1 into Map 2 was reverted after
inspecting what each actually renders — they are different jobs.** Check the
rendered content, not the data source or the label, before merging anything
here.

**What exists today** (all under `packages/portal/src/app/topologies/`):

| Surface             | Route                                  | Nav label            | Job                                                                                                                                                          |
| ------------------- | -------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Map 1               | `/topology-map`                        | **Network Control**  | a **control board** — five sections (networks, routing groups, connections, standalone listeners, simulators) each with start/stop / enable-disable controls |
| Map 2               | `/network-map` (was `/topology-map-2`) | **Live Network Map** | the **live diagram** only — `/network-graph` nodes+edges, lane layout, animated data-exchange, activity feed. NO control sections.                           |
| Chronoscope (Map 3) | `/topology-map-3`                      | **Network Replay**   | **replay / offline review** — 8-min buffer, scrubber, chronicle, import/export                                                                               |
| Networks canvas     | `/network-flows`                       | (under Networks)     | **authoring** one network + its per-network live health overlay                                                                                              |

**Three distinct jobs — manage, watch, replay — plus per-network authoring.**
Map 1 (control) and Map 2 (diagram) share neither content nor purpose; Map 2
has none of Map 1's control sections, and those controls are only otherwise
reachable one-registry-at-a-time (Connections / Simulators / Groups /
Participants pages). Map 1's _combined_ board has real standalone value.

**The decision — clarity, not a merge:**

1. **Keep all three; rename for clarity.** DONE 2026-07-07: nav is
   "Network Control" / "Live Network Map" / "Network Replay" (not
   "Topology Map 1/2/3"); `/topology-map-2` redirects to the clearer
   `/network-map`. No component deleted, nothing lost.
2. **Keep Chronoscope separate** (a distinct mode); cross-link it from the
   live map ("replay the last 8 min").
3. **Keep the Networks canvas** — it is _per-network_ authoring; the maps are
   _global_ runtime (all running pieces incl. standalone). Different scopes.
4. **Cross-link the four** so they feel like views of one system — the
   map→canvas deep-links (already built) are the pattern to extend.
   DONE 2026-07-07 (items 2+4): a shared segmented switcher (Control /
   Live map / Replay, `topologies/map-switcher.component.ts`) sits in all
   three control bars, and the Live Network Map gained an additive node
   **action drawer** (`topologies/tm2-node-drawer.component.ts` — live stats,
   DE39/MTI breakdowns, deep links to simulator metrics & chaos, the flow
   editor, network trace, live sessions, diagnostics). The two maps also
   gained an honest **staleness state** (`shared/poll-health.ts`): failed
   polls flip Live → Stale with a banner + frozen animations instead of
   silently showing a dead board as live. All portal-only — needs a host
   `npm run build` to verify (§8).

**The trap (twice now):** don't merge by data-source or by the word "map".
Map 1 reads five services and renders controls; Map 2 reads one graph and
renders a picture; the canvas is scoped to one network; Chronoscope is offline
replay. Each earns its place. The only real cleanup was the _naming_, which is
done. Any deeper merge (e.g. adding a control affordance to the live diagram)
should be additive, not a deletion.

**Why the naming-only scope:** the deeper changes are ~2,400 lines of the most
animation- and drag-heavy portal code, portal-only, and the portal can't be
built in the AI sandbox (§8) — a blind merge here is high-regression. Anything
beyond naming wants a sighted
effort with `npm run build` in the loop, which is why it's a documented plan
rather than a rushed change.

## 13. Decision log pointers

ADRs: 0001 fleet (implemented), 0002 proxy tap (stages 1–2 built, TLS
deferred), 0003 report gates/provenance/sign-off (implemented), 0004 networks
unification (implemented, incl. alias removal), 0005 insight service (built,
advise-only), 0006 NATS (implemented), 0007 playground (backend + portal page
built; host-build verify owed), 0008 proxy-tap TLS (proposed — closes 0002's
deferred half), 0009 payment-provider integration (proposed — PSP packs +
provider simulators + generic MCP client adapter; Stripe/Adyen/PayPal
first). The finished build specs of the major
subsystems live in `docs/history/`, in the order they landed;
`docs/history/PROGRESS.md` and `docs/history/project-review.md` capture the
mid-project hardening pass. The
`.claude/skills/` pair is kept operationally accurate and is the fastest
answer to "which env flag / which endpoint".

---

_Written as a handover by the Claude collaboration that built this with
David. The single most transferable lesson from the whole project: the system
already knows most of what you're about to ask the user for — resolve it,
derive it, or advertise it before adding a knob._
