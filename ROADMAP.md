# PayProbe Roadmap

## v0.1 — Foundation _(current)_

Core platform functional with mock adapters.

- [x] Worker Engine (asyncio + uvloop)
- [x] Three-phase execution pipeline
- [x] All adapter base classes
- [x] Mock adapters for all system types
- [x] Scenario JSON schema
- [x] PostgreSQL schema
- [x] FastAPI backend services (Scenario, Orchestrator, Report, Auth)
- [x] Angular portal (Dashboard, Run Monitor, Reports)
- [x] Docker Compose (mock + production templates)
- [x] Example scenarios (chip purchase, NFC, refund, reversal)

## v0.2 — Real Adapters

Production-grade adapter implementations.

- [ ] xPay adapter (REST)
- [ ] HSM adapter (PKCS#11 — Thales payShield)
- [ ] Simulated terminal adapter (software EMV)
- [ ] ISO 8583 switch adapter
- [ ] Core Banking adapter (REST)
- [ ] DB Probe adapter (PostgreSQL + Oracle)
- [ ] Physical terminal adapter (serial/TCP)

## v0.3 — Test Constructor

Visual workflow editor in the Angular portal.

- [ ] ngx-graph canvas integration
- [ ] Step palette (all node types)
- [ ] Per-node configuration panel
- [ ] Inter-step variable references (`${step_001.response.rrn}`)
- [ ] Scenario import/export (JSON + YAML)
- [ ] Scenario versioning

## v0.4 — Observability

Deep visibility into run execution.

- [ ] Per-step request/response capture (full payloads)
- [ ] Timing waterfall view in portal
- [ ] Log correlation (attach system logs to run by timestamp)
- [ ] Grafana dashboard template
- [ ] Prometheus metrics endpoint on worker

## v0.5 — CI/CD Integration

First-class pipeline support.

- [ ] GitHub Actions example workflow
- [ ] GitLab CI example pipeline
- [ ] Jenkins pipeline example
- [ ] CLI tool (`payprobe run --env staging --suite regression`)
- [ ] Webhook on run completion (Slack, Teams, email)

## v1.0 — Production Ready

Hardened for production use.

- [ ] Role-based access control (admin, operator, viewer)
- [ ] Audit log for all portal actions
- [ ] Scenario permission model (owner, team, public)
- [ ] Full API documentation (OpenAPI)
- [ ] Helm chart for Kubernetes deployment
- [ ] Python SDK for programmatic scenario creation

## Future Ideas _(community input welcome)_

- AMQP / RabbitMQ adapter
- Visa/Mastercard scheme simulator adapter
- HSM adapter for Utimaco SecurityServer
- ISO 20022 message support
- Android POS terminal adapter (via ADB)
- Record-and-replay: capture real traffic and convert to scenarios
