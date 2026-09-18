# Agentic engine — evaluation

**Status:** Evaluation, not a decision. Pre-ADR.
**Date:** 2026-08-21
**Scope:** the five agent flavors proposed — autonomous test agent, agentic
participants, exploratory/fuzzing agent, ops/triage agent, autonomous observer.

Written in the ATLAS voice: what it buys, what it costs, what to cut, and the
gate that would tell us we were wrong. Companion to
[`ATLAS.md`](ATLAS.md) §11 and [ADR-0005](adr/0005-ml-insight-service.md).

---

## 1. Start here: PayProbe already has an agentic engine

Before proposing one, be honest about what is built:

| Piece | Where | What it already does |
|---|---|---|
| Tool-calling loop | `assistant_service/loop.py` | Provider-neutral, `MAX_ITERATIONS=8`, streams `tool` events, three modes |
| Tool layer | `payprobe_common/agent_toolkit.py` | 40 tools — 27 read / 11 write / 2 execute — one definition, two backends |
| Reversibility | `ChangeJournal` + `restore_one` | Every write journalled with `before`; restore is a pure function |
| Modes | `main.py` | `full` / `advisor` (read-only) / `plan` (proposes exact calls, human applies each) |
| Guardrails | tool layer, not prompt | Builtins undeletable, referenced connections undroppable (invariant #6) |
| MCP surface | `packages/mcp-server` | Both services proxied, HTTP + stdio |
| Advisory ML | `packages/insight-service` | Categorize / explain / predict, advise-only by design |

So the question is **not** "should PayProbe have agents." It has them. The
question all five flavors actually ask is narrower and sharper:

> **What is allowed to happen when nobody is watching?**

Every loop today is bounded by a chat turn and terminates in a human reading a
reply. The proposal is to remove that terminator. Everything below is a
consequence of that one change.

---

## 2. The reason this is more viable here than almost anywhere else: the oracle

Most agentic projects fail for one reason — the model grades its own homework.
There is no independent, machine-readable answer to "did that work?", so the
loop converges on plausible-looking nonsense.

PayProbe has **five independent verdict functions already built, already pure,
already tested**:

- `iso8583.iso_validate` — spec conformance against a bound dialect
  (`TcpResponder._validate`, `validate_mode`).
- `report_service/generators.py::certify(run_detail, pack)` — compliance % against
  a certification pack.
- `report_service/gates.py` — an explicit `GatePolicy` → Go/No-Go verdict, with
  no-silent-skips (ADR-0003).
- `report_service/diagnose.py::classify_error` — 10-category failure taxonomy
  with fix hints.
- `orchestrator/api/resilience.py::score_resilience` — graded resilience score
  over chaos stages.

Plus run history, `RunStore.flakiness()`, and the insight-service predictions.

`report_service` being a **pure library — functions over dicts, no I/O, no
state** (ADR-0003) is the quiet enabler here. An agent loop can call the same
verdict function the report calls, in-process, deterministically, thousands of
times, with no fixtures. That is an unusually strong foundation and it is the
single best argument for proceeding at all.

**Corollary:** any proposed agent that cannot be scored by one of those five
functions is a weak candidate. Use this as the filter.

---

## 3. The reason to be careful: the product *is* the evidence

ADR-0003 and ADR-0005 independently drew the same line, and it is the most
load-bearing line in the codebase:

> Sign-off provenance stays purely factual. Advisory output never gates a run,
> never modifies a scenario, never alters a verdict.

PayProbe's pitch is not "tests payment systems." It is "hands you an artifact
you can defend at a gate six months later." Three properties make that artifact
worth anything — **reproducibility, determinism, and provenance** — and
autonomy attacks all three by default. This is not a reason to stop; it is the
constraint that decides which flavors survive.

---

## 4. Flavor-by-flavor

### 4.1 Autonomous test agent — *the strongest candidate*

**Usage.** "Certify the acquirer network against the VISA pack." The agent
starts the network, drives load, injects chaos, reads the resilience score,
notices a coverage gap ("no timeout case against issuer 3"), authors and runs a
scenario, and hands back a certificate. Operator walks away.

**What it actually buys.** Not "AI" — the honest framing is
**`scripts/showcase.py --certify`, generalized to networks it has never seen.**
That script already does build → stress → verdict end to end. It is hard-coded.
The agent is the version that works on the customer's topology. Stated that
way, the value is obvious and the scope is bounded.

**What it costs.**

- **A new execution home.** The assistant is *request-shaped*: SSE per turn,
  session TTL, 8 iterations. A certification is minutes to hours — *run-shaped*:
  durable, resumable, cancellable, budgeted. The orchestrator already owns
  everything long-lived (`TOPOLOGY_RUNS`, `schedule_store`'s background loop,
  load coordination). **The agent run belongs in the orchestrator, not the
  assistant.** The assistant stays the conversational surface; the toolkit stays
  shared (invariant #3 intact).
- **A capability the assistant is deliberately denied.** The system prompt says
  it outright: *"Apart from load tests you never start or stop runtime pieces
  (listeners, simulators, networks, scenario runs) — that stays with the
  operator."* An autonomous certifier needs exactly that. Do **not** resolve
  this by widening the assistant's tier — that is how invariant #6 dies. Resolve
  it with a **separate principal** (§5.1).
- **Cost per run.** Hundreds of model calls over a 50+ tool schema. Budget in
  tokens *and* wall-clock, enforced in `dispatch`, not in the prompt.

**Verdict: build it, second.** Aligned with the thesis, oracle-backed, and it
has a working hard-coded proof to be measured against.

---

### 4.2 Agentic participants — *cut the online form*

**Usage as proposed.** A fourth node kind: an LLM-driven issuer that reasons
about traffic and adapts; a fraud actor; a switch that degrades intelligently.

**Technically easy, which is the trap.** The seam exists —
`TcpResponder._resolve_async` is explicitly documented as the async override
point, and `FlowResponder` already uses it. Wiring a model call in is an
afternoon.

**Three reasons not to.**

1. **Latency.** A model call is 300 ms–3 s. Load runs target thousands of TPS
   across a fleet. That is three to four orders of magnitude off. The feature
   would be structurally excluded from the workload PayProbe exists to run.
2. **Determinism — the fatal one.** A nondeterministic participant makes runs
   un-comparable. "Vs the last **approved** run" (ADR-0003's baseline
   comparison) becomes meaningless, and a resilience certificate that cannot be
   reproduced is not evidence. This breaks the product, not just the feature.
3. **The precedent is already recorded.** ADR-0009 weighed MCP as payment
   transport and **explicitly rejected it**, keeping it as control plane. Same
   question, same shape of answer.

**What to keep instead — the offline form.** An agent that reads captured
traffic and **authors the responder rules / participant flow**, once, at design
time. The deterministic responder then serves them at full speed. This is
strictly better on every axis: it is reviewable, it is a config write so
invariant #2's journalling already covers it, it costs one model call instead of
one per transaction, and it produces an artifact a human can read. It also
dovetails with the existing roadmap item *record-and-replay* and the proxy tap's
save-as-scenario.

**Narrow exception, if wanted.** A low-rate adaptive actor (fraud sequence
generation at ~1 TPS in a functional test) is defensible. It must be
*structurally* incapable of appearing in a load run or a certified run — the
guardrail shape already exists: `external: true` connections are refused by load
runs by design (ADR-0009). Reuse that pattern; do not invent a new one.

**Verdict: reject as stated (§10 pattern — it answers the same question as
simulator rules, worse). Keep the design-time authoring version.**

---

### 4.3 Exploratory / fuzzing agent — *valuable, but the LLM is the small half*

**Usage.** "Here is the VISA dialect and this switch — go find what breaks it."
Mutate fields, probe edges, promote findings to authored scenarios.

**The oracle question is answered here**, which is why this is viable at all:
`iso_validate` says what is spec-legal, the certification pack says what should
happen, `classify_error` names what came back. "Interesting" is
machine-decidable. No LLM judgment required to know a finding is real.

**But be honest about where the LLM earns its keep.** For field-level mutation,
it does not — grammar-driven / property-based fuzzing over a dialect you already
have machine-readable is cheaper, faster, and more thorough than a model
guessing bytes. The model earns its keep in exactly two places:

- **Triage** — collapsing 10,000 failures into 12 distinct findings with a
  readable hypothesis each. (Which is really §4.4.)
- **Semantic sequences** — multi-message flows the grammar does not express:
  auth → partial reversal → late advice, in an order the spec permits and nobody
  tests. Domain knowledge beats mutation here.

**So split it.** Build the deterministic dialect fuzzer first — no LLM, no new
service, reuses the format catalog and the validator. Then let the agent be the
layer that reads the results and proposes sequences. The promotion path
(save-as-scenario) already exists in the playground and the proxy tap.

ADR-0005's cold-start rule applies verbatim: **the deterministic layer is the
floor; the learned layer is allowed on top only when it beats the floor.**

**Verdict: build it, third — deterministic half first.**

---

### 4.4 / 4.5 Ops-triage agent and the autonomous observer — *best value per unit of effort*

These are one flavor with a dial: **observe → alert → propose → act.** The
proposed observer ("analyse the process, and if there are issues, solve them or
throw an alert") is the triage agent turned two clicks further along that dial.

**The most useful finding in this memo: most of the observe/alert half is
already designed, and part of it is built.**

- `packages/insight-service` — categorize / explain / predict, live.
- **ADR-0005 Phase 2 already specifies exactly this**: LLM-written explanations
  grounded on Phase-1 features, via the standalone assistant, with *no new LLM
  egress path*. It is an open, accepted, unbuilt phase.
- `assistant_service/explain.py` — advisory endpoint, `advisory_only: True`
  stamped on every response.
- `suggest.py` — advisory suggestions, human-approved.
- `orchestrator/api/schedule_store.py` — durable cadence registry with a
  background loop. The "runs unattended on a schedule" substrate exists.
- Network trace capture, `/diagnostics` layers, Grafana metrics.

**Roughly 60% of the requested observer is an open ADR phase plus a scheduler.**
That is a much cheaper first step than a new engine, and it closes something
already on the books.

**What is genuinely missing: a delivery channel.** PayProbe has no notification
surface — no outbound webhook for alerts, no email, no chat integration. This is
the boring half, and it is the half that decides whether anyone ever sees the
alert. Budget for it honestly.

**Now the dangerous half — "solve".** An observer that fixes things *changes
what the evidence means*. If a run goes green because an agent quietly widened
an assertion, retried a flaky step, or bumped a timeout, the Go/No-Go artifact
is lying. Draw the line by **what the action changes**, not by how risky it
feels:

| Allowed — changes *availability* | Forbidden — changes *evidence* |
|---|---|
| Restart a dead `flow_host`; re-place a listener after worker death | Edit a scenario, step, or assertion |
| Resume trace capture (off by default — a real recurring support issue) | Change a gate policy, pack, or threshold |
| Re-run a run that failed `unreachable` before any transaction landed | Re-run a failed run and report the passing one |
| Raise an alert, open a finding, annotate a run | Anything touching a run that will be certified |

The left column has a precedent: **the fleet already does autonomous
re-placement with a cooldown.** That is unattended self-repair, shipped, and
nobody considers it controversial — because it restores capacity without
touching outcomes.

Enforce the line structurally, not by prompt: **the observer principal is denied
the `write` tier entirely** and granted a whitelist of runtime-repair
operations. Then invariant #2 is not even the guard — the agent cannot reach the
tools.

**Verdict: build it, first — observe/alert only. "Solve" scoped to availability
repair, shipped separately and later.**

---

## 5. Cross-cutting costs (the part that is not per-flavor)

### 5.1 Autonomy needs a *principal*, not a mode

Today the assistant runs **as the user**, on the caller's JWT. An unattended
agent has no user in the loop, and every autonomy question then degenerates into
"widen the assistant's tier" — which is precisely how invariant #6 dies.

What is needed instead: an **agent identity** with its own grants (which tiers,
which resources), its own budget, and its own audit trail. `packages/auth-service`
already does JWT + roles, and there is already a service-JWT `svc` claim pattern
(`/assist/config/material`, `/test-data/keys/{name}/material`). This is an
extension of an existing pattern, not a new subsystem.

This is **Stage 0**: it is a prerequisite for flavors 1, 3, 4 and 5, and it is
useful on its own.

### 5.2 Budgets must be runtime-enforced

`MAX_ITERATIONS=8` is fine for a chat turn. Hours-long autonomy needs token
budget, wall-clock budget, and blast-radius budget, checked in `dispatch` —
same principle as invariant #6, applied to resource consumption.

### 5.3 Provenance for agent-authored artifacts

Anything an agent authored that feeds a report must say so: `authored_by`,
model, prompt/config hash, journal id. ADR-0003's provenance block is the place;
this is small, cheap, and a hard prerequisite for §4.1 and §4.3. Without it,
agent-authored evidence silently launders its own origin.

### 5.4 The offline-deployment risk

Payment testing lives inside banks. The LLM today is an **optional, configurable
egress boundary** — Settings → AI assistant, `ASSIST_LLM_*`, and everything
still works without it. If agentic features become load-bearing rather than
assistive, PayProbe stops working in exactly the environments most likely to buy
it.

**Generalize the ADR-0005 rule into a platform rule: every agentic feature must
degrade to a deterministic floor.** Fuzzer without the LLM triage. Certification
without the planner (i.e. the scripted showcase path). Observer without the
explanation (heuristic `diagnose()` findings). If a feature has no floor, it is
the wrong feature.

### 5.5 Cost and determinism are *product* risks

Not just infra risks. Nondeterminism anywhere near the evidence path is a
product bug, and a per-transaction model call is a per-transaction invoice.

---

## 6. Recommended sequence

| Stage | What | Why this order |
|---|---|---|
| **0** | Agent principal + runtime budgets + provenance stamp | Prerequisite for 1–3; useful alone; no LLM involved |
| **1** | **Observer** — scheduled ADR-0005 Phase 2 + alert delivery channel. Advise-only. | Cheapest, closes an open ADR phase, ~60% designed already |
| **2** | **Autonomous certification agent** — run-shaped, in the orchestrator, generalizing `showcase.py --certify` | The thesis, unattended. Has a hard-coded proof to be measured against |
| **3** | **Deterministic dialect fuzzer** (no LLM) + agent triage layer on top | Floor first, learned layer only if it beats the floor |
| **Cut** | LLM in the participant data path | Latency, determinism, and ADR-0009's precedent. Keep design-time rule authoring |
| **Later / maybe never** | Autonomous "solve" beyond availability repair | Only after the observer has a track record worth trusting |

---

## 7. The honest gate

In the spirit of ADR-0005's own falsifier, name it before building:

> **If the autonomous certification agent cannot reproduce a `--certify` run's
> verdict, unattended, in 9 of 10 attempts on the showcase network, the autonomy
> layer has not earned its keep and the scripted path wins.**
>
> That outcome is a success, not a failure — it is the cheapest possible way to
> learn it.

Two more, one per remaining stage:

- **Observer:** if operators dismiss more than ~half of its alerts without
  action, it is noise, and the heuristic `diagnose()` headline was already
  enough.
- **Fuzzer:** if LLM-proposed message sequences do not find defects the
  deterministic mutator misses, drop the LLM half and keep the fuzzer.

---

## 8. Open questions for the ADR

1. Does the operator-only boundary on start/stop survive autonomy, or does an
   agent principal get an explicit, budgeted grant to cross it?
2. Where does an agent *run* live — a new `AGENT_RUNS` registry alongside
   `TOPOLOGY_RUNS`, or a special case of an existing run?
3. Does a `plan` tier become a first-class artifact (durable, reviewable,
   applied later) rather than a chat mode?
4. What is the alert delivery channel — outbound webhook (smallest, most
   composable) or something richer?
5. Does agent-authored content need a separate review state before it can appear
   in a certified run, or is the provenance stamp sufficient?

---

## 9. Bottom line

The idea is sound, and PayProbe is better positioned for it than most platforms,
for one specific reason: **the oracles are already built and pure.** But four of
the five proposed flavors are variations on "remove the human turn", and the
existing architecture has answered that question three separate times already —
ADR-0003 (provenance stays factual), ADR-0005 (advise-only is non-negotiable),
ADR-0009 (control plane yes, data plane no).

The pattern those three share is the actual recommendation:

> **Let the agent decide, propose, and explain. Never let it be the thing that
> executes inside the evidence path, or the thing that decides whether the
> evidence is good.**

Start with the observer, because it is mostly designed. Prove autonomy on the
certification agent, because there is a scripted baseline to beat. Cut the
LLM-in-the-loop participant, because it trades the product's core property for a
demo.
