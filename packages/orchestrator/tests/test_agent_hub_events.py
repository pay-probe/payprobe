"""ADR-0010 phase 4: the orchestrator tells agent-hub about run-lifecycle events
(fire-and-forget, optional deployment, never in the run's critical path)."""
import asyncio

import pytest

import orchestrator.api.main as main


def test_terminal_status_maps_to_an_event_or_silence():
    assert main._agent_hub_event_for("passed") == "run.completed"
    assert main._agent_hub_event_for("completed") == "run.completed"
    assert main._agent_hub_event_for("failed") == "run.failed"
    assert main._agent_hub_event_for("error") == "run.failed"
    assert main._agent_hub_event_for("cancelled") is None
    assert main._agent_hub_event_for("pending") is None


@pytest.mark.asyncio
async def test_notify_posts_to_agent_hub_events_with_the_service_token(monkeypatch):
    calls = []

    async def fake_post(url, body):
        calls.append((url, body))
        return {"event": body["event"], "woken": []}

    monkeypatch.setattr(main, "AGENT_HUB_API_URL", "http://agent-hub:8600/")
    monkeypatch.setattr(main, "_http_post_json", fake_post)
    rec = main.RunRecord("run-9", {"name": "mock"}, [{"id": "scn-1"}, {"id": "scn-2"}])
    rec.status = "failed"
    rec.summary = {"status": "failed", "total": 2, "passed": 1, "failed": 1,
                   "error": "issuer timeout", "scenarios": ["big"]}

    main._notify_run_terminal(rec)
    await asyncio.sleep(0)  # let the fire-and-forget task run

    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://agent-hub:8600/events"  # trailing slash folded
    assert body["event"] == "run.failed" and body["at"]
    subj = body["subject"]
    assert subj["run_id"] == "run-9" and subj["status"] == "failed"
    assert subj["environment"] == "mock" and subj["scenario_ids"] == ["scn-1", "scn-2"]
    assert subj["error"] == "issuer timeout"
    assert subj["summary"] == {"status": "failed", "total": 2, "passed": 1, "failed": 1}
    assert "scenarios" not in subj["summary"]  # only the small, bounded keys travel


@pytest.mark.asyncio
async def test_notify_is_a_no_op_without_agent_hub_and_swallows_delivery_errors(monkeypatch):
    calls = []

    async def fake_post(url, body):
        calls.append(url)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main, "_http_post_json", fake_post)
    monkeypatch.setattr(main, "AGENT_HUB_API_URL", "")
    main._notify_agent_hub("run.failed", {"run_id": "r"})
    await asyncio.sleep(0)
    assert calls == []  # agent-hub not deployed: nothing sent

    monkeypatch.setattr(main, "AGENT_HUB_API_URL", "http://agent-hub:8600")
    main._notify_agent_hub("gate.failed", {"run_id": "r", "verdict": "NO-GO"})
    await asyncio.sleep(0)
    assert calls == ["http://agent-hub:8600/events"]  # attempted; the error stayed a log line

    rec = main.RunRecord("run-c", {"name": "mock"}, [])
    rec.status = "cancelled"
    main._notify_run_terminal(rec)
    await asyncio.sleep(0)
    assert len(calls) == 1  # cancelled runs wake nobody
