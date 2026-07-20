<div align="center">

<img src="docs/assets/logo.svg" width="120" alt="PayProbe Logo" />

# PayProbe

**A digital twin of a payment network — source-available testing & simulation platform for payment systems**

[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-orange.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/pay-probe/payprobe?style=social)](https://github.com/pay-probe/payprobe)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Quick Start](#quick-start-docker) · [Documentation](docs/) · [Simulators](#simulators) · [Adapters](#adapters) · [Architecture](#architecture) · [Contributing](CONTRIBUTING.md)

</div>

---

> **Disclaimer**
>
> This project is an independent, source-available infrastructure testing and simulation framework.
> It is not affiliated with my employer and does not include employer code, confidential information,
> internal specifications, production data, customer data, credentials, configurations, or proprietary business logic.
> All development is performed outside working hours using personal equipment.

---

## What is PayProbe?

PayProbe models your acquirers, switches, issuers, HSMs and gateways as a wired
**network**, drives realistic traffic through it at production-like rates,
injects faults, and hands you an evidence-grade verdict — a resilience
certificate, a compliance report, a signed Go/No-Go.

It grew out of a scenario tester (send ISO 8583, assert on the response) into a
full platform: a visual scenario constructor, protocol simulators you can stand
up instead of the real thing, canvas-authored networks of long-lived listening
participants, distributed load generation, chaos storms, and run sign-off. You
can test *against* it, test *with* it, or stand up an entire mock payment
network inside it.

```
        driver ──► switch ──► issuer-1 / issuer-2 / issuer-3
                     │
                     └──► payShield HSM (simulated, real crypto)
```

*(That network is one command: `make showcase` — add `--certify` to load it,
storm it, and score a resilience certificate.)*

## Key Features

- **Visual scenario constructor** — drag-and-drop authoring of test scenarios and
  participant flows on one canvas model; import/export; "✨ Ask AI" drafts a flow
  from a plain-language description
- **Protocol simulators** — rules-driven ISO 8583 responder, VISA Base I
  (auth/reversal/clearing + CVV2/PVV/ARQC), Thales payShield 10K HSM (real
  crypto under a test LMK), CyberSource-style REST gateway, NATS/JetStream
  responders
- **Networks** — wire participants, simulators and traffic drivers on a canvas;
  start order is derived, ports are planned before anything binds, partial
  starts roll back; opt-in **fleet mode** places listeners across worker hosts
- **Distributed load** — steady/ramp/spike/soak profiles on Redis-coordinated
  workers (20K+ TPS target), card/terminal pools, DUKPT/ARQC/PVV generators,
  hot retune mid-run, run-vs-run comparison
- **Chaos & resilience certification** — per-simulator fault dial (latency,
  drops, malformed frames), timed storms (outage/brownout/flapping), and a
  graded resilience certificate scored from availability, absorption, recovery
  and latency bands
- **End-to-end observability** — network trace stitched across every hop by
  correlation id, live network map, "Chronoscope" time-travel replay, execution
  waterfalls, Prometheus metrics + Grafana dashboards
- **Reports & sign-off** — two-mode reports (improvement vs Go/No-Go) with
  explicit gates, provenance of what ran against what, and an immutable,
  tamper-evident sign-off snapshot; JUnit XML for CI
- **AI assistants & MCP** — a multi-turn config assistant with journalled,
  fully reversible writes (session-wide undo); an MCP server exposing the
  platform to any MCP client; an advisory ML insight service for failure
  categorization and outcome prediction
- **Security** — fail-closed JWT auth on every service, RBAC in the portal,
  secrets encrypted at rest and masked everywhere, sandboxed code steps

## Quick Start (Docker)

Run the **entire platform** in Docker — mock mode by default, so no payment
hardware, secrets, or local toolchain are required.

```bash
# 1. Clone the repository
git clone https://github.com/pay-probe/payprobe.git
cd payprobe

# 2. Build and start the whole stack
docker compose -f infra/docker/docker-compose.yml up --build

# 3. Open the portal
open http://localhost:8080
```

Or skip the build entirely and run the published Docker Hub images:

```bash
make up-hub        # docker compose -f infra/docker/docker-compose.hub.yml up -d
```

Then either start a mock run from **Run Monitor**, or build the full demo
acquiring network (driver → switch → 3 issuers + payShield HSM) in one command:

```bash
make showcase                      # stand it up
python scripts/showcase.py --certify   # …then load + chaos storm + certificate
```

See [docs/getting-started/showcase.md](docs/getting-started/showcase.md) for the tour.

### Services & ports

| Service | URL | Role |
|---|---|---|
| Portal (UI + nginx proxy) | http://localhost:8080 | Angular management UI |
| Scenario service | http://localhost:8000 | config registry (scenarios, connections, formats, networks, …) |
| Orchestrator | http://localhost:8100 | runs, listeners, networks, simulators, load, reports |
| MCP server | http://localhost:8200 | FastMCP proxy over the platform APIs |
| Auth service | http://localhost:8300 | JWT auth, users & roles |
| Assistant | http://localhost:8400 | standalone LLM-gateway config assistant |
| Insight service | http://localhost:8500 | advisory ML insights (read-only) |

The stack also brings up Postgres, Redis, a 3-node NATS JetStream cluster,
Prometheus and Grafana. nginx routes `/api/scenarios/*` → scenario-service and
`/api/orch/*` → orchestrator (REST + `ws`); the orchestrator runs the worker
engine in-process, with optional fleet workers for load and listener hosting.

**Configuration:** copy `infra/docker/.env.example` → `infra/docker/.env` and
edit (all values have mock defaults). Full details are in
[infra/docker/README.md](infra/docker/README.md) and the
[configuration reference](docs/operations/configuration.md).

## Simulators

Stand up a realistic downstream instead of the real thing — each simulator is
config-driven, chaos-capable, and usable standalone or as a node in a network:

| Simulator | Speaks | Highlights |
|---|---|---|
| TCP responder | ISO 8583 / header-echo | rules-driven request→response, format validation |
| VISA scheme | ISO 8583 (Base I) | auth, reversal, network mgmt, clearing; opt-in CVV2/PVV/ARQC verification |
| payShield 10K | payShield host commands | NC/A0/BU/CW/CY/CA/EC/M6/M8 with real crypto under a test LMK |
| CyberSource-style gateway | REST | decision ladder (accept/review/reject/error) |
| NATS responder | NATS / JetStream | subject-based rules, queue-group fan-out |
| TCP proxy / tap | any TCP | transparent MITM: capture (redacted), intercept, stub, save-as-scenario |

## Adapters

Outbound protocol support for scenario steps and load drivers:

| Adapter | Protocol | Status |
|---|---|---|
| `tcp_iso8583` | ISO 8583 over raw TCP (pluggable dialects) | ✅ Stable |
| `http` | REST / HTTPS | ✅ Stable |
| `grpc` | gRPC (descriptor / compile / reflection) | ✅ Stable |
| `nats` | NATS publish / request / JetStream | ✅ Stable |
| `hsm` | payShield host commands | ✅ Stable |
| `db_probe` | PostgreSQL / Oracle / MSSQL | ✅ Stable |
| `mock_*` | In-memory | ✅ All systems |

The adapter interface is protocol-agnostic — anything you can speak over a
socket can be wrapped. See [Writing an Adapter](docs/adapters/writing-an-adapter.md).

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                 Angular 22 Portal                     │
│   constructor · networks canvas · maps · run monitor  │
└──────────────────────────┬───────────────────────────┘
                           │ REST + WebSocket/SSE
┌──────────────────────────▼───────────────────────────┐
│                Backend services (FastAPI)             │
│  scenario-service · orchestrator · auth · assistant   │
│           mcp-server · insight-service                │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│           Worker engine (asyncio + uvloop)            │
│  scenarios · participant flows · simulators · load    │
│     + opt-in fleet: load workers & flow hosts         │
└──────────────────────────┬───────────────────────────┘
                           │ adapter calls
┌──────────────────────────▼───────────────────────────┐
│      Simulated or real targets: switch · issuer       │
│         HSM · gateway · broker · database …           │
└──────────────────────────────────────────────────────┘
```

The full architecture — **with the reasoning behind it, the invariants, and
the decision log** — lives in [docs/ATLAS.md](docs/ATLAS.md), with formal ADRs
in [docs/adr/](docs/adr/). AI assistants (and new humans) should start with
[CLAUDE.md](CLAUDE.md).

## Project Structure

```
payprobe/
├── packages/
│   ├── worker/              # Execution engine, adapters, simulators, fleet roles
│   ├── orchestrator/        # Runtime: runs, networks, simulators, load, reports (8100)
│   ├── scenario-service/    # Config registry + in-process assistant (8000)
│   ├── auth-service/        # JWT auth + users/roles (8300)
│   ├── payprobe-assistant/  # Standalone LLM-gateway assistant (8400)
│   ├── insight-service/     # Advisory ML insights (8500)
│   ├── mcp-server/          # FastMCP proxy over the platform (8200)
│   ├── payprobe_common/     # Shared: assistant tool layer, crypto (SecretBox)
│   ├── report_service/      # Shared report/gates/provenance library
│   └── portal/              # Angular 22 management UI
├── infra/                   # Docker compose, nginx, Prometheus, Grafana
├── examples/                # Example scenarios and environments (all CI-tested)
├── scripts/                 # Showcase, publishing, tooling
└── docs/                    # ATLAS, ADRs, guides, runbooks
```

## Running the tests

The whole Python suite (~1,275 tests across worker, orchestrator,
scenario-service, mcp-server, assistant and insight-service, plus the showcase
guard) runs offline —
fakes for providers, in-memory buses, `fakeredis` — from one command:

```bash
make test            # full suite (the CI gate; non-zero exit on any failure)
make test-worker     # just the worker engine/adapter/simulator tests
make test-cov        # full suite with coverage
make install         # worker runtime + dev/test deps
```

CI (GitHub Actions) runs every suite plus the portal build, a Playwright
golden-path smoke, and a docker-compose mock-integration run. `make test`
includes `test_example_scenarios.py`, which runs **every** bundled scenario in
`examples/scenarios/` end-to-end through the real engine (mock mode) — a broken
scenario can never ship silently. Test-environment rules (required deps,
per-package collection) are documented in [CLAUDE.md](CLAUDE.md).

### Adding a scenario

A scenario is a JSON document under `examples/scenarios/` (or authored in the
portal's visual editor and saved to the scenario service). Minimum shape:

```jsonc
{
  "id": "my_case",
  "name": "my_case",
  "test_class": "e2e",
  "steps": [
    { "id": "auth", "target": "tcp_iso8583", "action": "send_0200",
      "payload": { "amount": 10000, "pan": "4111111111111111" },
      "assertions": [ { "field": "response_code", "operator": "eq", "expected": "00" } ] }
  ],
  "edges": []                       // omit/empty = steps run top-to-bottom
}
```

- `target` is the adapter/connection the step talks to (e.g. `tcp_iso8583`,
  `hsm`, `http`), or a saved **Connection** name. To pin a step to a
  specific connection from the editor, use the step's *Connection* dropdown — it
  records the choice and the orchestrator points the step at it at run time.
- `action` is one of that target's catalog actions; `assertions` decide
  pass/fail. Drop the file in `examples/scenarios/` and `make test` will pick it
  up automatically (no test code to write). **Don't add a scenario you can't
  actually run** — if it can't reach a real system, run it in mock mode.

### Reading a report

Every run produces a report via the orchestrator:

- **Live/JSON:** `GET /runs/{id}` — per-scenario and per-step results
  (`status`, `duration_ms`, `request`, `response`, `assertions`, `error`) plus a
  rolled-up summary (`total / passed / failed / blocked`).
- **JUnit XML:** `GET /runs/{id}/junit` — for CI dashboards.
- **HTML:** `GET /runs/{id}/report` — a self-contained page; also viewable in the
  portal under **Runs → Run history → Full report**.

Beyond per-run reports: two-mode reports (improvement vs **Go/No-Go**) with
gates and provenance, immutable sign-off snapshots, scheduled regression runs
with trend lines, and resilience certificates at `/resilience`.

> **Secrets:** connection configs hold only host/port/protocol/framing — no
> credentials. Keep keys/PINs/passwords in the scoped **variables/secrets**
> mechanism (encrypted at rest, masked in every API, resolved at run time),
> never in a connection or committed scenario.

## Documentation

| Guide | Description |
|---|---|
| [Quick Start](docs/getting-started/quick-start.md) | Up and running in 10 minutes |
| [Showcase network](docs/getting-started/showcase.md) | The full demo network, one command |
| [ATLAS](docs/ATLAS.md) | Architecture with its reasoning + the honest roadmap |
| [ADRs](docs/adr/) | Fleet, proxy tap, report gates, networks, insights, NATS, playground, proxy TLS |
| [Architecture Overview](docs/architecture/overview.md) | How the system fits together |
| [Writing an Adapter](docs/adapters/writing-an-adapter.md) | Add support for a new system |
| [Using the Code Step](docs/scenarios/code-step.md) | Custom Python/TypeScript nodes |
| [Configuration Reference](docs/operations/configuration.md) | Every env var, with defaults |
| [Observability](docs/operations/observability.md) | Metrics, health, Grafana, tracing |
| [Load test runbook](docs/operations/load-test-runbook.md) | Running distributed load safely |
| [Participant flows, end to end](docs/participant-flow-end-to-end-guide.md) | Author long-lived listeners and wire them into networks |
| [Simulator references](docs/simulators/) | payShield 10K, HSM command reference, VISA scheme |

Each service also serves a live, try-it-out API reference at `/reference`
(Scalar over the OpenAPI spec) — linked from the portal's **Docs** page.

## Roadmap

The maintained roadmap — with the reasoning per item — is
[ATLAS §11](docs/ATLAS.md#11-roadmap--with-the-why). Headlines:

- [ ] Proxy tap TLS termination (ADR-0008, the deferred half of ADR-0002)
- [ ] Spec-exact VISA (Phase 2), then further schemes (Mastercard next)
- [ ] Playground portal-page verification pass (backend + page landed, ADR-0007)
- [ ] Consolidated canvas overlays for the network map surfaces

## Contributing

PayProbe welcomes contributions. The most valuable contributions are new adapters and simulators — if you work with a protocol or system not yet supported, your adapter helps the whole community.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Security

Found a security issue? Please report it privately — see [SECURITY.md](SECURITY.md).
Don't open a public issue for vulnerabilities.

## License

PolyForm Noncommercial License 1.0.0 — see [LICENSE](LICENSE).

This is a **source-available, noncommercial** license. You may use, modify, and
share the code for any noncommercial purpose (personal study, research, hobby
projects, and use by nonprofit, educational, or government organizations).
**Commercial use is not permitted** under this license. Note that this means the
project is *source-available*, not "open source" in the OSI sense — the standard
open-source definition does not allow restrictions on commercial use. For
commercial licensing, contact the author.

This is an independent project (see the disclaimer above). It is not affiliated with,
endorsed by, or built from the proprietary materials of any employer.

PayProbe was created by [David Sakhelashvili](https://github.com/pay-probe).
