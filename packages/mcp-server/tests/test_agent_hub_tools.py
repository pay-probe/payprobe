"""ADR-0010 agent-hub tools: endpoints, methods, payloads (HTTP mocked), and the
service-principal claim agent-hub's gate needs."""
import pytest

from mcp_server import registry, tools


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def fake_request(method, url, body=None, raw=False):
        seen.append((method, url, body))
        return {"ok": True}

    monkeypatch.setattr(tools, "_request", fake_request)
    return seen


def test_agent_reads_hit_agent_hub(calls):
    tools.list_agents()
    assert calls[-1][:2] == ("GET", f"{tools.AGENT_HUB_API}/agents")
    tools.list_agents(status="active")
    assert calls[-1][1].endswith("/agents?status=active")
    tools.get_agent("failure-triage")
    assert calls[-1][:2] == ("GET", f"{tools.AGENT_HUB_API}/agents/failure-triage")
    tools.list_heartbeats(agent="observer", limit=5)
    assert calls[-1][1].endswith("/heartbeats?agent=observer&limit=5")
    tools.get_heartbeat("hb/1")
    assert calls[-1][1].endswith("/heartbeats/hb%2F1")  # ids are path-escaped


def test_wake_posts_mcp_wake_source(calls):
    tools.wake_agent("observer", input="anything wrong?")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/agents/observer/wake")
    assert body == {"input": "anything wrong?", "wake": "mcp"}
    tools.wake_agent("observer", version=2)
    assert calls[-1][2]["version"] == 2 and "subject" not in calls[-1][2]
    tools.wake_agent("failure-triage", input='{"run_id": "r1"}', subject="run:r1")
    assert calls[-1][2]["subject"] == "run:r1"
    tools.cancel_heartbeat("h1")
    assert calls[-1][:2] == ("POST", f"{tools.AGENT_HUB_API}/heartbeats/h1/cancel")


def test_workflow_runs_and_inbox(calls):
    tools.list_workflows()
    assert calls[-1][1].endswith("/workflows")
    tools.get_workflow("certification-plan")
    assert calls[-1][1].endswith("/workflows/certification-plan")
    tools.run_workflow("observer", {"focus": "recent runs"})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/workflows/observer/run")
    assert body == {"inputs": {"focus": "recent runs"}}
    tools.list_workflow_runs(workflow="observer", status="waiting")
    assert calls[-1][1].endswith("/runs?workflow=observer&status=waiting&limit=50")
    tools.get_workflow_run("r1")
    assert calls[-1][:2] == ("GET", f"{tools.AGENT_HUB_API}/runs/r1")
    tools.cancel_workflow_run("r1")
    assert calls[-1][:2] == ("POST", f"{tools.AGENT_HUB_API}/runs/r1/cancel")
    tools.list_approvals()
    assert calls[-1][1].endswith("/approvals?status=pending&limit=100")
    tools.list_approvals(status="all", run_id="r1")
    assert calls[-1][1].endswith("/approvals?status=all&run=r1&limit=100")


def test_no_decide_approval_tool():
    """Deciding an approval is a human act recorded in the portal, never an MCP call."""
    assert not hasattr(tools, "decide_approval")
    names = {n for n, _, _ in registry.TOOL_SPECS}
    assert "list_approvals" in names and not any("decide" in n for n in names)


def test_service_jwt_carries_svc_claim(monkeypatch):
    jwt = pytest.importorskip("jwt")
    monkeypatch.setattr(tools, "_jwt_cache", {"token": None, "exp": 0})
    monkeypatch.delenv("MCP_JWT_SUB", raising=False)
    token = tools._service_jwt("s3cret")
    claims = jwt.decode(token, "s3cret", algorithms=["HS256"])
    assert claims["sub"] == "mcp-server" and claims["svc"] == "mcp-server"
