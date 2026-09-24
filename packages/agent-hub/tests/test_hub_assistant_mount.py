"""D2: the standalone :8400 assistant is mounted inside agent-hub under /assistant."""

import time

import jwt


def test_assistant_is_mounted_and_its_health_is_public(client, monkeypatch):
    r = client.get("/assistant/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "service": "assistant", "via": "agent-hub", "mounted": True}
    # public even when the gate is armed (the orchestrator's /status probe sends no token)
    monkeypatch.setenv("PAYPROBE_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "k")
    assert client.get("/assistant/health").status_code == 200
    # ...while the mounted app's own routes stay behind its gate
    assert client.get("/assistant/agent/chats").status_code == 401
    now = int(time.time())
    tok = jwt.encode(
        {"sub": "olga", "roles": ["operator"], "iat": now, "exp": now + 300}, "k", algorithm="HS256"
    )
    assert (
        client.get("/assistant/agent/chats", headers={"Authorization": f"Bearer {tok}"}).status_code
        == 200
    )


def test_mounted_assistant_serves_its_routes_with_live_stores(client, monkeypatch):
    # its lifespan ran from ours: the chat-history store exists and answers
    assert client.get("/assistant/agent/chats").json() == {"chats": []}
    # a chat turn with no provider configured is answered, not crashed
    for k in ("ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSIST_SETTINGS_LLM", "0")
    r = client.post(
        "/assistant/agent/chat",
        json={"messages": [{"role": "user", "content": "list my connections"}], "mode": "advisor"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict) and body
    # agent-hub's own surface is untouched by the mount
    assert client.get("/health").json()["service"] == "agent-hub"
    assert client.get("/catalog").status_code == 200
