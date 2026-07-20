# Using the Code Step

The **Code step** runs a small piece of custom **Python** or **TypeScript** inside
a scenario. Use it to transform data between steps, compute values, reshape an
API response, or make a decision that would be awkward to express with the
built-in nodes.

It is the escape hatch: when no adapter action or control node does quite what
you need, drop in a few lines of code.

## The contract

A Code step has **one input** and **one output**:

- It receives the full run **context** — every earlier node's result, keyed by
  node id. Each entry looks like `{ "request": {...}, "response": {...} }`.
- It **returns an object**. Whatever you return becomes available to later nodes
  as `${<node_id>.response.<key>}`.

So a node with id `surcharge` that returns `{ "total": 10150 }` exposes
`${surcharge.response.total}` to everything downstream.

`context` is also available under the alias `inputs`, if that reads better.

## Python

The snippet is the body of a function, so you return with `return`:

```python
# context["auth"] is the result of an earlier node called "auth"
auth = context["auth"]["response"]

base_amount = 10000             # minor units
fee = round(base_amount * 0.015)  # 1.5% surcharge

return {
    "rrn": auth.get("rrn"),
    "approved": auth.get("response_code") == "00",
    "total": base_amount + fee,
}
```

`print(...)` is allowed — it goes to the run logs, not the return value.

## TypeScript / JavaScript

The snippet is the body of an `async` function, so you can `await` and `return`:

```typescript
const auth = context["auth"].response as { response_code: string; rrn: string };

const baseAmount = 10000;
const fee = Math.round(baseAmount * 0.015);

return {
  rrn: auth.rrn,
  approved: auth.response_code === "00",
  total: baseAmount + fee,
};
```

`console.log(...)` goes to the run logs. If you return something that is not an
object it is wrapped as `{ "value": <result> }`.

## Adding a Code step in the editor

1. Open a scenario in the **Constructor**.
2. From the **Logic & Flow** palette on the left, drag **Code** onto the canvas
   (or double-click it to append).
3. Select the node. In the right-hand panel choose the **Language**, write your
   snippet, and set a **Timeout**.
4. Wire it up: connect the previous node's output into the Code node, and the
   Code node's `out` port into whatever runs next.
5. Reference its output downstream, e.g. `${surcharge.response.total}`.

### Test it in isolation

Double-click the node to open the detail view (**INPUT │ Parameters │ OUTPUT**).

- Click **Execute previous nodes** to populate the INPUT pane with real upstream
  results.
- Click **Execute step** to run just this snippet and see its OUTPUT — handy for
  iterating on the code without running the whole scenario.

## How it runs

Each Code step executes in a **fresh, sandboxed subprocess** with a wall-clock
timeout (default 5s, max 60s) and CPU/memory limits, so a slow or broken snippet
is killed instead of stalling the run. The subprocess gets only the JSON context
you see — no scenario state leaks in.

> **Network note:** outbound network access is *not* blocked at this layer. If
> you run untrusted snippets, deploy the worker/orchestrator in a
> network-restricted container. To make an HTTP call as a first-class, visible
> step, prefer the **HTTP Request** node over `requests`/`fetch` in code.

TypeScript needs Node.js + esbuild in the runtime; both ship in the worker and
orchestrator images. Python needs nothing extra.

## Worked example

A complete, runnable scenario lives at
[`examples/scenarios/code_step_surcharge.json`](../../examples/scenarios/code_step_surcharge.json):

```
auth ──▶ surcharge (code) ──▶ approved (if) ──true──▶ settle_check
```

- **auth** — an `http` authorization request.
- **surcharge** — a Code step that reads the auth response, computes a 1.5% fee,
  and returns `{ rrn, approved, fee, total }`.
- **approved** — an `if` node branching on `${surcharge.response.approved}`.
- **settle_check** — a DB probe using `${surcharge.response.rrn}` from the code.

Run it from the editor with **▶ Execute** (mock environment), or in batch:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker
```

You should see the `surcharge` node return `{"fee": 150, "total": 10150, ...}`
and the scenario pass.

## Tips & gotchas

- **Always `return` a dict/object.** A bare value is wrapped as `{ "value": ... }`;
  returning nothing yields `{}`.
- **Reference upstream by node id**, not list position:
  `context["step_001"]["response"]["rrn"]`.
- **Keep it deterministic and fast.** The timeout is a safety net, not a budget —
  long-running work belongs in a real adapter or an HTTP node.
- **`${...}` interpolation does not run inside code.** Read values from `context`
  directly; the `${...}` syntax is for the payload/URL/condition fields of other
  nodes.
- **Errors fail the step.** An exception (Python) or a thrown error (JS) marks the
  node failed, and — with `stop_on_failure` on — halts the scenario. The message
  appears in the node's OUTPUT pane and the run report.
```
