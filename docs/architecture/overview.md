# Architecture Overview

## Layers

```
┌─────────────────────────────────────────┐
│         Angular Portal (~30 users)       │  packages/portal
└──────────────────┬──────────────────────┘
                   │ REST + WebSocket
┌──────────────────▼──────────────────────┐
│             Nginx (reverse proxy)        │  infra/nginx
└──┬──────┬──────┬──────┬─────────────────┘
   │      │      │      │
  auth  scen  orch  report   packages/auth-service
                              packages/scenario-service
                              packages/orchestrator
                              packages/report-service
┌──────────────────▼──────────────────────┐
│         PostgreSQL + Redis               │  infra/postgres, infra/redis
└──────────────────┬──────────────────────┘
                   │ run jobs
┌──────────────────▼──────────────────────┐
│     Worker Engine (asyncio + uvloop)     │  packages/worker
│     AdapterRegistry → adapters           │
└──────────────────┬──────────────────────┘
                   │ helper calls
┌──────────────────▼──────────────────────┐
│           Helper Services                │  packages/helpers/*
└──────────────────┬──────────────────────┘
                   │ real protocol calls
┌──────────────────▼──────────────────────┐
│           Target Systems                 │
│  xPay · HSM · Core Banking · Switch      │
└─────────────────────────────────────────┘
```

## Key Design Decisions

### Why asyncio for the worker?
Payment system calls are IO-bound — the bottleneck is network round-trip time,
not CPU. A single asyncio event loop with uvloop handles 50K concurrent 
connections efficiently without threads or multiple processes.

### Why PostgreSQL + Redis (not Kafka)?
The portal serves ~30 users. Redis pub/sub handles the WebSocket fan-out
with no operational overhead. Kafka would be engineering complexity with
no benefit at this scale.

### Why the adapter pattern?
Each payment system speaks a different protocol (REST, ISO 8583, PKCS#11).
The adapter pattern isolates protocol complexity behind a uniform interface,
making it possible to add new systems without touching the worker engine.

### Why Apache 2.0?
Maximum adoption. Companies can use PayProbe internally without any obligation
to open-source their adapter configs or scenario libraries.
