<div align="center">

<img src="docs/assets/logo.svg" width="120" alt="PayProbe Logo" />

# PayProbe

**Open-source regression testing platform for payment terminal ecosystems**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/pay-probe/payprobe?style=social)](https://github.com/pay-probe/payprobe)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Discord](https://img.shields.io/badge/Discord-Join-7289da)](https://discord.gg/payprobe)

[Quick Start](#quick-start) · [Documentation](docs/) · [Adapters](#adapters) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

</div>

---

## What is PayProbe?

PayProbe is a complete regression testing platform for payment terminal ecosystems. It orchestrates automated test runs across all layers of a payment stack — from physical or simulated POS terminals through a payment server (like xPay) to downstream systems including HSM, Core Banking, Switch, and Acquirer Host.

**It solves a real problem:** payment systems are deeply interconnected, and a change in one component can silently break another. PayProbe runs structured three-phase test suites after every deployment and tells you exactly which component or integration pair regressed — not just "something failed."

```
Terminal ──► Payment Server ──► Core Banking
                    │
                    ├──► HSM / Crypto
                    ├──► Payment Switch
                    └──► Acquirer Host
```

## Key Features

- **Three-phase execution** — component health checks → integration pairs → full end-to-end flows
- **50K concurrent connections** — async worker engine built on Python asyncio + uvloop
- **10,000 operations/sec** — sustained throughput against real payment infrastructure
- **Visual test constructor** — Angular drag-and-drop workflow editor, no code required
- **Baseline diffing** — every run compared against a pinned known-good snapshot
- **Adapter framework** — plug in any payment system via a clean adapter interface
- **CI/CD ready** — JUnit XML output, REST API trigger, Docker-native deployment
- **Mock mode** — run the full platform locally without real payment hardware

## Quick Start

```bash
# Clone the repository
git clone https://github.com/pay-probe/payprobe.git
cd payprobe

# Start with mock adapters (no real hardware needed)
docker compose -f infra/docker/docker-compose.mock.yml up

# Open the portal
open http://localhost:4200
```

The mock environment includes simulated versions of all adapters. You can run the included example scenarios immediately.

## Adapters

PayProbe ships with adapters for common payment system components:

| Adapter | Protocol | Status |
|---|---|---|
| `xpay` | REST / HTTPS | ✅ Stable |
| `hsm` | PKCS#11 | ✅ Stable |
| `terminal_sim` | EMV Simulator | ✅ Stable |
| `terminal_physical` | Serial / TCP | ✅ Stable |
| `core_banking` | REST / SOAP | ✅ Stable |
| `switch` | ISO 8583 | ✅ Stable |
| `acquirer` | ISO 8583 / REST | ✅ Stable |
| `db_probe` | PostgreSQL / Oracle / MSSQL | ✅ Stable |
| `mock_*` | In-memory | ✅ All systems |

**Adding a new adapter takes ~2 hours.** See [Writing an Adapter](docs/adapters/writing-an-adapter.md).

## Architecture

```
┌─────────────────────────────────────────┐
│         Angular Portal (~30 users)       │
└──────────────────┬──────────────────────┘
                   │ REST + WebSocket
┌──────────────────▼──────────────────────┐
│         Backend Services (FastAPI)       │
│  Scenario · Orchestrator · Report · Auth │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│     Worker Engine (asyncio + uvloop)     │
│     50K connections · 10K ops/sec        │
└──────────────────┬──────────────────────┘
                   │ adapter calls
┌──────────────────▼──────────────────────┐
│           Target Systems                 │
│  Payment Server · HSM · Core Bank · ...  │
└─────────────────────────────────────────┘
```

Full architecture documentation: [docs/architecture/](docs/architecture/)

## Project Structure

```
payprobe/
├── packages/
│   ├── worker/          # Async test execution engine
│   ├── orchestrator/    # Run lifecycle management (FastAPI)
│   ├── scenario-service/# Scenario CRUD (FastAPI)
│   ├── report-service/  # Report generation (FastAPI)
│   ├── auth-service/    # Authentication (FastAPI)
│   ├── portal/          # Angular management UI
│   └── helpers/         # System probe services
├── infra/               # Docker, Nginx, DB configs
├── examples/            # Example scenarios and environments
└── docs/                # Full documentation
```

## Documentation

| Guide | Description |
|---|---|
| [Quick Start](docs/getting-started/quick-start.md) | Up and running in 10 minutes |
| [Architecture Overview](docs/architecture/overview.md) | How the system fits together |
| [Writing an Adapter](docs/adapters/writing-an-adapter.md) | Add support for a new system |
| [Scenario Reference](docs/scenarios/schema-reference.md) | Full JSON scenario schema |
| [Deployment Guide](docs/deployment/production.md) | Production deployment |
| [Configuration Reference](docs/configuration/environments.md) | All config options |

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan. Near-term:

- [ ] Grafana dashboard integration
- [ ] Scenario versioning and diff
- [ ] AMQP / RabbitMQ adapter
- [ ] Kubernetes Helm chart
- [ ] Python SDK for programmatic scenario creation

## Contributing

PayProbe welcomes contributions. The most valuable contributions are new adapters — if you work with a payment system not yet supported, your adapter helps the whole community.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

PayProbe was created by [David Sakhelashvili](https://github.com/pay-probe) and is used in production at Georgian Card JSC.
