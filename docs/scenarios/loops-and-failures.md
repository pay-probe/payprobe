# Loops and failure routes

A **loop** repeats a group of steps; every step that executes something can
**fail**, and you decide what a failure does. In many payment processes a
failing call is the standard path, not an accident: an authorization that
times out is followed by a reversal; a decline on one test card moves on to
the next card. PayProbe lets you draw that path and still pass the run.
Design record: ADR-0014.

## The loop container

Drop a **Loop** node on the canvas. Next to it a dashed box appears with
"Drop steps here to repeat them". Every step you drop into the box belongs to
the loop (its `parent` is the loop's id), and the first one is wired from the
loop's **loop** port automatically.

| Loop port | Runs |
|---|---|
| `loop` | the first step of the body, once per pass |
| `done` | after the last pass |
| `error` | when a failure leaves the loop (`on_failure: break`); unwired, `done` runs instead |

- **Drag a step into or out of the box** to change membership. A wire that
  would then cross the box edge is removed, with a message.
- **Drag the loop node** and its whole box moves with it.
- **Wires never cross the box**, except the loop's own `loop` wire into the
  body. To leave a loop early on failure use **Leave loop**; otherwise the
  body ends when its last step has no next step.
- Loops nest: a loop inside a box gets its own box.
- **Deleting a loop** keeps its steps; they move one level up.
- Inside the body, `${loop.index}` and `${loop.item}` hold the current pass.

### Loops saved before containers

A loop whose body is wired from its `loop` port but not inside a box still
runs exactly as before. Its panel offers **▣ Make container**, which puts the
body chain in the box. If that body wired back into the main flow (so every
pass also ran the steps after the loop), the validator now warns about it,
and Make container tells you which wires to remove.

## What a failure does

A step **fails** when an assertion fails, the adapter errors, a reference does
not resolve, or a code/HTTP/crypto node returns an error. What happens next is
decided in this order:

1. **The red `error` port**, if wired. Every executing node (action, code,
   HTTP, init, crypto, call, relay) has one at its bottom-right corner. Drag it
   onto the step that handles the failure, for example a reversal.
2. **On failure** of the step (side panel).
3. **On failure** default of the nearest loop around the step (loop panel,
   "Steps inside — on failure").
4. The scenario's **Stop on first failure**: on = stop, off = ignore.

| On failure | Inside a loop | Outside a loop |
|---|---|---|
| Stop scenario | end the run now | end the run now |
| Next iteration | skip the rest of this pass, start the next item | not allowed |
| Leave loop | exit the loop: its `error` port, else `done` | not allowed |
| Ignore | carry on to the step's `out` | carry on to the step's `out` |

The node shows a small badge (`on fail: Next iteration`,
`on fail → error port`) whenever its route is not the scenario default, and
the side panel says in one sentence what a failure will do and where that
choice comes from.

A failed step is readable downstream, so a recovery path can use it:

| Reference | Value |
|---|---|
| `${auth.status}` | `failed` or `error` |
| `${auth.error}` | the error text (for example `timeout after 30000 ms`) |
| `${auth.request.*}` | what was sent, for a reversal |

## Does a handled failure fail the run?

A failure is **handled** when it took the `error` port or an On failure
choice other than Stop. Whether it fails the run is a setting:

- **Scenario settings → Handled failures**: *Count as passed* (default) or
  *Fail the run*.
- **Loop panel → Handled failures inside** overrides it for the steps in that
  loop.

Counted as passed, the step's status is **`handled`** (amber), the scenario
stays `passed`, and the run keeps a count per step and route
(`handled: {"auth:error_port": 1}` in the summary, `handled:auth:error_port`
in the scenario notes). The run monitor's feed has a `↷ N handled` filter,
the report shows the route on the step, JUnit reports it as a passing case
with a `system-out` line, and the Go/No-Go evidence shows a **handled**
count next to the verdict.

Counted as a failure, the step keeps `failed` / `error` and the scenario
fails, but the route is still taken (the reversal still runs).

A failure that only followed **Stop on first failure** (nothing set on the
step or loop) is never handled: old scenarios keep their verdicts.

## Example: timeout, then reversal

```
auth (0200) ──out──▶ capture
   └──error──▶ reversal (0400, DE 11 = ${auth.request.values.11})
```

The scenario passes when the host answers (capture runs) and when it times
out (the reversal runs and `auth` shows `handled`). It fails only if the
reversal itself fails.

## Example: skip a bad card

A `forEach` loop over `${cards.response.list}` with **Steps inside — on
failure: Next iteration**: a declined card skips the rest of its pass, the
loop goes on with the next card, and `done` runs once at the end. The loop's
result shows `{"iterations": 20, "ended": "done", "failed_iterations": 3}`.

## Limits

- **Next iteration** inside a `while` loop whose condition does not change
  repeats until **Max iterations** (default 1000); the panel warns.
- A relay node in a participant flow stays a transparent byte-level proxy
  when only its `error` port is wired; only an `out` wire makes it
  graph-computed.
- The orchestrator and the workers must run the same release: an older
  worker ignores `on_failure`, `parent` and `error` wires and applies Stop on
  first failure as before.
- Edge-less (linear) scenarios have no ports: only Stop, Ignore and Handled
  failures apply there.
