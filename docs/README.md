# PayProbe documentation

> **New here (human or AI)?** Start with the repo-root [CLAUDE.md](../CLAUDE.md)
> (rules + gotchas) and **[ATLAS.md](ATLAS.md)** — the architecture with its
> reasoning, the invariants, and the roadmap.

These docs follow the **[Diátaxis](https://diataxis.fr/)** framework — organized
by what you're trying to do, not by how the code is structured. Four modes:

| Mode | When you're… | Where |
|---|---|---|
| **Tutorial** | learning by doing, start to finish | [Quick start](getting-started/quick-start.md) · [Showcase network](getting-started/showcase.md) · in‑app **Docs** page |
| **How‑to guide** | getting one specific job done | [Writing an adapter](adapters/writing-an-adapter.md) · [gRPC adapter](adapters/grpc.md) · [Code step](scenarios/code-step.md) · [Participant flows end to end](participant-flow-end-to-end-guide.md) · [Load test runbook](operations/load-test-runbook.md) |
| **Reference** | looking a fact up | [Configuration](operations/configuration.md) · [payShield 10K simulator](simulators/payshield-10k.md) · [payShield HSM commands](simulators/payshield-hsm-reference.md) · [VISA scheme simulator](simulators/visa-scheme.md) · **interactive API reference** (below); scenario shape: ["Adding a scenario" in the README](../README.md#adding-a-scenario) |
| **Explanation** | understanding the why | [Architecture overview](architecture/overview.md) · [Live run streaming](architecture/streaming.md) · [Insight service](architecture/insight-service.md) · [ADRs](adr/) |

## Operating PayProbe

Deploying or running the platform? See the **operations** guides:

- [Configuration reference](operations/configuration.md) — every environment
  variable, what it does, and its default. The portal's **Settings → System**
  panel shows the resulting posture live.
- [Observability](operations/observability.md) — `/metrics`, `/ready`,
  `/status`, the Grafana dashboard, and optional tracing.
- [Load test runbook](operations/load-test-runbook.md) — configuring and
  running distributed load safely.

## Interactive API reference

Each service serves a live, try-it-out API reference (powered by
[Scalar](https://github.com/scalar/scalar), fed by the service's OpenAPI spec):

| Service | Interactive reference | Also |
|---|---|---|
| Scenario Service | `http://localhost:8000/reference` | `/docs` (Swagger), `/redoc` |
| Orchestrator | `http://localhost:8100/reference` | `/docs`, `/redoc` |

Open them from the portal's **Docs** page, or directly. The reference lets you
send real requests and generates client code in several languages — no separate
docs build, it always matches the running API.

## Executable documentation

Examples are tested, so the docs can't silently drift:

- Every scenario in [`examples/scenarios/`](../examples/scenarios) runs
  end-to-end in `make test` (`test_example_scenarios.py`) and fails the build if
  it errors out.
- Example environments in [`examples/environments/`](../examples/environments)
  are validated by the adapter-registry tests.

## Decision record & history

- [ATLAS.md](ATLAS.md) — the architecture with its reasoning + the honest
  roadmap (the handover document).
- [adr/](adr/) — eight ADRs; statuses in the files are kept truthful.
- [history/](history/) — finished build specs, migration plans, and working
  notes, kept as the historical record of how each subsystem landed.

## Contributing to the docs (docs-as-code)

Docs live in this repo and ship with the code they describe — change them in the
same pull request, reviewed together. Add a new guide under the matching
Diátaxis folder (`getting-started/`, `adapters/`, `scenarios/`,
`architecture/`, …) and link it from the table above.
