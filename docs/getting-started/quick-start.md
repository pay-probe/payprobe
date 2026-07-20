# Quick Start

Get PayProbe running locally in under 10 minutes. The default mode is **mock**
— in-memory adapters, no payment hardware, secrets, or local toolchain
required.

## Prerequisites

- Docker + Docker Compose
- Git

## Step 1 — Clone

```bash
git clone https://github.com/pay-probe/payprobe.git
cd payprobe
```

## Step 2 — Start the stack

```bash
docker compose -f infra/docker/docker-compose.yml up --build
```

Or skip the build and run the published Docker Hub images:

```bash
make up-hub        # docker compose -f infra/docker/docker-compose.hub.yml up -d
```

Wait for the services to report healthy (the first build takes a few minutes;
subsequent starts are fast).

## Step 3 — Open the portal

Navigate to [http://localhost:8080](http://localhost:8080).

If the portal asks you to sign in, the local dev bootstrap account is
`admin` / `admin` (see the
[configuration reference](../operations/configuration.md) — production
deployments seed no default user).

## Step 4 — Run your first test

1. Open **Run Monitor** and click **Start mock run** — the bundled example
   scenarios execute against the in-memory mock adapters, and you'll see steps
   stream in live.
2. Open **Scenarios** to see the same examples in the visual constructor —
   every one of them also runs in CI, so what you're looking at is tested.

## Step 5 — Explore the report

Open **Runs → Run history** and select the run you just completed. You'll see
per-step results, assertions, timing, and the full HTML report (also available
as JUnit XML for CI at `GET /runs/{id}/junit`).

## Step 6 — Stand up a whole network (optional)

The showcase builds a complete simulated acquiring network — driver → switch →
3 issuers + a payShield HSM — in one command:

```bash
make showcase
```

See [the showcase tour](showcase.md) — including `--certify`, which loads the
network, storms it with chaos, and scores a resilience certificate.

## Next steps

- [The showcase network](showcase.md) — the full demo, one command
- Add a scenario — see ["Adding a scenario" in the README](../../README.md#adding-a-scenario)
- [Run distributed load safely](../operations/load-test-runbook.md)
- [Write a custom adapter](../adapters/writing-an-adapter.md)
- [Configuration reference](../operations/configuration.md) — every env var
