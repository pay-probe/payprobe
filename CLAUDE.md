# CLAUDE.md — read this first

Guidance for AI assistants (and new humans) working in this repo. This file is
deliberately short; the *reasoning* behind the architecture — why things are
shaped the way they are, what was decided and rejected, and the honest roadmap
— lives in **[docs/ATLAS.md](docs/ATLAS.md)**. Read that before proposing any
structural change; most "obvious improvements" here were considered and have a
recorded reason for not existing.

## What this is

PayProbe is a payment-systems testing platform: a visual scenario constructor,
protocol simulators (ISO 8583, payShield HSM, VISA Base I, CyberSource REST,
NATS messaging, and the Stripe / Adyen / PayPal PSP simulators of ADR-0009),
canvas-authored **networks** of listening participants,
distributed load
generation, chaos/resilience certification, and Go/No-Go run sign-off. It is
becoming a *digital twin of a payment network*. Source-available under
PolyForm Noncommercial — it is positioned generically, not as any employer's
product.

## Monorepo map

| Package | Role | Port |
|---|---|---|
| `packages/scenario-service` | Config registry (scenarios, connections, formats, networks, …) + in-process assistant | 8000 |
| `packages/orchestrator` | Runtime: runs, listeners, networks, simulators, load, reports | 8100 |
| `packages/worker` | Execution engine + adapters + simulators; also `load_worker` / `flow_host` fleet roles | — |
| `packages/mcp-server` | FastMCP proxy over both services (HTTP + stdio) | 8200 |
| `packages/auth-service` | JWT auth + users/roles | 8300 |
| `packages/payprobe-assistant` | Standalone LLM-gateway assistant (REST-backed) | 8400 |
| `packages/insight-service` | Advisory ML insights: failure categorization + explanation + outcome prediction (ADR-0005; read-only, advise-only) | 8500 |
| `packages/payprobe_common` | Shared: `agent_toolkit` (assistant tool layer), `crypto` (SecretBox) | — |
| `packages/report_service` | Shared report/gates/provenance library (orchestrator imports it) | — |
| `packages/portal` | Angular 22 UI (standalone components, signals, `pp-*` design system) | 4200 |

Infra (compose, nginx, grafana, redis) is under `infra/`. Historical
build specs and working notes live in [docs/history/](docs/history/); ADRs are
in `docs/adr/`.

## Running tests — the rules

- Always from `packages/` with `PYTHONPATH=packages` (or use the Makefile:
  `make test`, `make test-<pkg>`). Sibling packages import each other
  (`worker`, `payprobe_common`, `report_service`) by path, not installation.
- Python deps that are NOT installed by default:
  `structlog httpx iso8583 "mcp[cli]" aiohttp pycryptodome pytest pytest-asyncio`
  (+ `fakeredis` for the Redis-path tests; `nats-py` for the ADR-0006 NATS
  tests). Missing `pycryptodome` fails every EMV/ARQC/payShield/VISA crypto test
  with ModuleNotFoundError — that is **environmental, not a regression**. Same
  for missing `aiohttp` (test_cybersource_sim collection) and missing `nats-py`
  (the NATS adapter/responder/flow suites — they skip, not fail, on absence).
- If cross-package collection ever produces import collisions, run suites **per
  package** — that is the known-safe mode. `test_payshield_e2e` can flake when
  run with the whole worker suite (shared simulator state); it passes alone.
- The MCP registry has a **generated portal catalog**: after touching
  `mcp_server/registry.py` or `prompts.py`, run
  `python packages/mcp-server/scripts/gen_catalog.py` or
  `test_catalog_generated` fails by design.
- The Angular portal often cannot be built in AI sandboxes (node_modules
  contains absolute symlinks from the machine that installed it). Portal
  changes require a real `npm run build` on the host — say so explicitly
  rather than claiming verification.

## Invariants — do not break these

1. **Edge semantics are per graph level.** In scenarios and participant flows
   an edge means *"then"* (control flow, `source_port` matters). In networks
   an edge means *"sends traffic to"* (wiring; start order = topological sort,
   callees first). Never blur them; validators enforce the node-kind
   partitions of `NodeKind` (scenario-only `call`/`init`, flow-only
   `trigger`/`reply`/`state`/`relay`, network-only
   `participant`/`scenario`/`simulator`/`group`).
2. **Every assistant write is reversible from data.** The change journal
   records `before` state; restore is a pure function of
   (resource, key, before) — JSON-serializable, replayable by any replica.
   Never register a write tool without journalling + a `restore_one` branch.
3. **The assistant tool layer lives ONCE** in
   `payprobe_common/agent_toolkit.py`. A new tool/domain = one handler there +
   one primitive op in each backend (`StoresBackend` in scenario-service,
   `RestBackend` in payprobe-assistant). Never re-grow per-service copies —
   that drift caused real bugs twice.
4. **Port planning (or subject claim) happens before anything binds.** One
   instance per (host, port), collisions are a 409 *before* any listener
   starts, partial starts roll back completely (across the fleet too). Fleet
   endpoint discovery matches by planned port — fixed listen ports are required
   for cross-host wiring. **NATS participants are port-less** (ADR-0006):
   broker-mediated, they claim `(server, subject, queue_group)` instead of a
   port. Same subject + same queue group across instances is legal fan-out;
   the same subject with **no** queue group claimed twice is the 409. NATS
   downstreams reach callees by subject via the broker, so port-less instances
   are simply skipped by the fleet port→host wave index.
5. **Stop-ownership.** A network run stops only what it started: simulators it
   found already running are never torn down (`simulators` = started-by-run
   vs `simulator_ids` = health set). Readiness (`_run_health`) counts *all*
   referenced pieces.
6. **Guardrails live in the tool layer, not the prompt.** Builtins can't be
   deleted; referenced connections can't be dropped; the model proposes, the
   registry is the bouncer.
7. **Connections have a single `port`** for both directions (`listen_port` is
   legacy, folded on read, self-heals). Direction is `mode`. Per-environment
   values come from the override matrix — the connection is the shape, the
   matrix is the values.
8. **Secrets never round-trip in plaintext.** SecretBox encrypts at rest,
   APIs mask, the vault page never reveals. Don't "fix" masking.

## Operational gotchas

- **Trace capture is OFF by default** (buffer churn under load). Empty
  Network Trace ⇒ resume capture first (portal button, `set_trace_capture`
  MCP tool, or `POST /participants/capture`).
- `requires_topology` on runs is a **historical field name** — it takes a
  network(-flow) id. Topologies were absorbed by networks (ADR-0004);
  migration reused ids on purpose.
- Fleet hosting is **opt-in**: `NETWORK_FLEET=1` *and* ≥1 `worker.flow_host`
  heartbeating on Redis; otherwise networks start in-process (dev/CI default).
- The in-process assistant reaches the orchestrator via `RUN_API_URL`
  (compose sets `http://orchestrator:8100`) for its runtime-read tools.
- scenario-service and payprobe-assistant Docker images build from the
  **`packages/` context** (they ship `payprobe_common`); the orchestrator
  builds from repo root. Don't "simplify" contexts back.
- File-backed registries live beside the SQLite db (`:memory:` in tests);
  `ORCH_RUNTIME_FILE` persists the running network so a restart re-launches it.
- The dashboard has TWO health panels: "Endpoints" (client-side probes from
  RuntimeConfig) and "System health" (orchestrator `/status` services map).
  A new service must be added to both, plus Settings→Endpoints.
- **Provider connections marked `external: true`** (the real Stripe/Adyen/PayPal
  sandboxes a provider pack installs) are **refused by load runs by design**
  (ADR-0009 — load-testing a provider's sandbox violates its ToS). Don't "fix"
  the 400; point the load at the provider simulator (saved-simulator `kind`
  `stripe`/`adyen`/`paypal`). The `/diagnostics` **providers layer** reports
  each one as credential-set / token-obtainable / reachable. Provider specifics
  live in the pack's *data*, never in a per-provider adapter class — the `http`
  adapter (+ oauth2/form) and the generic `mcp` adapter cover them. See
  [docs/authoring-a-provider-pack.md](docs/authoring-a-provider-pack.md).

## Where knowledge lives

- `docs/ATLAS.md` — architecture with reasoning + roadmap (the companion to
  this file).
- `docs/adr/` — nine ADRs; 0001 (fleet), 0004 (networks) and 0006 (NATS)
  are fully implemented, 0002 (proxy tap/intercept/stub) through stage 2 with
  only TLS deferred (now specced as 0008, proposed), 0005 (insight service)
  built as advise-only, 0007 (playground) backend built, portal page written
  2026-07-16 but still owed a host build + click-through, 0009 (payment-provider
  integration — PSP simulators/packs + generic `mcp` client adapter + signed
  webhook emission) implemented, portal presets owed a host build; statuses in
  the files are kept truthful.
- `.claude/skills/payprobe-run-and-operate` and `payprobe-config-and-flags`
  — operator-grade API/env-flag reference, kept current.
- `docs/history/` — finished build specs, plans and working notes (accurate at
  the time of build; the code has moved past some of them).
- `docs/history/project-review.md` — the hardening review and what it changed.
