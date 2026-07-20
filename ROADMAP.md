# PayProbe Roadmap

The **maintained roadmap — with the reasoning per item — lives in
[ATLAS §11](docs/ATLAS.md#11-roadmap--with-the-why)**. This file is the short
version.

## Where the platform is today

The core thesis is built and working: model a payment network on a canvas,
start it as a unit, drive realistic traffic through it, inject faults, and
walk away with an evidence-grade verdict. That includes the visual scenario
constructor, protocol simulators (ISO 8583, VISA Base I, payShield 10K HSM,
CyberSource-style REST, NATS/JetStream), canvas-authored networks with
opt-in fleet hosting, distributed load generation, chaos storms + resilience
certification, two-mode reports with Go/No-Go sign-off, end-to-end network
tracing, AI assistants, an MCP server, and an advisory ML insight service.
Decisions and status are recorded honestly in [docs/adr/](docs/adr/) and
[docs/ATLAS.md](docs/ATLAS.md).

## Next

- [ ] Verification pass on the newest portal pages (Playground incl. sample
      chips — backend + page landed per ADR-0007; needs a host build +
      click-through)
- [ ] Proxy tap TLS termination (ADR-0008, the deferred half of ADR-0002)
- [ ] Payment-provider integration (ADR-0009, proposed): provider packs over
      the generic http adapter (+ oauth2 auth strategy), provider simulators
      with webhook emission, and a generic MCP client adapter — Stripe first,
      then Adyen + PayPal
- [ ] Spec-exact VISA (Phase 2), then further schemes (Mastercard next)
- [ ] Consolidated canvas overlays for the network map surfaces (additive —
      see ATLAS §12)

## Eventually (re-evaluate, don't assume)

- Single flow document (ADR-0004 "Option C") — only if the three stores start
  duplicating logic
- k8s substrate for the fleet — only if the Redis bus + hosts hit real limits

## Ideas (community input welcome)

- Mastercard / ISO 20022 message support
- More HSM vendors (e.g. Utimaco SecurityServer)
- AMQP / RabbitMQ adapter
- Record-and-replay: capture real traffic and convert to scenarios (the proxy
  tap's save-as-scenario is the first step of this)
