# Observability

> **Reference / how-to** — what each service exposes for monitoring, the key
> metrics, and how to bring up Prometheus + Grafana. Operator-focused bring-up
> notes also live next to the stack in [`infra/OBSERVABILITY.md`](../../infra/OBSERVABILITY.md).

## Endpoints

Every service exposes:

| Endpoint | Purpose |
|---|---|
| `/health` | Liveness — the process is up. |
| `/ready` | Readiness — dependencies (DB, Redis) answer; returns 503 when not. |
| `/metrics` | Prometheus exposition (text format 0.0.4). |

The orchestrator additionally exposes:

| Endpoint | Purpose |
|---|---|
| `/status` | Aggregated health of the orchestrator + its dependencies, in one call. |
| `/system` | Non-secret runtime posture (auth, sandbox, durability, tracing) — drives **Settings → System** in the portal. |

`/health`, `/ready`, `/metrics`, and `/status` are unauthenticated so probes and
scrapers reach them even when the auth gate is enforced.

## Key metrics

| Metric | Meaning |
|---|---|
| `payprobe_http_requests_total{method,route,code}` | Request rate / errors per route. |
| `payprobe_http_request_duration_seconds` (histogram) | Latency; use `histogram_quantile` for p95/p99. |
| `payprobe_runs_inflight`, `payprobe_runs_total{status}` | Scenario-run concurrency and outcomes. |
| `payprobe_load_runs_inflight` | Active load tests. |
| `payprobe_load_run_tps{run_id}` | Live achieved throughput per load run. |
| `payprobe_load_run_errors{run_id}` | Cumulative errors per load run. |
| `payprobe_load_run_workers_reporting{run_id}` | Workers reporting samples — fleet liveness. |

These directly answer **"why is throughput low?"**: `workers_reporting` below the
configured worker count means the fleet isn't fully up; `errors` climbing while
`tps` stays flat means the target is rejecting traffic.

## Bring up Prometheus + Grafana

```bash
docker compose -f infra/docker/docker-compose.yml --profile observability up
```

- Prometheus → http://localhost:9090 (config: `infra/prometheus/prometheus.yml`)
- Grafana → http://localhost:3000 (`admin`/`admin` by default; set `GRAFANA_PASSWORD`).
  The **PayProbe — Load & Service Health** dashboard is auto-provisioned.

## Tracing (optional)

The orchestrator calls `init_tracing()` on startup — a no-op unless the OTel SDK
is installed *and* `OTEL_EXPORTER_OTLP_ENDPOINT` is set:

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

Cross-service span propagation (orchestrator → worker → adapter) is the next
increment; the bootstrap seam is in `orchestrator/api/observability.py`.

## Related

- [Configuration reference](./configuration.md) — all the env knobs.
- Portal **Settings → System** — the live read-only view of this posture.
