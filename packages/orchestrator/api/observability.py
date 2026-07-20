"""Service observability: Prometheus metrics + HTTP instrumentation.

Defines the orchestrator's metric series and a middleware that records request
rate / latency by route, plus run and load-run gauges the rest of the app
updates. Exposed at ``GET /metrics`` (see ``main.py``).

Also provides an optional OpenTelemetry tracing bootstrap: a no-op unless both
the OTel SDK is installed and ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so tracing
can be switched on in production without code changes or a hard dependency.
"""
from __future__ import annotations

import os
import time

from worker.engine.metrics import Counter, Gauge, Histogram

# -- HTTP -----------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "payprobe_http_requests_total",
    "HTTP requests handled, by method, route and status code.",
    ["method", "route", "code"],
)
HTTP_LATENCY = Histogram(
    "payprobe_http_request_duration_seconds",
    "HTTP request latency in seconds, by route.",
    ["route"],
)
HTTP_INFLIGHT = Gauge(
    "payprobe_http_inflight_requests",
    "In-flight HTTP requests.",
)

# -- runs -----------------------------------------------------------------------

RUNS_INFLIGHT = Gauge(
    "payprobe_runs_inflight",
    "Scenario runs currently executing on this replica.",
)
RUNS_TOTAL = Counter(
    "payprobe_runs_total",
    "Scenario runs finished, by terminal status.",
    ["status"],
)

# -- load runs ------------------------------------------------------------------

LOAD_RUNS_INFLIGHT = Gauge(
    "payprobe_load_runs_inflight",
    "Load runs currently executing on this replica.",
)
LOAD_RUN_TPS = Gauge(
    "payprobe_load_run_tps",
    "Latest achieved throughput (transactions/sec) per active load run.",
    ["run_id"],
)
LOAD_RUN_ERRORS = Gauge(
    "payprobe_load_run_errors",
    "Cumulative errors per active load run.",
    ["run_id"],
)
LOAD_RUN_WORKERS = Gauge(
    "payprobe_load_run_workers_reporting",
    "Workers reporting samples per active load run (fleet liveness).",
    ["run_id"],
)
LOAD_WORKER_RSS = Gauge(
    "payprobe_load_worker_rss_bytes",
    "Resident memory (bytes) of each reporting load worker — trended across "
    "soak cycles for leak detection.",
    ["run_id", "worker_id"],
)


# -- host/responder simulators -------------------------------------------------

SIMULATOR_UP = Gauge(
    "payprobe_simulator_up",
    "1 while a saved/ad-hoc simulator is running (absent once stopped).",
    ["sim_id", "label"],
)
SIMULATOR_MESSAGES = Gauge(
    "payprobe_simulator_messages_received",
    "Messages received by a running simulator since its last stats reset.",
    ["sim_id", "label"],
)
SIMULATOR_RPS = Gauge(
    "payprobe_simulator_rps",
    "Instantaneous requests/sec handled by a running simulator.",
    ["sim_id", "label"],
)
SIMULATOR_FAULTS = Gauge(
    "payprobe_simulator_faults",
    "Faults injected (chaos) by a running simulator since its last reset.",
    ["sim_id", "label"],
)


def _route_template(request) -> str:
    """The matched route path (``/runs/{run_id}``) rather than the raw URL, so
    run ids don't explode metric cardinality."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "other"


async def metrics_middleware(request, call_next):
    method = request.method
    HTTP_INFLIGHT.inc()
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = _route_template(request)
        HTTP_INFLIGHT.dec()
        HTTP_LATENCY.observe(time.perf_counter() - start, route=route)
        HTTP_REQUESTS.inc(method=method, route=route, code=str(status))


# -- optional OpenTelemetry tracing --------------------------------------------

def init_tracing(service_name: str = "payprobe-orchestrator") -> bool:
    """Wire OTLP tracing if configured + the SDK is present. Returns True if on.

    Activated only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; otherwise a
    no-op. Import failures degrade to no-op so the SDK stays an optional extra.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True
