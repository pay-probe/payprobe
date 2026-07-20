"""Metrics, readiness and status endpoints."""
import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def test_metrics_endpoint_exposes_prometheus(client):
    # generate some traffic first
    client.get("/runs")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "payprobe_http_requests_total" in body
    assert "# TYPE payprobe_runs_inflight gauge" in body


def test_metrics_is_public_even_when_gated(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("API_TOKEN", "secret")
    assert client.get("/metrics").status_code == 200
    assert client.get("/ready").status_code in (200, 503)


def test_ready_reports_checks(client):
    r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "ready" in body and "checks" in body
    assert body["checks"].get("run_store") == "ok"


def test_system_reports_runtime_posture(client):
    r = client.get("/system")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"environment", "security", "runtime", "observability"}
    assert "auth_enforced" in body["security"]
    assert "code_sandbox_mode" in body["security"]
    assert "run_store_durable" in body["runtime"]
    assert body["observability"]["metrics"] == "/metrics"


def test_status_aggregates_services(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "orchestrator" in body["services"]
    assert body["services"]["orchestrator"]["status"] == "ok"
    # dependencies are unreachable in the test env → reported, not raised
    assert "scenario-service" in body["services"]
    # Redis is always reported: "disabled" when REDIS_URL is unset (single-node),
    # otherwise "ok"/"down" from a server-side PING. "disabled" never degrades.
    assert "redis" in body["services"]
    assert body["services"]["redis"]["status"] in ("ok", "down", "disabled")
    # MCP server is reported too: "disabled" when MCP_API_URL is unset (stdio /
    # not deployed), otherwise "ok"/"down". "disabled" never degrades overall.
    assert "mcp-server" in body["services"]
    assert body["services"]["mcp-server"]["status"] in ("ok", "down", "disabled")
    # Config assistant is reported too: "disabled" when ASSIST_API_URL is unset
    # (not deployed), otherwise "ok"/"down". "disabled" never degrades overall.
    assert "assistant" in body["services"]
    assert body["services"]["assistant"]["status"] in ("ok", "down", "disabled")
