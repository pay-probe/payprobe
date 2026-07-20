# Observability

Every service exposes operational endpoints; Prometheus + Grafana are opt-in.

## Per-service endpoints

| Endpoint   | Purpose                                                              |
|------------|---------------------------------------------------------------------|
| `/health`  | Liveness — the process is up.                                        |
| `/ready`   | Readiness — dependencies (DB, Redis) answer; 503 when not.          |
| `/metrics` | Prometheus exposition (text format 0.0.4).                          |
| `/status`  | **Orchestrator only** — aggregates its own + dependencies' health.  |

`/health`, `/ready`, `/metrics`, `/status` are unauthenticated so probes and
scrapers reach them even when the auth gate is enforced.

## Key metrics

- `payprobe_http_requests_total{method,route,code}` and
  `payprobe_http_request_duration_seconds` — request rate, errors, latency
  (per service via the `service` scrape label).
- `payprobe_runs_inflight`, `payprobe_runs_total{status}` — scenario run load
  and outcomes.
- `payprobe_load_runs_inflight`, `payprobe_load_run_tps{run_id}`,
  `payprobe_load_run_errors{run_id}`,
  `payprobe_load_run_workers_reporting{run_id}` — live load-test throughput,
  error count, and fleet liveness. These answer "why is throughput low?" at a
  glance (e.g. workers_reporting < expected ⇒ fleet not fully up; errors rising
  with tps flat ⇒ the target is rejecting).

## Bring up Prometheus + Grafana

```bash
docker compose -f infra/docker/docker-compose.yml --profile observability up
```

- Prometheus → http://localhost:9090 (scrape config: `infra/prometheus/prometheus.yml`)
- Grafana → http://localhost:3000 (default `admin`/`admin`; set `GRAFANA_PASSWORD`).
  The "PayProbe — Load & Service Health" dashboard is auto-provisioned.

### Soak / leak detection

While a load run is active the orchestrator exports each reporting worker's
resident memory as `payprobe_load_worker_rss_bytes{run_id,worker_id}` (workers
ship their own RSS in every metric sample — no worker `/metrics` scrape needed).
Two dashboard panels trend it: **"Soak — worker RSS (leak watch)"** (absolute
RSS) and **"Soak — worker RSS growth rate"** (10m `deriv`, the leak signal). A
clean cyclic soak saw-tooths and the growth rate averages ~0; a monotonic climb
trips the **`WorkerMemoryLeak`** Prometheus alert
(`infra/prometheus/rules/payprobe-soak.rules.yml`, loaded via `rule_files`).

## Tracing (optional)

The orchestrator calls `init_tracing()` on startup. It is a **no-op** unless the
OpenTelemetry SDK is installed *and* `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so you
can turn on OTLP tracing to a collector (Tempo/Jaeger) without code changes:

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

Cross-service span propagation (orchestrator → worker → adapter) is the next
increment; the bootstrap seam is in `orchestrator/api/observability.py`.

## Not yet wired (roadmap)

- **Secrets via Vault/KMS** — secrets are env vars today (`AUTH_JWT_SECRET`,
  provider API keys). Mount them from a secret manager in production rather than
  committing to `.env`.
- **Distributed tracing spans** across the worker/adapter boundary.
- **Worker fleet `/metrics`** — load workers run headless; add an exporter so
  Prometheus scrapes them directly (scrape job stub already in `prometheus.yml`).
