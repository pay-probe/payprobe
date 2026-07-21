# Authoring a payment-provider pack

A how-to for adding a new payment service provider (Stripe, Adyen, PayPal,
Square, Razorpay, Mollie, Paddle, Chargebee, …) to PayProbe. The design and its
reasoning live in [ADR-0009](adr/0009-payment-provider-integration.md); this
guide is the practical recipe: what to write, where, and what you get for free.

The governing principle (from the ADR): **provider specificity lives in data,
not code.** Adding a provider is mostly a *pack* — connection presets, actions,
scenarios, a card pool — plus one rules-driven *simulator* so the pack runs
offline. Only three generic capabilities ever needed real code, and they
already exist: an OAuth2 auth strategy, form-body encoding, and webhook
emission. If you find yourself writing a `FooAdapter` class, stop — that is the
TestPay mistake ([ATLAS §10](ATLAS.md)) and the `http` adapter already does it
in config.

## The three planes you're wiring

A provider is integrated on three planes, each mapped to machinery that already
exists:

1. **Data plane** — drive the provider's REST API (real sandbox *or* the local
   simulator) through the generic `http` adapter. This is where payments flow.
2. **Offline plane** — a `HttpResponder` subclass that speaks the provider's
   contract, so CI and laptops never touch the network and chaos/load can storm
   a provider that's safe to break.
3. **Control plane** — the provider's MCP server, reached through the generic
   `mcp` adapter, for fixture provisioning and provider-side assertions.

You author all three in one pack.

## Anatomy of a pack

Packs are defined in `packages/scenario-service/models/pack.py` as `Pack`
objects in `BUILTIN_PACKS`. A provider pack uses these fields:

| Field | What it carries |
|---|---|
| `cases` | `PackCase`s — each a full scenario document, imported into a new project on install |
| `connections` | connection presets (create-only on install): the simulator, the `external: true` sandbox, the MCP server, and the inbound webhook endpoint |
| `card_pools` | the provider's documented test cards, usable via `${card.*}` |
| `participant_flows` | webhook-receiver flows (the merchant side of the back-channel) |
| `mock_runnable` | **`False` for provider packs** — their runnable baseline is the provider simulator, not `MockAdapter` (the mock gate skips them) |

Install is **create-only**: an existing document with the same name/id is never
overwritten, so re-installing a pack can't clobber a user's edited base URL,
credentials, or pool.

## The data plane: connection presets + actions

A provider connection is `adapter: "http"` with a `base_url`, an authentication
block, and named `actions`. Each action maps a name to `{method, path}` plus
optional per-action knobs. The three providers cover the three auth shapes you
will meet — copy the closest one:

**Stripe — bearer token + form bodies.** Stripe's API rejects JSON; set
`content_type: "form"` on the connection and payloads are url-encoded with
bracket notation (`card[number]=…`) automatically.

```python
{
  "name": "stripe_simulator",
  "adapter": "http",
  "base_url": "http://127.0.0.1:8085/v1",
  "content_type": "form",
  "health_any_status": True,
  "headers": {"Authorization": "Bearer sk_test_payprobe"},
  "actions": {
    "create_payment_intent": {"method": "POST", "path": "/payment_intents"},
    "create_payment_intent_expecting_decline": {
      "method": "POST", "path": "/payment_intents",
      "accept_statuses": [200, 402],   # a decline (402) is the outcome under test
    },
    "get_payment_intent": {"method": "GET", "path": "/payment_intents/${request.id}"},
    "create_refund": {"method": "POST", "path": "/refunds"},
  },
}
```

**Adyen — API-key header + JSON.** JSON is the default `content_type`; auth is a
custom header.

```python
"authentication": {"type": "header", "headerName": "X-API-Key", "headerValue": ""},
```

**PayPal — OAuth2 client credentials + JSON.** The `oauth2_client_credentials`
strategy fetches a token from `token_url` (cached, refreshed before expiry) and
attaches it as a bearer; the `client_secret` is stored encrypted.

```python
"authentication": {
  "type": "oauth2_client_credentials",
  "token_url": "https://api-m.sandbox.paypal.com/v1/oauth2/token",
  "client_id": "", "client_secret": "",
},
```

### The knobs, and when to reach for them

- **`content_type: "form"`** (connection-level; a per-action `content_type`
  overrides it) — provider requires url-encoded bodies (Stripe). Nested dicts
  flatten to bracket notation.
- **`accept_statuses: [200, 402]`** (per action) — a provider *decline* is a
  legitimate outcome a scenario asserts on, not a transport failure. Without
  this the step fails on the non-2xx before assertions run.
- **`headers: {...}`** (per action) — merges over connection headers; used for
  the provider's negative-testing header (e.g. PayPal's `PayPal-Mock-Response`).
- **`health_any_status: True`** (connection-level) — a provider API root answers
  401/404 to an anonymous GET, which *proves it's reachable*. Without this the
  health probe reads that as "down" and blocks the run's component-health phase.
  `health_path` probes a different path than the base URL.

### The external guardrail

Every connection that points at a **real** provider (sandbox or live) must carry
`external: True`. The load engine refuses to start a load run against an
external connection — load-testing a provider's sandbox violates its terms of
service, so the registry is the bouncer, not the docs. Scenarios, the
playground and functional runs may use external connections freely; load runs
target the simulator instead. The refusal message names the simulator to use.

## The offline plane: a provider simulator

Subclass `HttpResponder` (see `packages/worker/adapters/scheme/stripe_sim.py`
as the template) and implement only what's provider-specific:

- **`PROTOCOL`** — the `kind` string (e.g. `"stripe"`).
- **`_resolve(parsed)`** — route the request and compute the reply. Explicit
  `rules` are evaluated first (they let a test pin an outcome); the gateway
  logic follows. Return `{"status": int, "json": {...}}`.
- **`_response_code(action)`** — bucket the by-decision metric on the *gateway
  decision* (e.g. `succeeded` / `insufficient_funds`) rather than the raw HTTP
  status. (This hook exists on `HttpResponder` and is wired into the stats/chaos
  path — you only override the bucketing.)
- **`_decode(request)`** — override only if the provider uses form bodies; the
  Stripe sim decodes bracket notation back into a nested dict.

Reproduce the provider's **documented test triggers** so a pack case behaves the
same against the sim and the real sandbox: Stripe's test-card ladder
(`4242…` succeeds, `…9995` insufficient funds, `…3155` 3DS), Adyen's
`holderName` / `RequestedTestAcquirerResponseCode` triggers, PayPal's
`PayPal-Mock-Response` header. Aim for **functional** fidelity (the status
machine, the error envelope, the decision set) — not a byte-exact clone.

Register the `kind` in the orchestrator's `_responder_for`
(`packages/orchestrator/api/main.py`):

```python
elif kind == "stripe":
    from worker.adapters.scheme.stripe_sim import StripeSimulator
    cls = StripeSimulator
```

Add a saved-simulator preset to the portal Simulators page
(`portal/src/app/simulators/simulators.component.ts`) — a template constant, a
segmented-button entry, and a `usePreset` branch. (Portal changes need a host
`npm run build`; say so, don't claim verification from the sandbox.)

## Webhook emission: the provider calls back

Real PSPs call the merchant back, and merchant webhook handling — signature
verification, retries, duplicates, out-of-order delivery — is exactly what teams
need to test. Every `HttpResponder` owns a `WebhookEmitter`
(`packages/worker/adapters/http/webhooks.py`); the simulator only decides *when*
to emit:

```python
from ..http.webhooks import stripe_signature

def _emit_event(self, event_type, data_object):
    if not self.webhooks.enabled:
        return
    payload = json.dumps({..., "type": event_type, "data": {"object": data_object}})
    self.webhooks.emit(event_type, payload,
                       {"Stripe-Signature": stripe_signature(self.webhooks.secret, payload)})
```

Emission is **fire-and-forget** — a slow or dead webhook endpoint never delays
the payment reply — and the **chaos dial applies to the emission leg**: drop
loses it, latency delays it, `malformed` corrupts the body *after signing* so a
verifying receiver rejects it. Use the provider-correct signature helper
(`stripe_signature`, `adyen_hmac_signature`, `paypal_transmission_headers`);
where a provider's real scheme can't be reproduced offline (PayPal's is
cert-based), send an honestly-labelled HMAC stand-in rather than pretend.

The pack ships the receiver side: an inbound `merchant_webhooks_<provider>`
connection and a minimal `trigger → reply` participant flow bound to it, so the
emitted webhook lands on a canvas participant out of the box. Point the sim
preset's `webhooks.url` at that connection's port. Signature *verification* is
the merchant logic under test — the shipped flow just acknowledges (Adyen's must
answer the literal `[accepted]`); extend it with branch/code nodes to verify and
dead-letter.

## The control plane: an MCP preset

Add the provider's MCP server as an `adapter: "mcp"` connection, `external:
True`. Remote servers use `transport: "http"` + a `base_url` + bearer auth;
self-hosted ones use `transport: "stdio"` + `command`/`args`/`env`. Scenarios
reach it with `list_tools`, `call_tool`, or the convenience form (the action
name *is* the tool name). Use it to provision fixtures before a run, or to
assert on the provider's own view of the world after one — not to move payments
(that's the data plane). Note that a stdio server is a local subprocess spawned
at run time; a headless worker can't drive an interactive browser-OAuth remote
(PayPal's), so ship the stdio variant there.

## Testing: three levels

1. **Simulator suite** — drive the sim over a real socket, one test per
   decision path (mirror `test_stripe_sim.py`). Assert the status machine, the
   error envelope, the test-card ladder, the responder surface.
2. **Pack-against-simulator baseline** — the provider-pack analog of the mock
   gate: run every pack case against a live instance of your simulator
   (`test_stripe_pack_against_simulator` in
   `scenario-service/tests/test_packs.py`, via the shared `_run_pack_against_sim`
   helper). This proves the pack's connections, actions and assertions line up
   with the sim.
3. **Opt-in sandbox smoke** — gated on a real test key in the environment
   (`test_stripe_sandbox_smoke.py`, skipped unless `STRIPE_TEST_KEY` is set).
   The **only** sanctioned real-API traffic in the repo; keep it to one
   low-volume lifecycle.

Webhook emission gets its own coverage (`test_webhook_emission.py`): recompute
each signature over the raw received body, and assert the reply path is never
delayed by a dead endpoint.

## What you get for free

Once the pack and sim exist, the rest of the platform already knows about them:

- **Load & chaos** target the simulator (the external connection is refused);
  resilience certification scores a provider brownout.
- **The playground** lists the connection and can fire a single action.
- **The doctor** (`/diagnostics`, ADR-0009 providers layer) reports each
  provider connection: "credential not set" (the freshly-installed state),
  "token obtainable" (OAuth2), or "reachable + authorized" — and skips stdio
  MCP servers rather than spawning them.
- **Reports & provenance** record what ran against what, so a green sim run and
  a green sandbox run stay distinguishable in the verdict.

## Checklist for a new provider

1. Simulator: `HttpResponder` subclass in `adapters/scheme/`, documented test
   triggers, `_response_code` bucketing; register the `kind` in
   `_responder_for`; portal preset (host-build caveat).
2. Pack in `pack.py`: `mock_runnable=False`; connections (simulator,
   `external` sandbox, MCP, inbound webhook endpoint); actions with the right
   `content_type`/`accept_statuses`/`headers`; card pool; webhook-receiver flow;
   cases covering auth+refund, a decline, and any challenge path.
3. Auth: pick bearer / header / `oauth2_client_credentials`; secrets ship empty
   and store encrypted.
4. Webhooks: emit on state transitions with the provider-correct signature;
   point the preset's `webhooks.url` at the pack's receiver connection.
5. Tests: simulator suite, pack-against-simulator baseline, opt-in sandbox
   smoke, webhook signature verification.
6. Docs: add the provider to this guide's examples if it introduces a genuinely
   new shape; otherwise it's config, and config needs no prose.
