# Quick Start

Get PayProbe running locally in under 10 minutes using mock adapters.
No real payment hardware or systems required.

## Prerequisites

- Docker + Docker Compose
- Git

## Step 1 — Clone

```bash
git clone https://github.com/pay-probe/payprobe.git
cd payprobe
```

## Step 2 — Start

```bash
docker compose -f infra/docker/docker-compose.mock.yml up
```

Wait for all services to report healthy (about 15 seconds).

## Step 3 — Open the Portal

Navigate to [http://localhost](http://localhost)

Default credentials: `admin@payprobe.dev` / `payprobe`

## Step 4 — Run Your First Test

1. In the portal, click **Environments** → select **mock**
2. Click **Scenarios** → you will see the bundled example scenarios
3. Click **New Run** → select all example scenarios → **Start**
4. Watch the live run monitor — all three phases should pass

## Step 5 — Explore the Report

Click **Reports** → select the run you just completed.
You will see the full three-phase breakdown, per-step results, and timing.

## Next Steps

- [Write your first scenario](../scenarios/schema-reference.md)
- [Connect a real system](../deployment/connecting-real-systems.md)
- [Write a custom adapter](../adapters/writing-an-adapter.md)
