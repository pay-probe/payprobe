# ADR-0009: Payment-provider integration — PSP packs, sandbox driving, provider simulators, and an MCP control-plane adapter

**Status:** Accepted — implemented (phases 0–5, 2026-07-20 → 2026-07-21)
**Date:** 2026-07-20
**Deciders:** PayProbe maintainers (David + reviewers)

> **Implementation summary (2026-07-21).** All five phases landed; backend +
> tests green per-package in the sandbox (worker / orchestrator /
> scenario-service / mcp-server / assistant). Host `make test` and a portal
> `npm run build` are still owed (the Angular app can't be compiled in an AI
> sandbox — the PSP simulator presets, the adapter-catalog `mcp` entry and the
> preset `webhooks` blocks are written to pattern, not claimed as verified).
> Deviations and additions discovered while building, recorded honestly:
>
> - **`accept_statuses` on http actions** — a provider *decline* (Stripe 402,
>   PayPal 422) is the outcome under test, not a transport failure; an action
>   may declare accepted non-2xx statuses so assertions decide the verdict.
> - **Per-action `headers`** (merge over connection headers) — carries a
>   provider's negative-testing header (PayPal's `PayPal-Mock-Response`).
> - **`health_any_status` / `health_path` on http connections** — a provider
>   API root answers 401/404 anonymously (which *proves reachable*); the strict
>   default is unchanged.
> - **Assertion field extraction learned list indices** (`details[0].issue`) —
>   provider payloads are list-shaped; `_extract_field` now matches the bracket
>   syntax `${...}` interpolation already supported.
> - **`headervalue` joined the encrypted secret-named fields** — header-style
>   auth (Adyen's `X-API-Key`) encrypts at rest like `token`/`client_secret`.
> - **`HttpResponder._response_code` was defined but never wired** (TCP always
>   called it) — now live, so every HTTP simulator's by-decision stats buckets
>   work (CyberSource included).
> - **Webhook emission** (phase 4) lives in the HttpResponder base
>   (fire-and-forget so the reply path is never delayed; chaos on the emission
>   leg incl. corrupt-after-signing; `stats()["webhooks"]`), with
>   provider-correct signatures (Stripe `t=…,v1=HMAC`; Adyen's colon-joined
>   notification HMAC + the `[accepted]` contract; PayPal transmission headers
>   with an honestly-labelled HMAC stand-in — the real scheme is cert-based).
> - **The `mcp` client adapter** (phase 3) uses per-call sessions to respect
>   anyio task affinity; `mcp` joined `_ALLOWED_ADAPTERS`.
> - **Diagnostics `providers` layer** (phase 5) reports each provider
>   connection: credential-set → token-obtainable (oauth2) / reachable (http,
>   remote MCP); stdio MCP is reported "manual" (never spawned).
> - **Assistant / MCP-server exposure** (phase 5 decision): **no new tools.**
>   The MCP server already exposes `list_packs` / `install_pack` /
>   `diagnose_platform` / `playground_*`, which give an AI client the whole
>   provider on-ramp (install a pack → the providers layer diagnoses it → fire
>   playground traffic) — `diagnose_platform` surfaces the new layer for free.
>   Assistant→provider-MCP *passthrough* stays deferred: the `mcp` adapter is
>   unproven in scenario use and no need has been shown (invariant #3 — a tool
>   ships when a real need does, not before). Scenarios call `mcp` connections
>   as ordinary adapter actions; prove it there first.

## Context

PayProbe simulates the *inside* of a payment network — schemes (ISO 8583,
VISA Base I), HSMs, gateways (CyberSource-style REST), messaging fabric
(NATS) — but has no story for **payment service providers as they exist
commercially**: Stripe, Adyen, PayPal, Square, Razorpay, Mollie, Paddle,
Chargebee. A team whose "payment network" is *merchant backend ↔ PSP* rather
than *acquirer ↔ switch ↔ issuer* cannot model it as a digital twin today,
even though the thesis (§1 of ATLAS) applies identically: model the
counterparty, drive realistic traffic, inject faults, walk away with a
verdict.

Three distinct integration needs were identified (all three are wanted; the
question is the mechanism and the phasing):

1. **Drive real provider sandboxes** — scenarios and networks send traffic to
   Stripe test mode, Adyen test, PayPal sandbox, so a merchant integration can
   be exercised against the real counterparty's real behavior.
2. **Use the providers' MCP servers** — every major PSP now publishes a Model
   Context Protocol server; the table below is the landscape. PayProbe already
   *serves* MCP (`packages/mcp-server`); the question is what a PayProbe MCP
   *client* is for.
3. **Simulate providers offline** — provider-shaped simulators (the
   CyberSource precedent) so CI and laptops never depend on external services,
   and so the chaos/resilience machinery can storm a "Stripe" that is safe to
   break.

### The provider MCP landscape (as of 2026-07)

| Provider | MCP capabilities | Status |
|---|---|---|
| Stripe | Payments, customers, products, subscriptions, refunds + Stripe docs | Available — remote (`mcp.stripe.com`, OAuth or bearer key) and self-hosted (`@stripe/mcp`) |
| PayPal | Orders, payments, invoices, subscriptions, disputes, transaction search | Available — remote and local (`npx @paypal/mcp`, `PAYPAL_ENVIRONMENT=SANDBOX\|PRODUCTION`) |
| Square | Payments, orders, catalog, customers, payouts | Available — remote and sandbox-local |
| Adyen | Checkout, payment links, refunds, account + terminal management | Available — **self-hosted only** (`Adyen/adyen-mcp`, TypeScript), **alpha**; test env = API key, live adds URL prefix |
| Razorpay | Payments, orders, refunds, QR, settlements, payouts | Available — remote or local |
| Mollie | Payments, captures, payment links, settlements, subscriptions, terminals | Available — remote |
| Paddle | Products, pricing, billing, subscriptions, transactions, refunds | Available — remote, sandbox and live |
| Chargebee | Subscription billing, invoices, customers, quotes, docs | Available |
| Coinbase | Agent wallets, on-chain, stablecoin, x402 | Available |
| Mastercard | Developer docs, guides, API specs | Available, docs-only |
| Visa | Intelligent Commerce, tokenization, VTS | Partner/onboarding access |
| American Express | Amex ACE Developer Kit | Announced, not public |

Verified in detail for the phase-1 trio (Stripe/Adyen/PayPal); the rest are
recorded as landscape. Two observations matter architecturally:

- **These MCP servers are control planes, not wire protocols.** Their tools
  create customers, payment links, refunds, invoices; search resources; search
  documentation. They are agent-facing *operations* surfaces. No provider
  positions its MCP server as the transport a production merchant integrates
  checkout through — that remains REST/SDK. Several are remote-hosted with
  provider-side rate limits; Adyen's is alpha with a subset of endpoints.
- **They are converging on one shape.** One generic MCP client that can
  `list_tools` / `call_tool` over streamable HTTP (remote) or stdio
  (self-hosted) reaches *every* row of the table — including providers not yet
  published — with zero per-provider code.

### Forces

- **CI is offline by design** (ATLAS §9: fakes for providers, ~1,275 tests,
  no external dependencies). Real-sandbox traffic must be opt-in and gated on
  credentials being present — never a default test path.
- **Load discipline.** The load subsystem targets 20K TPS. Pointing that at a
  real provider sandbox violates every provider's ToS and gets keys revoked.
  The guardrail must live in the tool/registry layer, not in documentation
  (invariant #6).
- **The TestPay lesson** (ATLAS §10): per-provider adapter classes were built
  once (`TestPay` → `RestPay`) and folded into the generic `http` adapter.
  Provider specificity belongs in **config/data**, not code. Overlapping
  abstractions get cut in favour of the one with the clearer owner.
- **Secrets and environments.** Provider API keys are connection secrets:
  SecretBox at rest, masked in APIs (invariant #8). Sandbox-vs-live base URLs
  and Adyen's live URL prefix are exactly what the per-environment override
  matrix exists for — connection is the shape, matrix is the values
  (invariant #7).
- **Auth is the one real gap in the data plane.** The `http` adapter already
  does base_url + named actions + headers + bearer auth. Stripe (`Bearer
  sk_test_…`) and Adyen (`X-API-Key`) fit today. PayPal does not: it needs an
  OAuth2 client-credentials exchange (client_id + secret → short-lived access
  token, refreshed before expiry). That capability is provider-agnostic and
  belongs in the shared http runner, once.
- **Webhooks are half of every PSP integration.** Stripe events, Adyen
  notifications, PayPal webhooks — the provider *calls the merchant back*, and
  merchant webhook handling is precisely the kind of logic teams need to test
  (signature verification, retries, out-of-order delivery). The twin is not
  faithful without this leg. `HttpResponder`/participant flows can terminate
  webhooks already; what's missing is simulators that *emit* them, and (for
  real sandboxes) public reachability — an ops problem PayProbe should not
  own.
- **Optional-dependency discipline** (registry precedent: grpcio, aiohttp,
  nats-py): an MCP client library must degrade to "adapter unavailable",
  never an import crash. The `mcp` package is already a platform dependency
  on the server side.
- **One tool layer** (invariant #3): any assistant exposure goes through
  `payprobe_common/agent_toolkit.py` + both backends + `gen_catalog.py`, or it
  doesn't ship.

## Decision

Integrate providers on **three planes, each mapped to machinery that already
exists** — packs and the `http` adapter for the data plane, one new generic
`mcp` adapter family for the control plane, `HttpResponder` subclasses for the
offline plane. No per-provider adapter classes, ever.

### Plane 1 — Data plane: provider packs over the generic `http` adapter

A **provider pack** (the existing pack machinery: `list_packs` /
`install_pack`) is the unit of provider support. A pack ships *data only*:

- **Connection presets** — `adapter: "http"`, sandbox `base_url` as the
  default env value (live URL / Adyen prefix as matrix overrides), auth shape
  referencing a secret, named `actions` for the provider's core objects
  (Stripe: `create_payment_intent`, `confirm`, `create_refund`, `get_intent`;
  Adyen: `payments`, `payments_details`, `refunds`; PayPal: `create_order`,
  `capture_order`, `refund_capture`), idempotency-key header options.
- **Starter flows + scenarios** — authorize→capture→refund lifecycles, decline
  paths, assertion templates on provider response shapes.
- **Test-data pools** — the provider's published test cards (Stripe `4242…`
  ladder, Adyen test cards, PayPal sandbox accounts) as card pools usable via
  `${card.*}`.
- **A webhook-receiver starter flow** — trigger on `POST /webhooks/<provider>`
  with the provider's signature-verification rule (Stripe `Stripe-Signature`
  HMAC, Adyen HMAC, PayPal verification), so the merchant side of the
  conversation is authorable on the canvas.
- **The matching simulator preset** (plane 3).

One bounded code change supports this: an **auth strategy layer** in the
shared http runner — existing `bearer`/header auth plus
`oauth2_client_credentials` (token URL, cached token, refresh-before-expiry,
secret-backed credentials). Implemented once; PayPal is merely its first
consumer.

**The guardrail:** pack connection presets that point at real provider
endpoints carry `external: true`. The load engine **refuses** to start a load
run against an `external` connection — loudly, at `start_load_run`, in the
registry layer. Scenarios, playground and functional runs may use them; load
runs target simulators. This is invariant #6 applied to other people's
infrastructure.

### Plane 2 — Control plane: one generic `mcp` adapter family

`worker/adapters/mcp_client/` registered lazily under `"mcp"` (same pattern
as `nats`): actions `list_tools` and `call_tool(name, arguments)`; transports
**streamable HTTP** (remote servers — `mcp.stripe.com`, PayPal remote) and
**stdio** (self-hosted — Adyen's server, `@stripe/mcp`); auth = bearer token /
headers from connection secrets. `health_check()` = initialize + list_tools,
never raises.

What it is *for*: **fixture provisioning and out-of-band verification as
scenario steps.** "Create a test customer and product before the run"; "after
the capture scenario, `call_tool(search_stripe_resources)` and assert the
payment exists on the provider side." That second usage is genuinely new
capability: asserting on the *provider's* view of the world, not just the API
response. It is not for payment traffic — the data plane is (see Option B).

Assistant exposure is deferred by default: scenarios calling `mcp` connections
is just an adapter action; giving the *assistant* provider-MCP passthrough
would require agent_toolkit handlers (invariant #3) and an `execute`-tier
treatment (ADR-0007 precedent), and earns its keep only after the adapter
proves useful in scenarios.

### Plane 3 — Offline plane: provider simulators

`HttpResponder` subclasses in `adapters/scheme/` (the `CyberSourceSimulator`
precedent — inherit lifecycle, rules, stats, peers, chaos):

- **`StripeSimulator`** — `/v1/payment_intents` + `/v1/refunds` with the
  documented test-card decision ladder (amount/PAN-triggered declines,
  `requires_action` for 3DS cards), Stripe-shaped JSON + error envelopes.
- **`AdyenCheckoutSimulator`** — `/payments`, `/payments/details`, `/refunds`
  with the `resultCode` ladder (Authorised / Refused / ChallengeShopper).
- **`PayPalOrdersSimulator`** — v2 orders create/capture/refund with PayPal's
  status transitions.

Plus one new shared capability: **webhook emission** — a simulator option
(`webhooks: {url, secret, events[]}`) that POSTs provider-signed events on
state transitions (payment succeeded, refund completed). This closes the loop
offline: the simulated Stripe calls the merchant participant back, signature
verification and all, on the same canvas — and the chaos dial applies to the
webhook leg too (delayed/dropped/duplicate webhooks are exactly the failure
modes merchants mishandle).

Load runs, chaos storms and resilience certification target these simulators
with no restrictions — that is the point of them.

## Options considered

### Option A — Three planes on existing machinery (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium — one auth strategy, one generic adapter, N rules-driven sims; packs are data |
| Fidelity | High — real sandboxes for truth, sims for scale/chaos, MCP for provider-side verification |
| CI posture | Fully offline by default; sandbox smoke opt-in on credential presence |
| Scaling to more providers | Config-only per provider once the planes exist |

**Pros:** every plane reuses a proven pattern (packs, http adapter,
HttpResponder, lazy registry); provider count scales in data, not code; the
guardrail story is enforceable. **Cons:** pack content is a real maintenance
surface (provider APIs drift; samples need refresh discipline); three planes
must be explained clearly in docs or users will point load runs at sims'
real-sandbox siblings and wonder why one is refused.

### Option B — Provider MCP servers as *the* integration transport

Route all provider traffic — including payment scenarios — through the
providers' MCP tools.

**Pros:** one mechanism for everything; no http presets needed.
**Cons:** misrepresents reality — no merchant integrates checkout through an
MCP server, so scenarios would exercise a path production never takes (the
twin stops being a twin); remote rate limits make load testing impossible and
even functional suites fragile; Adyen's is alpha and self-host-only; tool
inventories are provider-controlled and changing monthly; CI would depend on
external services. MCP is a control plane and earns exactly that role.
**Rejected as transport; adopted as plane 2.**

### Option C — Per-provider SDK adapters (`StripeAdapter`, `AdyenAdapter`, …)

Wrap official Python SDKs as adapter classes.

**Pros:** typed request building; SDKs track API changes.
**Cons:** this is TestPay again, N times (ATLAS §10 — the fold into generic
`http` was deliberate and earned); each SDK is a heavyweight dependency on
the optional-deps list; SDK retry/connection pooling fights the engine's own
timing, chaos and stats instrumentation; provider count scales in code and
dependencies. The http adapter + pack actions achieve request parity in
config. **Rejected.**

### Option D — Proxy-tap record-and-replay against sandboxes only

No new integration surface; put ADR-0002's `TcpProxy` between the merchant
and the sandbox, capture, save-as-scenario, replay against sims.

**Pros:** zero provider-specific work; strengthens the record-and-replay
roadmap item. **Cons:** all three providers are HTTPS-only, so this is
**blocked on ADR-0008** (TLS termination — proposed, not built); capture
gives you yesterday's conversation, not an authorable counterparty; no
provisioning, no webhooks. **Rejected as the mechanism; noted as a future
complement once 0008 lands** (a TLS-terminating tap in front of a provider
sim is a natural evolution).

### Deferred (recorded on purpose)

- **Public webhook reachability for real sandboxes** (tunnel/relay so
  `mcp.stripe.com`-side events reach a laptop PayProbe). Real infrastructure,
  real security surface, not core. Sims' webhook emission covers the testing
  need; document `stripe listen`-style forwarders as the manual workaround.
- **Agentic-payment rails** (Coinbase x402, Visa Intelligent Commerce) — a
  different problem (PayProbe *making* payments, not testing them). Revisit
  if agentic-commerce testing becomes a user ask.
- **Card-network MCPs** (Mastercard docs-only, Visa partner-gated, Amex
  unreleased) — nothing to integrate yet; the scheme roadmap (spec-exact VISA
  Phase 2, Mastercard) is the ISO 8583 side and proceeds independently.
- **Square/Razorpay/Mollie/Paddle/Chargebee packs** — config-only once the
  planes exist; sequence by demand.

## Trade-off analysis

The real decision is **where provider specificity lives**. Option C puts it
in code (N adapters), Option B outsources it to the providers' agent surface
(wrong plane), Option A keeps it in data — packs — with exactly three generic
code additions (oauth2 strategy, mcp adapter, webhook emission), each useful
beyond any single provider. That matches the platform's strongest recurring
lesson (ATLAS §10): when specificity leaks into code, it gets cut later at
higher cost.

The second decision is **honesty about what each plane can claim**. A green
run against `StripeSimulator` proves the merchant logic handles Stripe-shaped
conversations including failures; a green run against Stripe's sandbox proves
the integration actually works today; an MCP-step assertion proves the
provider recorded what we think it did. The report/provenance machinery
(ADR-0003) already records what ran against what — provider runs inherit that
for free, and the distinction stays visible in the verdict.

The MCP-client question deserves one more sentence: PayProbe serving MCP and
consuming MCP are symmetric halves of the same bet — that operations surfaces
converge on this protocol. Betting the *data plane* on it would be confusing
the console for the wire.

## Consequences

- **Easier:** merchant↔PSP topologies become authorable twins (merchant flow
  + provider sim + webhook leg on one canvas); provider-logic CI runs
  offline; resilience certification extends to "your PSP browns out — does
  your checkout degrade or die?"; provider-side state becomes assertable
  (plane 2); each further provider is a pack, not a project.
- **Harder:** pack maintenance is ongoing (drifting provider APIs — mitigated
  by keeping actions to the stable core objects); the http runner's auth
  config grows a strategy dimension; simulators gain an outbound leg (webhook
  emission) that stats/traces must attribute clearly; docs must teach
  three planes without jargon.
- **Guardrail to enforce and test:** `external: true` connections refused by
  the load engine; refusal message names the sim preset to use instead.
- **New docs on acceptance:** CLAUDE.md gotcha ("never point load at
  `external` connections — the registry will refuse; use the provider sim"),
  ATLAS §11 entry, pack authoring notes in `docs/`.
- **Revisit later:** TLS tap in front of provider sims (post-ADR-0008);
  assistant passthrough to provider MCPs (post-adapter, via agent_toolkit +
  execute tier); webhook tunnel integration; remaining provider packs.

## Action items (phased — implementation handover)

Each phase lands green before the next starts; per-package tests with
`PYTHONPATH=packages` from `packages/` as usual. All external-network tests
are opt-in (skip without credentials/reachability), matching the NATS
integration-test precedent.

1. [x] **Phase 0 — auth strategy + guardrail.** `oauth2_client_credentials` +
   `form` bracket encoding in the shared http runner (token cache keyed by
   (url, client, scope), refresh 60 s before expiry; basic/body credential
   styles); `external: true` on ConnectionDraft (not stripped from worker
   configs); `POST /load-runs` refusal covering env configs, registry docs
   (the mix path) and group members. `test_http_oauth_form.py`,
   `test_load_external_guardrail.py`.
2. [x] **Phase 1 — Stripe end-to-end.** `StripeSimulator` (kind `stripe`;
   form-body decode, test-card ladder, PaymentIntent status machine, refund
   ledger) + `stripe_provider` pack (4 cases; `stripe_simulator` /
   `stripe_sandbox` connections, `stripe_test_cards` pool; create-only
   install) with a live-simulator baseline; opt-in sandbox smoke on
   `STRIPE_TEST_KEY`. (Webhook-receiver flow moved to phase 4; starter flows
   dropped — `FlowStep` carries no payload.)
3. [x] **Phase 2 — Adyen + PayPal.** `AdyenCheckoutSimulator` (kind `adyen`;
   documented holderName / acquirer-code refusal triggers, 3DS2 challenge →
   `/payments/details`, async-accepted refunds) + `PayPalOrdersSimulator`
   (kind `paypal`; serves its own `/v1/oauth2/token`, INSTRUMENT_DECLINED via
   the real negative-testing header) + both provider packs with live-sim
   baselines incl. a strict-token oauth2 end-to-end. Confirmed the convention
   set: Stripe = form+bearer, Adyen = JSON+header, PayPal = JSON+oauth2.
4. [x] **Phase 3 — `mcp` adapter family.** `adapters/mcp_client/` (streamable
   HTTP + stdio, per-call sessions, `list_tools`/`call_tool` + named-tool
   convenience, bearer/header auth, health = initialize+list_tools), lazy
   registration + `_ALLOWED_ADAPTERS` entry, tests against an in-process
   FastMCP double over both transports + opt-in `mcp.stripe.com` check;
   `stripe_mcp` / `adyen_mcp` / `paypal_mcp` pack presets (`external: true`);
   adapter-catalog entry (host-build caveat; typed connection-editor fields
   still owed — the JSON editor covers it meanwhile).
5. [x] **Phase 4 — webhook emission.** `WebhookEmitter` in the HttpResponder
   base (fire-and-forget, events filter, chaos on the leg, sent/failed/dropped
   in `stats()`), provider-correct signatures, emission wired into all three
   sims' state transitions, pack `participant_flows[]` +
   `merchant_webhooks_<provider>` inbound connections + receiver flows,
   preset `webhooks` blocks (unverified), an 11-test emission suite + the
   offline sim→receiver-flow loop test. Trace hops for the emission leg
   deferred (needs in-band correlation — ADR-0007 precedent).
6. [x] **Phase 5 — surfaces + docs.** Diagnostics **providers layer**
   (`/diagnostics`: credential-set → token-obtainable / reachable; stdio MCP
   reported manual; 8 tests) — also fixed the stale doctor skill (the `nats`
   layer was undocumented). Pack authoring guide
   ([docs/authoring-a-provider-pack.md](../authoring-a-provider-pack.md));
   CLAUDE.md + ATLAS + ROADMAP + operator-skills swept. MCP/assistant exposure
   decided: no new tools (existing `install_pack` / `diagnose_platform` /
   `playground_*` suffice; passthrough deferred) — so no `gen_catalog.py`
   run needed (registry.py / prompts.py untouched). ADR status flipped.
