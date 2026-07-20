# ADR-0007: Playground — a unified ad-hoc execution surface

**Status:** Accepted — backend implemented (phases 1–3, 2026-07-16); portal
page written 2026-07-16 but UNVERIFIED — needs a host `npm run build` +
click-through (the sandbox cannot build the portal)
**Date:** 2026-07-15
**Deciders:** PayProbe maintainers (David + reviewers)

## Context

The idea under evaluation: a **Playground** — one place where an operator can
act on *any* live or configured resource in the platform interactively. Call a
single step, fire a message at a simulator directly or through a registered
connection, poke a listening network participant, run a crypto function, send
an HSM command — without first authoring a scenario, flow, or network.

The honest starting point is that **most of these capabilities already
exist** — as scattered, single-purpose surfaces:

| Capability | Where it lives today | Shape |
|---|---|---|
| Execute one node (http / code / crypto / init / action) | `POST /nodes/execute` (the editor's per-node "Execute" picker) | Client hand-builds node JSON + optional environment |
| Send one payShield command | `POST /hsm/command` (+ `hsm_command` / `run_hsm_example` MCP tools) | Friendly one-shot, hand-typed host/port |
| Probe a connection | `POST /connections/test` | Reachability + health only, no message send |
| Test-fire a participant flow | `POST /flows/debug-run` + DebugSession step-through | Flow-scoped |
| Build / analyze ISO 8583 bytes | Inspector (`iso8583_analyze` / `iso8583_build`) | Offline — never touches a socket |
| Chaos on a running simulator | chaos dial / storms | Fault injection, not message send |

Two observations follow:

1. **The assistant already has a playground; humans don't.** Every primitive
   above is an MCP tool. An agent can compose "build an 0100 with the 8583NT
   dialect, send it to the running VISA simulator, diff the response" today.
   A human operator has to hop between four pages and hand-type host/ports,
   or write a throwaway one-step scenario.
2. **There is one genuine architectural gap, not just a UX gap.** The
   existing ad-hoc paths take *materialized* adapter configs from the client
   (`/nodes/execute` accepts a full `environment`; `/hsm/command` takes raw
   host/port). To fire "through connection X in environment Y" the client
   would need the resolved connection values — including decrypted secrets —
   which invariant #8 (secrets never round-trip in plaintext) forbids
   handing out. Ad-hoc execution *by reference to a registered target* needs
   a server-side resolver.

### Forces

- **Overlap is debt.** ATLAS §10 records the pattern: when two features
  answer the same question, one of them gets cut (endpoints[] vs groups,
  topologies vs networks). A Playground that re-implements execution would
  be the next entry on that list. Whatever we build must *delegate* to the
  one execution owner (`WorkerEngine` / the adapter registry), not grow a
  second one.
- **Secrets at rest (invariant #8).** Target resolution materializes
  connection values from the override matrix and SecretBox-decrypts
  credentials. That must happen server-side; echoed request/response
  payloads shown to the user must be masked (PAN masking / redaction rules
  already exist in the proxy-tap `CaptureBuffer`).
- **Code execution is gated.** `/nodes/execute` refuses `code` nodes when
  auth is off and no sandbox is active (RCE/SSRF guard). A Playground
  inherits that gate untouched; it must not become a bypass.
- **Stop-ownership (invariant #5) reads across.** The Playground is a
  *client*. It never starts or stops anything — no listener binds, no
  simulator lifecycle, no port claims. Invariant #4 is therefore not in
  play except as a *lookup*: finding a network participant's planned port
  (or NATS subject, per ADR-0006) via the fleet endpoint registry.
- **One tool layer (invariant #3).** Any new assistant/MCP exposure is one
  handler in `payprobe_common/agent_toolkit.py` + one primitive per backend,
  and the portal catalog must be regenerated.
- **Provenance.** Go/No-Go sign-off (ADR-0003) certifies runs. Playground
  traffic hitting a running simulator mid-certification-run inflates its
  stats and traces. Cheap mitigation: tag playground-originated traffic in
  simulator stats/trace hops so reports can note contamination.

## Decision (proposed)

Build the Playground as a **thin composition surface over the existing
execution primitives**, adding exactly two backend pieces — a **target
resolver** and an **interaction log** — and one portal page. No new
execution engine, no new service.

1. **Target catalog.** `GET /playground/targets` (orchestrator) returns a
   merged, addressable view of everything that can be called right now:
   registered connections (× environments from the override matrix), running
   simulators, network participants with their planned ports / NATS subjects
   (fleet endpoint registry), participant groups, and the local function
   families (crypto ops, ISO codecs). Read-only aggregation of existing
   registries.
2. **Execute by reference.** `POST /playground/execute` takes
   `{target: {kind, id, environment?}, action, payload, message_format_id?}`,
   resolves the target server-side (connection + matrix + SecretBox decrypt →
   adapter config; simulator/participant → host/port or subject), then
   delegates to the existing paths: `WorkerEngine._execute_step` for adapter
   actions (exactly what `/nodes/execute` does for `action` nodes),
   `run_crypto` for functions, the HSM adapter for payShield commands.
   Responses echo request/response wire bytes + decoded fields + timing,
   **masked** with the CaptureBuffer redaction rules. Raw host/port targets
   stay allowed (parity with `/hsm/command`).
3. **Interaction log + replay + promote.** Per-user in-memory ring of recent
   playground interactions (the CaptureBuffer pattern — in-memory by design,
   not a file store): re-fire any entry, and **save-as-scenario** (the
   proxy-capture precedent) to promote an exploration into a durable
   scenario. This is the product payoff: the Playground becomes the on-ramp
   to authored tests, not a parallel world.
4. **Portal page** `/playground`: target picker (from the catalog), message
   composer that reuses the existing editors — Message Format/dialect picker
   + Inspector build for ISO 8583, JSON body for http/NATS, HSM action
   forms — a response pane (wire / decoded / timing), and the history rail.
5. **Assistant/MCP:** `playground_targets` + `playground_execute` as one
   agent_toolkit handler pair + one primitive per backend; regenerate the MCP
   catalog. Existing tools (`execute_node`, `hsm_command`, …) stay; the new
   pair adds target-by-reference, which agents currently lack too.
6. **Auth:** same fail-closed JWT gate as the run APIs. The code-node
   sandbox gate applies unchanged. Playground execute against a target
   resolved from a connection uses the connection's secrets server-side and
   never returns them.

Explicitly **not** in scope: starting/stopping anything, binding listeners,
editing configs (the Manage pages own that), a scripting/REPL language
(compose with the assistant instead — it already has the tools).

## Options considered

### Option A — Status quo (capabilities stay scattered)

| Dimension | Assessment |
|---|---|
| Complexity | None |
| Secrets exposure | No change (gap stays: ad-hoc-by-reference impossible without plaintext env) |
| Discoverability | Poor — four pages + hand-typed endpoints |
| Debt risk | None |

**Pros:** zero cost; every capability reachable by an agent already.
**Cons:** humans keep writing throwaway scenarios to ask one-message
questions; connection-referenced ad-hoc calls remain impossible without
violating #8; the "digital twin" demo story lacks its obvious interactive
moment.

### Option B — Thin composition layer (recommended)

| Dimension | Assessment |
|---|---|
| Complexity | Low-Med — 2 endpoints + 1 page; execution fully delegated |
| Secrets exposure | *Improves* posture: by-reference execution replaces plaintext-env round-trips |
| Discoverability | One page, everything addressable |
| Debt risk | Low — no second engine; catalog is a read-only merge |

**Pros:** closes the real gap (server-side target resolution); reuses
WorkerEngine, codecs, redaction, save-as-scenario; assistant parity via one
toolkit handler; natural on-ramp from exploration to authored tests.
**Cons:** one more portal surface to maintain (and portal work is
host-verify-only); the target catalog is another aggregation that must track
registry changes; stats-contamination question needs an answer (tagging).

### Option C — Full Playground service (own engine, sessions, persistence)

| Dimension | Assessment |
|---|---|
| Complexity | High — new package, new execution paths, session store |
| Secrets exposure | Same resolver needed anyway |
| Discoverability | Same as B |
| Debt risk | High — a second execution owner, guaranteed drift |

**Pros:** unconstrained UX (persistent notebooks, multi-step sessions).
**Cons:** re-implements what WorkerEngine owns; the exact overlap pattern
ATLAS §10 warns about; persistent ad-hoc sessions duplicate what scenarios
*are* — if an exploration is worth keeping, save-as-scenario is the answer.

## Trade-off analysis

The deciding question is not "is a Playground useful" (it is — it's the
interactive face of the digital-twin thesis) but "where does execution
live". Option C fails on the platform's own recorded history: every
abstraction that answered a question a neighboring abstraction already
answered got deleted. Option B keeps a single execution owner and spends its
budget on the two things that don't exist anywhere: target-by-reference
resolution (which *only* the server can do without breaking #8) and a
unified human surface. Against Option A, the marginal cost of B is small
because it is ~90% composition; the marginal value is that ad-hoc calls
through registered connections become possible *at all* — for humans and
agents alike.

## Consequences

- Easier: one-message questions ("what does the VISA sim answer to this
  0100?"), demoing the twin, onboarding (poke before you author), debugging
  a live network participant from the outside.
- Easier: explorations promote to scenarios (save-as-scenario), so the
  Playground feeds the authored-test culture instead of competing with it.
- Harder: the target catalog must be kept honest as registries evolve
  (new adapter families ride in via the adapter registry, but new *resource
  kinds* need a catalog entry).
- New risk to manage: playground traffic against running simulators during
  certification runs — now surfaced (2026-07-16): a per-simulator hit counter
  (`playground_hits` on `/simulators` rows + a Simulators-page badge) plus a
  **windowed** contamination count folded into the sign-off provenance stamp
  (`provenance.playground_traffic`: hits whose timestamp falls between the
  run's `started_at`/`completed_at`) and rendered as a "Playground traffic"
  line on the sign-off HTML. The stamp shape is stable (defaults to
  `{total:0,...}`) so the content hash is unaffected for clean runs.
- Promote UX (2026-07-16): save-as-scenario is now a dialog (name + optional
  project picker) with per-history-row checkboxes — tick a subset to promote
  just those interactions (the `seqs` param, already server-side), or leave
  all unticked to promote everything. Turns "poke around" into a curation step
  rather than an all-or-nothing dump.
- Samples (2026-07-16): every target carries a server-resolved `sample_family`
  and `GET /playground/targets` ships a `samples` block — one ready-made,
  composer-shaped example per wire family (iso8583/visa/header_echo/payshield/
  http/cybersource/nats/grpc) and one *runnable* parameter set per crypto
  operation (all 22, computed with `run_crypto` so each fires green as-is;
  `test_playground_samples.py` enforces coverage + green). Selecting a target
  seeds its first sample into the composer, so the textarea is never blank;
  chips switch between samples and show per-sample notes. The derived HTTP
  client config auto-carries a bearer credential so poking a running gateway
  sim passes its auth gate. Lives in `orchestrator/api/playground_samples.py`
  (pure data + resolver, unit-tested standalone like `playground.py`).
- Composer UX (2026-07-16): the action field is driven by the step catalog —
  a selected target's wire shape maps to a catalog family (mirroring the
  orchestrator's `_target_type_key`) and offers that family's real actions
  with payload-hint skeletons, with a "custom action" escape hatch. The
  dialect picker only shows for ISO-8583-family targets.
- Samples (2026-07-16): `GET /playground/targets` now carries `samples` —
  ready-made example interactions **per element family**. `samples.wire` is
  keyed by the target's `protocol` falling back to its `adapter`
  (`iso8583`, `visa`, `header_echo`, `payshield`, `http`, `cybersource`,
  `nats`, `grpc`); `samples.functions.crypto` carries one runnable parameter
  set for **every** `run_crypto` operation, with dependent values
  (pin_block, offset) precomputed by the platform's own crypto tools. Data
  lives in `orchestrator/api/playground_samples.py` (pure module, like
  `playground.py`); tests enforce full crypto coverage + that every function
  sample fires green and the ISO 0200 sample comes back approved from a live
  responder. The portal composer renders them as one-click chips (selecting
  a target seeds its first sample; selecting a function seeds its runnable
  parameters; manual edits drop the "applied" highlight). Samples are
  templates with classic test values — safe against the bundled simulators,
  masked like everything else on echo.
- Samples polish (2026-07-16, same day): (a) every target row in
  `/playground/targets` now carries a SERVER-resolved `sample_family`
  (protocol-over-adapter + shared-shape aliases live once, in
  `playground_samples.sample_family()`) — this also fixed participant groups
  getting no samples in the composer; (b) live-fire coverage extended beyond
  ISO: the payShield NC, VISA 0100 and CyberSource authorization samples are
  test-fired against their bundled simulators end-to-end through
  `/playground/execute`; (c) the client config derived for a running
  CyberSource sim carries a bearer credential so ad-hoc pokes pass its auth
  gate out of the box (real gateways use connection-configured auth); (d) a
  pristine applied sample rides as the history entry's `label` (history rail
  reads "Authorization (0100) → visa-sim" instead of `send_0100`); (e) the
  raw-target form gained a payShield option (`adapter: "payshield"` —
  host-command client, not a TCP dialect).
- To revisit: whether `/hsm/command` folds into `/playground/execute` once
  parity exists (deprecate, don't break — MCP tools reference it). Now more
  attractive since the composer already offers the HSM family's catalog
  actions.

## Action items (phased)

1. [ ] **Phase 0/UX — portal page:** WRITTEN 2026-07-16
   (`portal/src/app/playground/` — target rail grouped by kind with filter,
   composer with environment/dialect pickers + JSON payload, masked
   request/response panes, history rail with re-fire + save-as-scenario that
   deep-links into the editor; route + top-level nav entry). Checkbox stays
   OPEN until a host `npm run build` + click-through verifies it — the
   sandbox cannot build the portal.
2. [x] **Phase 1 — target resolver** (2026-07-16): `GET /playground/targets`
   + `POST /playground/execute` (by-reference: connection ⊕ override matrix,
   groups, running simulators, participants incl. fleet instances + port-less
   NATS subjects, raw host/port, crypto functions; server-side resolve,
   masked echo via the CaptureBuffer redaction rules extended to flat key
   names — `orchestrator/api/playground.py` + endpoints in `main.py`).
   Tests: resolve-through-matrix, masking, unknown-target 404, disabled
   connection 409. Code-gate inheritance is **by construction**: the
   Playground refuses `code` (400 → use `/nodes/execute`, where the sandbox
   gate lives) — there is no code path to bypass.
3. [x] **Phase 2 — history** (2026-07-16): per-user in-memory ring
   (`PLAYGROUND_HISTORY_MAX`, default 200; raw request kept in memory only,
   every echo masked) + `POST /playground/history/{seq}/refire` +
   `POST /playground/save-as-scenario` (tags `captured`/`playground`;
   connection-bound steps re-point through `step.config.connection` like
   authored scenarios). Provenance tag: playground fires that hit a RUNNING
   simulator are counted (`playground_hits` on `/simulators` rows) so
   certification-time stats contamination is visible. **Trace-hop tagging is
   deferred**: hops are recorded by listeners from wire traffic, which
   carries no origin marker — tagging them needs in-band correlation, not
   worth inventing a header for now.
4. [x] **Phase 3 — toolkit** (2026-07-16): `playground_targets` (read) +
   `playground_execute` (new **`execute` tier** — fires traffic, never
   journalled: invariant #2 governs *config* writes and there is nothing to
   restore; advisor mode never sees it, plan mode sees the schema but
   execution is tier-refused) in `payprobe_common/agent_toolkit.py`, one
   primitive per backend (StoresBackend via `RUN_API_URL`, RestBackend), MCP
   `Playground` group + portal catalog regenerated.
5. [x] ATLAS §11 roadmap + CLAUDE.md pointers updated (2026-07-16); the
   portal page (item 1) still owes a host `npm run build` + click-through.
