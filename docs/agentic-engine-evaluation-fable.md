# Agentic engine — re-evaluation (Fable) and diff vs the Opus memo

**Status:** Second opinion. Companion to
[`agentic-engine-evaluation.md`](agentic-engine-evaluation.md) (Opus,
2026-08-21). Same question, same codebase, different model.
**Date:** 2026-08-21

**Method note, honestly:** this pass had the Opus memo in view — it is a
re-derivation that was free to disagree, not a blind replication. Where the two
agree, that is worth something (the conclusion is probably overdetermined by
the codebase's own ADRs); where they differ, the difference is the interesting
part and is argued below, not just tallied.

---

## 1. Diff at a glance

| Topic | Opus | Fable | Verdict |
|---|---|---|---|
| Is an agentic engine viable here | Yes — the five pure oracles make it unusually viable | Same | **Agree** |
| Observer / ops-triage | Build **first**; ~60% is ADR-0005 Phase 2 + `schedule_store`; advise-only | Same | **Agree** |
| "Solve" autonomy | Availability repair only, `write` tier structurally denied | Same | **Agree** |
| LLM inside participants (online) | Cut; keep design-time rule authoring | Same, **harder**: also cut the "low-rate adaptive actor" exception Opus kept | **Differ (scope)** |
| Autonomous certification agent | Agent-in-the-loop, run-shaped, lives in orchestrator, budgeted nondeterminism | **Plan-then-execute**: agent authors a durable, reviewable *certification plan*; a deterministic pipeline executes it; agent re-enters only between runs | **Differ (architecture — the main one)** |
| Fuzzer | Build third; deterministic floor first, LLM triage on top | Same split, but the deterministic half is **unblocked now** — needs no LLM, no principal, no Stage 0 | **Differ (ordering)** |
| Agent principal (identity, grants, budgets) | Stage 0 prerequisite | Same, plus the outbound **alert webhook moves into Stage 0** (it is tiny and two stages need it) | **Agree + addition** |
| Honest gate for certification | Reproduce the showcase verdict unattended 9/10 | Too weak for a binary verdict; replaced (§4) | **Differ (gate)** |

Net: the big calls survive a model swap — observer first, advise-only line,
online participants cut, principal prerequisite. The disagreement concentrates
on **one axis: how much LLM nondeterminism is allowed *inside* the run loop.**
Opus said "some, budgeted." Fable says "none — move it all to the seams."

---

## 2. The main difference: plan-then-execute, not agent-in-the-loop

Opus §4.1 puts the agent in the driver's seat of a live certification run —
run-shaped, resumable, budgeted, hundreds of model calls steering a
minutes-to-hours process. Fable's position: **that design imports the risk the
rest of the memo spends its time fencing off.** There is a shape that gets the
same outcome with none of it:

1. **The agent authors a certification plan** — a durable artifact: network id,
   pack, load profile, chaos stages, coverage additions ("no timeout case
   against issuer 3 → add scenario X"), gate policy. Reviewable, journalled,
   diffable.
2. **A deterministic pipeline executes it.** The orchestrator already executes
   exactly this sequence with zero model calls — `scripts/showcase.py
   --certify` is that pipeline, hard-coded. The execution layer is
   generalization of existing code, not agent infrastructure.
3. **The agent re-enters between runs**, reading the report/diagnose output and
   proposing a plan *amendment* — which is itself a journalled artifact.

Why this is better than budgeted autonomy, point by point:

- **Evidence purity is structural, not budgeted.** Under agent-in-the-loop, the
  provenance stamp must attest "the agent behaved" — a claim about a
  nondeterministic process. Under plan-then-execute, the certified run contains
  *zero* model calls; the plan hash goes in the ADR-0003 provenance block and
  the run is exactly as reproducible as any scripted run. Re-certification =
  re-execute the same plan. The reproducibility gate stops being a risk and
  becomes a property.
- **The precedent already exists and is half-built.** Plan mode produces exact
  tool calls, and `POST /agent/apply` (`assistant_service/main.py:270`)
  executes them through the same journalled dispatch. What is missing is only
  that plans are chat-ephemeral — `session.py` has no plan persistence.
  "Promote plan to a durable artifact" was Opus open question #3; Fable's
  answer is that it is not an open question, **it is the design.**
- **The cost problem dissolves.** Opus §5.2's hours-long token budgets exist
  because the agent sits in the loop. A planner making ~10 calls to write a
  plan, and ~10 more to review a report, needs a per-invocation cap — the
  existing `MAX_ITERATIONS` shape — not a new budget subsystem.
- **The deterministic floor comes for free.** Opus §5.4 requires every agentic
  feature to degrade to a floor. Under plan-then-execute the floor *is* the
  execution layer — a bank with no LLM egress writes plans by hand and runs the
  same pipeline. Under agent-in-the-loop the floor is a second implementation.

What is honestly lost: mid-run adaptation (abort early, redirect chaos while
load is running). That is real but small — certification runs are minutes to
hours, and between-run adaptation captures most of the value. If mid-run
reaction ever matters, it is the observer watching the run (Stage 1 machinery)
raising a finding — not the planner holding the wheel.

---

## 3. The smaller differences

**3.1 Cut the adaptive-actor exception entirely.** Opus rejected online LLM
participants but kept a carve-out: a ~1 TPS adaptive actor in functional tests,
fenced by an ADR-0009-style guardrail. Fable cuts the carve-out too. The fence
is not free — it is a taint bit ("this run touched a nondeterministic actor")
that every future run-consuming feature must remember to check, forever, to
protect against a feature with no named user. The same value — varied,
adversarial issuer behaviour — is available deterministically: agent-authored
responder rules plus data tables, seeded. When someone asks for the online
version with a concrete use case, write the ADR then. Rejecting it fully is the
codebase's own §10 pattern: *when two features answer the same question, one of
them is debt.*

**3.2 The deterministic fuzzer is unblocked today.** Opus sequenced it third,
behind the principal and the certification agent. But its deterministic half
needs none of that: no LLM, no principal, no budgets — it is
`list_formats`/dialect tables + `iso_validate` + a mutation engine + the
existing save-as-scenario promotion path. It is pure engineering value whether
or not any agent decision is ever taken, and it can proceed in parallel with
Stage 0. Only the LLM triage/sequence layer waits.

**3.3 Alert webhook into Stage 0.** The observer (Stage 1) and the
certification pipeline (Stage 2) both need "tell a human something happened,"
and the platform has no outbound notification surface at all (verified: no
webhook/notify/SMTP sender anywhere in the services — the only emitter is the
PSP simulators' `WebhookEmitter`, which is simulation traffic, not ops). One
outbound webhook with signed payloads — the emitter pattern already written —
is small enough to belong in Stage 0 next to the principal.

---

## 4. Revised gates

The Opus gate — *reproduce the showcase `--certify` verdict unattended, 9/10* —
is the wrong bar twice over: a binary verdict on a healthy fixed network
reproduces trivially, and under plan-then-execute reproduction is a property of
the pipeline, not an achievement of the agent. Replace it with gates that test
what the LLM actually contributes:

- **Planner:** on the showcase network, the agent-authored plan must include at
  least one *justified* coverage addition the scripted showcase lacks, and the
  executed plan's compliance % must land within tolerance of the scripted
  baseline. If its plans are just the showcase script restated, the planner has
  not earned its keep — script wins. (That outcome is a success: it is the
  cheapest way to learn it.)
- **Observer:** unchanged from Opus — if operators dismiss more than ~half of
  its alerts without action, it is noise and the heuristic `diagnose()`
  headline was already enough.
- **Fuzzer triage:** unchanged — if LLM-proposed sequences find nothing the
  deterministic mutator misses, keep the fuzzer, drop the LLM half.

---

## 5. Revised sequence

| Stage | What | Changed vs Opus |
|---|---|---|
| **0** | Agent principal + provenance stamp + **outbound alert webhook** | webhook added; hours-scale budget subsystem dropped (per-invocation caps suffice under §2) |
| **0∥** | **Deterministic dialect fuzzer** — no LLM, starts now in parallel | promoted from stage 3 |
| **1** | Observer — scheduled ADR-0005 Phase 2, advise-only, delivering via the Stage-0 webhook | unchanged |
| **2** | **Certification planner** — durable plan artifacts + generalized `--certify` pipeline + between-run review loop | re-architected from agent-in-the-loop to plan-then-execute |
| **3** | LLM triage + semantic-sequence layer on the fuzzer | was the second half of Opus stage 3 |
| **Cut** | Online LLM participants — **including** the low-rate exception | exception removed |

---

## 6. What the agreement means

Two models, told to disagree where warranted, converged on every call that the
codebase's own ADRs had already answered: advise-only survives contact with
autonomy (ADR-0005), provenance stays factual (ADR-0003), control plane yes /
data plane no (ADR-0009). Those verdicts are not model opinions — they are the
architecture reasserting itself, and an ADR-0010 can treat them as settled.

The one place the models genuinely split — *may the agent hold the wheel during
a run, on a budget, or must it hand a plan to a deterministic executor?* — is
therefore exactly the question ADR-0010 exists to decide. Fable's answer:
plan-then-execute, because it makes evidence purity a structural property
instead of a monitored budget, and because half of it (`/agent/apply`, the
journal, the showcase pipeline) is already in the repo. The strongest argument
for the Opus shape is mid-run adaptation; if that ever proves necessary, it
belongs to the observer, not the planner.
