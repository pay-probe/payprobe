"""D11 alert webhook: which heartbeats alert, how the POST is signed, retries,
and that a dead receiver never touches the heartbeat record."""

import json
import time

from agent_hub.alerts import Alerter, events_for, findings_of, sign, verify
from agent_hub.llm import FakeLLMBackend
from hub_testkit import FakeBackend, agent_spec, wire_fakes

# -- pure ----------------------------------------------------------------------------


def test_events_for_failed_statuses_and_advisor_findings():
    base = {"id": "h1", "agent": "observer", "version": 1, "status": "failed", "error": "llm: boom"}
    ev = events_for(base, mode="advisor")
    assert [e for e, _ in ev] == ["heartbeat.failed"]
    assert ev[0][1]["heartbeat_id"] == "h1" and ev[0][1]["error"] == "llm: boom"
    for status in ("budget_exceeded", "timed_out"):
        assert [e for e, _ in events_for({**base, "status": status}, mode="plan")] == [
            f"heartbeat.{status}"
        ]

    findings = [
        {"severity": "info", "headline": "all quiet"},
        {"severity": "warn", "headline": "run-1 failed", "subject": "run-1"},
        {"severity": "critical", "headline": "issuer dead"},
    ]
    done = {**base, "status": "done", "error": None, "result": json.dumps(findings)}
    ev = events_for(done, mode="advisor")
    assert [e for e, _ in ev] == ["finding", "finding"]
    assert [p["finding"]["severity"] for _, p in ev] == ["warn", "critical"]
    assert ev[0][1]["agent"] == "observer"  # finding carries the heartbeat head
    assert events_for(done, mode="plan") == []  # a plan-mode result is not findings
    assert events_for({**done, "status": "cancelled"}, mode="advisor") == []


def test_findings_of_tolerates_wrappers_and_garbage():
    assert findings_of('{"findings": [{"severity": "warn"}]}') == [{"severity": "warn"}]
    assert findings_of('```json\n[{"severity": "high"}]\n```') == [{"severity": "high"}]
    assert findings_of("not json") == []
    assert findings_of(None) == []
    assert findings_of("[1, 2]") == []


def test_signature_round_trips_and_rejects_tampering():
    body = '{"event":"heartbeat.failed"}'
    header = sign("whsec", body)
    assert header.startswith("t=") and ",v1=" in header
    assert verify("whsec", body, header)
    assert not verify("other", body, header)
    assert not verify("whsec", body + " ", header)
    assert not verify("whsec", body, sign("whsec", body, ts=int(time.time()) - 3600))
    assert not verify("whsec", body, "garbage")


# -- through the app -----------------------------------------------------------------


class _Receiver:
    """Fake transport: records every POST, answers the scripted statuses
    (the last one repeats)."""

    def __init__(self, statuses=(200,)):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, url: str, body: bytes, headers: dict) -> int:
        self.calls.append((url, body.decode(), dict(headers)))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


class _Boom(FakeLLMBackend):
    def complete(self, convo, tools):
        raise RuntimeError("provider down")


def _arm(client, receiver, secret="whsec") -> Alerter:
    client.app.state.alerts = Alerter(
        "http://alerts.local/hook", secret, backoff=(0.0, 0.0), transport=receiver
    )
    return client.app.state.alerts


def _wait_calls(receiver: _Receiver, n: int, timeout=5.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if len(receiver.calls) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"webhook received {len(receiver.calls)} call(s), wanted {n}")


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def _wait_stats(client, pred, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        stats = client.get("/health").json()["alerts"]
        if pred(stats):
            return stats
        time.sleep(0.02)
    raise AssertionError(f"alert stats never matched: {stats}")


def test_failed_heartbeat_posts_a_signed_alert(client):
    rx = _Receiver()
    _arm(client, rx)
    wire_fakes(client.app, FakeBackend(), lambda spec: _Boom())
    hb_id = client.post("/agents/observer/wake", json={}).json()["id"]
    _wait_calls(rx, 1)
    url, body, headers = rx.calls[0]
    assert url == "http://alerts.local/hook"
    assert headers["X-PayProbe-Event"] == "heartbeat.failed"
    assert headers["Content-Type"] == "application/json"
    assert verify("whsec", body, headers["X-PayProbe-Signature"])
    env = json.loads(body)
    assert env["event"] == "heartbeat.failed" and env["source"] == "agent-hub" and env["at"]
    assert env["data"]["heartbeat_id"] == hb_id and env["data"]["agent"] == "observer"
    assert "provider down" in env["data"]["error"]
    assert _wait_done(client, hb_id)["status"] == "failed"
    stats = _wait_stats(client, lambda s: s["sent"] == 1)
    assert stats["configured"] is True and stats["signed"] is True and stats["failed"] == 0


def test_advisor_findings_alert_at_warn_and_above(client):
    rx = _Receiver()
    _arm(client, rx)
    findings = [
        {"severity": "info", "headline": "quiet"},
        {"severity": "warn", "headline": "run-1 failed", "subject": "run-1"},
    ]
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final=json.dumps(findings)))
    hb_id = client.post("/agents/observer/wake", json={}).json()["id"]
    assert _wait_done(client, hb_id)["status"] == "done"
    _wait_calls(rx, 1)
    _wait_stats(client, lambda s: s["pending"] == 0)
    assert len(rx.calls) == 1  # the info finding did not alert
    assert rx.calls[0][2]["X-PayProbe-Event"] == "finding"
    data = json.loads(rx.calls[0][1])["data"]
    assert data["finding"]["headline"] == "run-1 failed" and data["heartbeat_id"] == hb_id


def test_budget_refusal_alerts_too(client):
    rx = _Receiver()
    _arm(client, rx)
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(tokens_per_turn=600))
    r = client.post(
        "/agents",
        json={
            "name": "thrifty",
            "spec": agent_spec(budget={"daily_tokens": 1000, "hard_stop": True}),
        },
    )
    assert r.status_code == 201, r.text
    assert client.post("/agents/thrifty/versions/1/publish").status_code == 200
    for _ in range(5):
        r = client.post("/agents/thrifty/wake", json={})
        if r.json()["status"] == "budget_exceeded":
            break
        _wait_done(client, r.json()["id"])
    else:
        raise AssertionError("budget never tripped")
    _wait_calls(rx, 1)
    assert rx.calls[0][2]["X-PayProbe-Event"] == "heartbeat.budget_exceeded"
    assert "daily token budget" in json.loads(rx.calls[0][1])["data"]["error"]


def test_delivery_retries_on_5xx_then_succeeds(client):
    rx = _Receiver(statuses=[503, 200])
    _arm(client, rx)
    wire_fakes(client.app, FakeBackend(), lambda spec: _Boom())
    client.post("/agents/observer/wake", json={})
    _wait_calls(rx, 2)
    stats = _wait_stats(client, lambda s: s["sent"] == 1)
    assert stats["failed"] == 0 and stats["recent"][-1]["attempts"] == 2


def test_4xx_is_final_no_retry(client):
    rx = _Receiver(statuses=[400])
    _arm(client, rx)
    wire_fakes(client.app, FakeBackend(), lambda spec: _Boom())
    client.post("/agents/observer/wake", json={})
    stats = _wait_stats(client, lambda s: s["failed"] == 1)
    assert len(rx.calls) == 1 and stats["sent"] == 0


def test_dead_receiver_never_touches_the_heartbeat(client):
    async def boom(url, body, headers):
        raise ConnectionError("no route to host")

    client.app.state.alerts = Alerter(
        "http://alerts.local/hook", "", backoff=(0.0,), transport=boom
    )
    wire_fakes(client.app, FakeBackend(), lambda spec: _Boom())
    hb_id = client.post("/agents/observer/wake", json={}).json()["id"]
    hb = _wait_done(client, hb_id)
    assert hb["status"] == "failed" and "provider down" in hb["error"]
    stats = _wait_stats(client, lambda s: s["failed"] == 1)
    assert stats["sent"] == 0 and stats["signed"] is False
    assert (
        stats["recent"][-1]["attempts"] == 2 and "ConnectionError" in stats["recent"][-1]["status"]
    )


def test_unconfigured_alerter_is_a_no_op(client):
    client.app.state.alerts = Alerter()  # no URL: the default deployment
    assert client.get("/health").json()["alerts"]["configured"] is False
    wire_fakes(client.app, FakeBackend(), lambda spec: _Boom())
    hb_id = client.post("/agents/observer/wake", json={}).json()["id"]
    assert _wait_done(client, hb_id)["status"] == "failed"
    assert client.get("/health").json()["alerts"] == {
        "configured": False,
        "signed": False,
        "sent": 0,
        "failed": 0,
        "pending": 0,
        "recent": [],
    }
