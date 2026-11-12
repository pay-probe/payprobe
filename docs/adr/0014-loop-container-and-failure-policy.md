# ADR-0014: The loop node as a container, with a failure policy for the steps inside it

**Status:** Accepted (2026-11-12, David: error ports + handled-failures switch);
phases 1 to 3 built 2026-11-12 on `main` (uncommitted at the time of writing);
host click-through owed (action item 11).
**Date:** 2026-11-12
**Deciders:** PayProbe maintainers (David)
**Touches:** invariant #1 (edge semantics per graph level) in `CLAUDE.md`

## Context

A scenario author wants two things from a loop: to **see** which steps repeat
(a box on the canvas that holds them), and to **decide what happens when one
of those steps fails** (try the next item, leave the loop, keep going, or stop
the run). Neither exists today. Facts below verified against `main` on
2026-11-12.

**1. A loop body is whatever hangs off the `loop` port.** `node_output_ports`
in `packages/scenario-service/models/scenario.py` gives a loop the ports
`loop` and `done`. The runner (`_walk` in `packages/worker/engine/runner.py`,
the `kind == "loop"` branch) takes the node wired to `loop` and, per
iteration, calls a nested `_walk` from it until the chain dead-ends, with
`context["loop"] = {"index", "item"}` set around the call. Then it records the
loop as passed and continues at `done`. Nothing stores which steps belong to
the body; the canvas draws body steps exactly like any other step.

**2. A body that wires back into the main graph is legal and wrong.** The
validator does not check where body edges go. If the last body step is wired
to a step after the loop, every iteration walks on into the rest of the
scenario, and `done` then runs it once more. No repository scenario, pack or
starter flow does this (no `"loop"` node in any JSON or YAML under
`packages/` on 2026-11-12), but a saved user scenario may.

**3. A failure inside a loop has only the scenario-wide answer.** Every
executing kind in `_walk` (`action`, `code`, `http`, `init`, `crypto`, `call`,
`relay`) repeats the same block: append the outcome, set
`result.status = FAILED`, and `raise _StopScenario()` when the scenario's
`stop_on_failure` (default `true`) is on. So a failed step in iteration 3 of
100 either ends the whole run, or (with `stop_on_failure: false`) the body
simply carries on to its next step. There is no "skip this item", no "leave
the loop and continue after it", and nothing per step.

**4. Two traps in the current loop code.** `context["loop"]` is restored
after the body walk, not in a `finally`, so any exception that leaves the
body leaves a stale `loop` for the caller. And `_graph_entry` picks the
first step with no incoming edge as the scenario start; a step dropped into
a container but not yet wired would be picked as the start.

**5. One model, three canvases, two runners' worth of callers.**
`ParticipantFlowDraft.nodes` and the network flow's `nodes` are both
`list[Step]`, so a field added to `Step` is persisted by flows too. The same
`ScenarioRunner` walks participant flows (responder) and load-run shards.
The portal's `ConstructorComponent` edits both scenarios and participant
flows (`/flow-editor/:id`). Save is refused with 422 on any validation
error (`create_scenario` / `update_scenario` in `scenario-service/api/main.py`).

## Decision

David chose on 2026-11-12: error output ports (failure handling Option B,
together with the policy of Option A) and a switch that lets handled failures
pass the run (verdict Option B), with the remark that **in some processes a
failing flow is the standard process** (a timed-out authorization followed by
a reversal is the textbook case). The decisions below follow from that.

1. **Membership is a step field.** `Step.parent: str | None = None` holds the
   id of the loop that contains the step. A loop may contain loops (nesting by
   `parent` chain). The body's entry is still the step wired to the loop's
   `loop` port; the `loop` edge does not change meaning.

2. **The runner keeps one meaning of "inside": the walk.** A step is inside a
   loop when the runner reached it through that loop's nested `_walk`, as
   today. `parent` is for the canvas and the validator; the validator makes the
   two agree (decision 7), so old loops without `parent` behave exactly as now.

3. **Every executing node gets an `error` output port.** `action`, `code`,
   `http`, `init`, `crypto`, `call` and `relay` expose `["out", "error"]`; the
   loop exposes `["loop", "done", "error"]`. When a step fails (status
   `failed` or `error`) and its `error` port is wired, the walk follows that
   wire. An unwired `error` port changes nothing. The loop's `error` port is
   taken when the loop ends by `break` after a failure; unwired, `break`
   continues at `done`.

4. **A failure policy per step, with a default per loop**, applied when the
   failed step's `error` port is not wired.
   `Step.on_failure: Literal["stop", "next", "break", "ignore"] | None = None`
   and `loop.config.on_failure` (same values, default unset).

   | Policy | Inside a loop | Outside a loop |
   |---|---|---|
   | `stop` | end the scenario now | end the scenario now |
   | `next` | skip the rest of this iteration, start the next | validator error |
   | `break` | leave the loop (its `error` port, else `done`) | validator error |
   | `ignore` | carry on to the step's `out` | carry on to the step's `out` |

   Resolution order: the step's `on_failure`, else the innermost enclosing
   loop's `config.on_failure`, else the scenario's `stop_on_failure`
   (`true` → `stop`, `false` → `ignore`). With nothing set, behaviour is
   byte-for-byte today's.

5. **A failed step is readable downstream.** Every executing node writes
   `context[step_id]` with `request`, `response`, `status` and `error`,
   including when the adapter raised or a reference did not resolve (today
   those write nothing). A recovery path reads `${auth.status}` /
   `${auth.error}`; a reversal reads `${auth.request.values.11}`.

6. **Handled failures and the verdict.** A failure is *handled* when the walk
   followed its `error` port or an explicitly set policy (`next`, `break`,
   `ignore` on the step or its loop). The legacy route
   (`stop_on_failure: false`) is **not** handled, so old scenarios keep their
   verdict. Whether a handled failure fails the run is a switch,
   `handled_failures: "pass" | "fail"`, on the loop's `config` and on the
   scenario; the innermost one wins; default `"pass"`, because the author drew
   the failure path on purpose.
   - Counts as pass: the step's status becomes **`handled`** (a new step
     status), the scenario keeps `passed`, and the scenario gets a note
     `handled:<step_id>:<route>` (`ScenarioResult.notes`, which never changes
     the verdict). Report gates, JUnit and trends treat `handled` as not
     failed; the run monitor shows it amber with the original error.
   - Counts as fail: the step stays `failed` / `error` and the scenario is
     `failed`, but the walk still takes the chosen route.
   - `stop` is never handled.
   - There is no new *scenario* status; `passed_with_warnings` was rejected
     because every reader of the scenario status (gates, trends, load
     success rate, provenance) would need it.

7. **Validator rules** (`validate_scenario`, `models/scenario.py`):
   - `error` is a valid `source_port` for the executing kinds and the loop.
   - `parent` must name an existing `loop` step and the parent chain must not
     cycle (error).
   - For a loop whose body has `parent` set, every edge between a contained
     step and an uncontained one is an error, except the loop's own `loop`
     edge into the body entry. The body entry must be contained. An `error`
     wire from inside a box must stay inside it (leave with `break`).
   - For a loop **without** contained steps (an old body), a body edge that
     reaches a step outside the `loop`-port chain is a **warning**, "loop body
     wires back into the main flow; every iteration will run it", so no saved
     scenario stops saving.
   - `next` / `break` on a step that is not inside any loop is an error.
   - `handled_failures` must be `pass` or `fail`.
   - A contained step is never the scenario entry: `_graph_entry` skips steps
     with `parent` set (and the validator warns about an unwired contained
     step: "not reachable from the loop's entry").

8. **Runner mechanics** (`packages/worker/engine/runner.py`):
   - The per-kind failed-outcome block is replaced by one helper that writes
     the context entry, chooses the route (error port, policy), decides
     handled / not handled, sets the status and the verdict, and returns the
     next node or raises; no kind branch handles failure itself.
   - `next` and `break` are internal signals caught only by the loop branch
     that owns them; `context["loop"]` is restored in a `finally`; neither
     signal can leave `_run_graph` or cross a `call` into a sub-scenario.
   - Every failed outcome records the route in its trace
     (`{"level": "warn", "msg": "on_failure=next: skipping rest of iteration 3"}`).
   - The loop node records how it ended: `response = {"iterations": n,
     "ended": "done" | "break", "failed_iterations": k}`.
   - Edge-less (linear) scenarios have no ports: only `on_failure` `stop` /
     `ignore` and `handled_failures` apply there.

9. **Canvas** (portal constructor, scenarios and participant flows):
   - A loop with contained steps is drawn as a box behind its children; the
     box is computed from the children's absolute `layout` positions plus
     padding, so no new stored geometry.
   - Dropping a step from the palette inside a box, or dragging a step into
     or out of a box, sets or clears its `parent`. Dragging the loop moves
     its children with it.
   - Every executing node shows a small red `error` port under `out`; wires
     from it are drawn red and dashed.
   - Opening a scenario whose loop has an old body shows it as before; a
     "Make container" action on the loop adopts the `loop`-port chain (sets
     `parent`) and marks the scenario changed. Opening never marks it changed.
   - The step side panel gets an **On failure** picker (Inherit / Stop
     scenario / Next iteration / Leave loop / Ignore) and says when the
     wired `error` port overrides it; the loop panel gets the default policy
     and the **Handled failures** switch (Count as passed / Fail the run).
   - `parent` survives rename (the rename rewrites children's `parent`) and
     duplicate. Deleting a loop moves its children one level up (to the
     deleted loop's own `parent`, or out of every box).

## Options considered

### Membership

#### Option A: `parent` field on the step (chosen)
- Pro: steps stay in one flat list; ids, `${step.response}` references,
  reports, the debugger and the agent tools keep working unchanged.
- Pro: additive and optional; old scenarios and old readers are unaffected.
- Con: the validator must keep `parent` and the wiring consistent (decision 7).

#### Option B: nested sub-graph in the loop's config (`config.body = {steps, edges}`)
- Pro: containment is structural; no consistency rule needed.
- Con: every consumer of `steps` (references, reports, run monitor, debugger,
  breakpoints, agent toolkit, MCP prompts, import/export, catalog generation)
  must recurse; step ids become scoped; a stored-shape migration for every
  existing loop.

#### Option C: derive the container from wiring only (no stored field)
- Pro: no model change.
- Con: an unwired step cannot be "in" a box, so drag-into-container cannot
  work; ambiguous when a body wires back out (fact 2).

### Failure handling

#### Option A: policy per step with a loop default (chosen, for steps whose error port is unwired)
- Pro: the author decides at the step that can fail; the loop default covers
  the common "skip bad items" case in one click.
- Con: one more field to explain; inherited value must be visible on the node.

#### Option B: an `error` output port on each step (n8n "continue using error output") (chosen, together with A)
- Pro: arbitrary recovery flow per failure (timeout → reversal, decline →
  retry with another card), drawn where the reader sees it.
- Con: two output ports on every executing node and one more wiring rule.

#### Option C: loop-level policy only
- Pro: smallest change.
- Con: cannot say "a decline on step 2 is fine, a timeout on step 3 is not".

### Verdict of a handled failure

#### Option A: a handled failure still fails the scenario
- Pro: the Go/No-Go report, gates and trends keep their meaning; this is
  exactly today's `stop_on_failure: false` behaviour.
- Con: a loop that expects some failures (polling, negative tests) cannot
  pass; the author asserts on outcomes instead.

#### Option B: a switch that lets handled failures pass (chosen)
- Pro: a failing flow that is the standard process (timeout → reversal,
  negative tests) passes; the default follows the author's drawn route.
- Con: a green run can contain failed calls; they are visible as `handled`
  steps and scenario notes. Implemented with a new *step* status and the
  existing notes, not a new scenario status (decision 6).

## Trade-off analysis

| | Membership A (`parent`) | Membership B (nested) | Membership C (derived) |
|---|---|---|---|
| Stored-shape change | 1 optional field | migration of every loop | none |
| Consumers to change | validator, runner entry, canvas | every reader of `steps` | canvas |
| Drag a step into a box | yes | yes | no |
| Old scenarios | unchanged | migrated on read | unchanged |
| Consistency rules to review | 5 (decision 7) | 0 | 0 |

## Consequences

**Easier:** a data-driven loop (forEach over 100 cards) can skip a bad item or
stop at the first decline and still run the steps after the loop; a timeout
can route to a reversal and the run still passes; the canvas shows what
repeats; a body that leaks into the main flow is finally reported.

**Harder:**
- Reviewers hold two rules on every runner change: failure goes through the
  one failure helper only, and no loop signal escapes `_run_graph` or a `call`.
- A new step status, `handled`, that every reader of step statuses must know
  (report generators, gates, run monitor, trends).
- `next` inside a `while` loop whose condition does not change spins to
  `max_iterations` (default 1000); the side panel says so when `next` is
  picked inside a `while` loop.
- The orchestrator and the workers must run the same release: an older worker
  ignores `on_failure` and `parent` (it applies `stop_on_failure` as today).
- **Rollback:** reverting the release is safe for runs (old workers ignore the
  new fields), but an older scenario-service drops `parent`, `on_failure` and
  `handled_failures` on the next save of a scenario, and refuses `error`
  wires with 422 (no such port). Remove error wires before rolling back, or
  export the scenarios that use them first.
- Invariant #1 in `CLAUDE.md` gains one sentence: inside a container, edges
  never cross the box except the loop's own `loop` edge.

**Revisit:**
- `retry: n` before the route applies (flaky upstreams).
- "Expect failure" (a step that must fail) as a first-class flag rather than
  `ignore` + assertions.
- Containers for other kinds (a "group" box without loop semantics).

**Out of scope here:** retries; a new scenario status; network-flow canvases
(their `group` node is a wiring target, not a box).

## Action items

### Phase 1: model, validator, runner (scenarios and participant flows)
1. [x] `Step.parent`, `Step.on_failure`, loop `config.on_failure` /
   `config.handled_failures`, scenario `handled_failures`, `error` ports in
   `node_output_ports` (`scenario-service/models/scenario.py`); mirror in
   `portal/src/app/constructor/scenario.models.ts`.
2. [x] Validator rules of decision 7 + tests in
   `scenario-service/tests/test_graph_validation.py` (10 new tests; 3 port
   expectations updated for `error`).
3. [x] Runner: `_failure_route` + `_after_outcome` replace the seven per-kind
   blocks; context entry for every executing outcome; error-port routing;
   `_NextIteration` / `_BreakLoop`; `finally` restore of `context["loop"]`;
   `_graph_entry` skips contained steps; `handled` status, aggregated
   `ScenarioResult.handled` + notes; loop outcome `response`. Tests:
   `worker/tests/test_loop_failure_policy.py` (14).
   - *Found while implementing:* one `ScenarioRunner` drives concurrent
     scenarios (`asyncio.gather` in `engine.py`), so the route decided before a
     node runs lives in a `ContextVar` (`_FAILURE_ROUTE`) that `_emit` reads;
     the published event and the stored outcome therefore carry the same
     status (tested). A sub-scenario run resets it, so a `call` step's route
     never reaches its children (tested).
   - *Found while implementing:* the original status lives in
     `StepOutcome.failure_route = {"route", "status"}` (also set for an
     explicit `stop`, which is never `handled`), and the `STEP_RESULT` event
     carries it for the live monitor.
   - *Found while implementing:* a step that raised writes a context entry
     only on a chosen route, so `${a.response.x}` after a legacy failure stays
     "unresolved" exactly as before.
4. [x] Participant-flow save/load keeps the fields
   (`test_loop_container_and_failure_route_survive_save`); scenarios keep them
   through create / get / duplicate / JSON and YAML export-import
   (`test_loop_container_fields_survive_save_duplicate_and_export`); an
   inline run through the orchestrator's `/runs` reaches the worker with them
   (`test_inline_run_keeps_failure_routes_through_the_orchestrator`).
5. [x] Report service: `handled` counts as passing in `run_rollup`
   (pass rate), JUnit (passing case + `system-out`), and the sign-off evidence
   (a **handled** tile + per-scenario line). Gates read `handled` as neither
   failed nor skipped; a step that passed in the baseline and is `handled` now
   shows as a regression (deliberate). 3 tests.
   - *Found while implementing:* `_relay_is_terminal` (orchestrator) treated
     any outgoing edge as "graph-computed"; an `error` wire alone now keeps the
     relay a transparent proxy (test in `test_relay_launch_mode.py`).

### Phase 2: canvas
6. [x] Container geometry as pure functions in
   `constructor/editor/loop-container.model.ts` + spec (7 cases, run with
   `tsc` + a jasmine shim; the portal has no test runner installed).
7. [x] Draw boxes (outer first, behind wires and nodes), drag into/out of a
   box (measured against the box without the dragged node; crossing wires are
   removed with a message), move members with the loop, palette drop into a
   box, double-click joins the selected step's box, "Make container" on old
   loops, deleting a loop releases its members.
   - *Found while implementing (review):* measuring a drop against boxes
     that left the dragged node out ejected a loop's only member on any
     nudge. The drop is now measured against the boxes as they stood when
     the drag began, grown by 24 px (`containerAfterDrag`), and those boxes
     stay on screen while a member is dragged; a dragged loop carries its
     live box. A step dragged out of every loop loses an `on_failure` of
     next/break (it would not save), with a message.
   - *Found while implementing (review):* the runner's context entry for a
     step that raised was built from the outcome after `_emit` had redacted
     it, so a reversal could send `***`. `_emit` now keeps the live values
     on the route for `_after_outcome` (test with `__secrets__`).
   - *Found while implementing:* a fresh loop has no members, so it gets an
     empty drop zone ("Drop steps here to repeat them"); the first step
     dropped in is wired from the loop's `loop` port automatically.
     Auto-connect never uses an `error` port and never crosses a box.
8. [x] Red `error` port at each executing node's bottom-right (main ports and
   node heights unchanged), red dashed error wires labelled "on error",
   failure badge on the node, On failure picker (step), Steps inside — on
   failure and Handled failures inside (loop), Handled failures (scenario
   settings), each with a one-line explanation.

### Phase 3: results and docs
9. [x] Run monitor: `handled` amber rows, a `↷ N handled` filter, the route
   on the status; run report: amber `handled` steps with a `↷ route` chip and
   a `↷ N handled` badge per test case. The sign-off page's downloadable HTML
   carries the handled tile (report_service); the page itself shows gates only.
   The scenario-level Handled failures switch is hidden in the flow editor
   (the flow runner has no scenario-level setting; loop settings apply).
10. [x] Docs: `docs/scenarios/loops-and-failures.md` (linked from
    `docs/README.md` and `README.md`), invariant #1 sentence in `CLAUDE.md`,
    MCP `create_participant_flow` docstring mentions the `error` port.
11. [ ] Host click-through: forEach container, fail one item under each
    policy and via an error port, check the run monitor, the report and the
    sign-off page; light and dark theme.
