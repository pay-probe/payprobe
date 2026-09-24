"""Phase 4 inbound webhooks: signed external events and direct agent wakes,
credentialed by the HMAC signature alone (the bearer gate skips /webhooks/)."""

import json
import time

from agent_hub.alerts import sign
from agent_hub.llm import FakeLLMBackend
from hub_testkit import FakeBackend, agent_spec, wire_fakes

SECRET = "whsec_inbound"


def _post(client, path, body: dict | str | None, secret=SECRET, sig=None):
    raw = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers["X-PayProbe-Signature"] = sig
    elif secret:
        headers["X-PayProbe-Signature"] = sign(secret, raw)
    return client.post(path, content=raw, headers=headers)


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_unconfigured_webhooks_answer_503(client, monkeypatch):
    monkeypatch.delenv("AGENT_HUB_WEBHOOK_SECRET", raising=False)
    r = _post(client, "/webhooks/events/run.failed", {"run_id": "r"}, secret="anything")
    assert r.status_code == 503 and "AGENT_HUB_WEBHOOK_SECRET" in r.text


def test_signed_event_webhook_wakes_agents_without_a_bearer(client, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_WEBHOOK_SECRET", SECRET)
    # arm the platform gate: a bearer would now be required everywhere else
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    assert client.get("/agents").status_code == 401  # the gate is really on
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="[]"))

    r = _post(client, "/webhooks/events/run.failed", {"run_id": "run-7", "status": "failed"})
    assert r.status_code == 202, r.text
    woken = {w["agent"]: w for w in r.json()["woken"]}
    assert set(woken) == {"observer", "failure-triage"}
    # read the heartbeat back with a real token (the gate is armed)
    import jwt

    now = int(time.time())
    tok = jwt.encode({"sub": "o", "roles": ["admin"], "iat": now, "exp": now + 300}, "k", "HS256")
    hb = client.get(
        f"/heartbeats/{woken['failure-triage']['heartbeat_id']}",
        headers={"Authorization": f"Bearer {tok}"},
    ).json()
    assert hb["wake"] == "event" and hb["invoked_by"] == "event:run.failed"
    payload = json.loads(hb["input"])
    assert payload["run_id"] == "run-7" and payload["source"] == "webhook"


def test_bad_signature_wrong_secret_or_stale_timestamp_is_401(client, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_WEBHOOK_SECRET", SECRET)
    body = {"run_id": "r"}
    assert _post(client, "/webhooks/events/run.failed", body, secret="wrong").status_code == 401
    assert _post(client, "/webhooks/events/run.failed", body, sig="").status_code == 401
    stale = sign(SECRET, json.dumps(body), ts=int(time.time()) - 3600)
    assert _post(client, "/webhooks/events/run.failed", body, sig=stale).status_code == 401
    # a tampered body no longer matches the signature
    good = sign(SECRET, json.dumps(body))
    assert (
        _post(client, "/webhooks/events/run.failed", {"run_id": "x"}, sig=good).status_code == 401
    )


def test_event_webhook_validates_name_and_body(client, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_WEBHOOK_SECRET", SECRET)
    assert _post(client, "/webhooks/events/Bad%20Event", {}).status_code == 422
    assert _post(client, "/webhooks/events/run.failed", "not json").status_code == 422
    assert _post(client, "/webhooks/events/run.failed", "[1, 2]").status_code == 422
    r = _post(client, "/webhooks/events/nobody.listens", None)  # empty body is fine
    assert r.status_code == 202 and r.json()["woken"] == []


def test_agent_webhook_is_opt_in_and_carries_the_body_as_input(client, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_WEBHOOK_SECRET", SECRET)
    wire_fakes(client.app, FakeBackend(), lambda spec: FakeLLMBackend(final="ok"))
    # observer does not declare a webhook trigger
    assert _post(client, "/webhooks/agents/observer", {"x": 1}).status_code == 409
    assert _post(client, "/webhooks/agents/nope", {"x": 1}).status_code == 404

    r = client.post(
        "/agents",
        json={"name": "hooked", "spec": agent_spec(triggers=[{"kind": "webhook"}])},
    )
    assert r.status_code == 201, r.text
    assert client.post("/agents/hooked/versions/1/publish").status_code == 200
    r = _post(client, "/webhooks/agents/hooked", {"build": 42, "status": "red"})
    assert r.status_code == 202, r.text
    hb = _wait_done(client, r.json()["id"])
    assert hb["status"] == "done" and hb["wake"] == "webhook"
    assert hb["invoked_by"] == "webhook:hooked"
    assert json.loads(hb["input"]) == {"build": 42, "status": "red"}
    # a second signed call while paused is refused and recorded, never run
    client.put("/pause", json={"paused": True})
    r = _post(client, "/webhooks/agents/hooked", {"build": 43})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    client.put("/pause", json={"paused": False})
    assert client.get("/catalog").json()["trigger_kinds"][-1] == "webhook"
