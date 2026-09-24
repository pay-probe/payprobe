"""Phase 5 egress allowlist: a prompt may only leave for an allowed provider host."""

import time
import urllib.request

import pytest
from agent_hub import egress, llm
from agent_hub import main as hub_main
from hub_testkit import FakeBackend


def _clear(monkeypatch):
    monkeypatch.delenv("AGENT_HUB_EGRESS_ALLOW", raising=False)
    monkeypatch.delenv("ASSIST_LLM_BASE_URL", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1/chat/completions",
        "https://api.anthropic.com/v1/messages",
        "https://API.Anthropic.com/v1/messages",  # host match is case-insensitive
    ],
)
def test_default_provider_hosts_over_https_are_allowed(monkeypatch, url):
    _clear(monkeypatch)
    assert egress.check(url) is None
    assert egress.allowed_hosts() == egress.DEFAULT_HOSTS


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("https://evil.example/v1/messages", "not in the LLM egress allowlist"),
        ("http://api.openai.com/v1/chat/completions", "https required"),
        ("https://api.openai.com@evil.example/v1", "userinfo"),
        ("https://api.openai.com.evil.example/v1", "not in the LLM egress allowlist"),
        ("https://api.anthropic.com:8443/v1/messages", "not in the LLM egress allowlist"),
        ("not a url", "no host"),
    ],
)
def test_everything_else_is_refused(monkeypatch, url, fragment):
    _clear(monkeypatch)
    reason = egress.check(url)
    assert reason and fragment in reason, reason


def test_operator_env_extends_the_allowlist(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AGENT_HUB_EGRESS_ALLOW", "*.azure-api.net, llm-proxy.internal:8080")
    assert egress.check("https://myco.openai.azure-api.net/deployments/x") is None
    assert egress.check("https://azure-api.net/x") is not None  # the wildcard needs a label
    assert egress.check("http://llm-proxy.internal:8080/v1") is None  # explicit host may be http
    assert egress.check("http://llm-proxy.internal:9090/v1") is not None  # port is part of it
    monkeypatch.setenv("ASSIST_LLM_BASE_URL", "https://gateway.corp.example/v1/chat/completions")
    assert egress.check("https://gateway.corp.example/v1/chat/completions") is None
    assert egress.check("http://gateway.corp.example/v1/chat/completions") is None  # operator's own
    assert egress.check("http://localhost:11434/v1/chat/completions") is not None  # not listed
    monkeypatch.setenv("AGENT_HUB_EGRESS_ALLOW", "localhost:11434")
    assert egress.check("http://localhost:11434/v1/chat/completions") is None  # local, listed


def test_post_json_refuses_before_any_bytes_leave(monkeypatch):
    _clear(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("urlopen must not be called for a refused host")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="egress refused"):
        llm._post_json("https://evil.example/v1/messages", {}, {"x": 1})


def test_settings_pointing_at_a_rogue_host_fails_the_heartbeat_not_the_platform(
    client, monkeypatch
):
    """The realistic attack: an admin session edits Settings → AI assistant's
    base_url. The wake is refused at egress, recorded as failed, alerts, and
    nothing is sent anywhere."""
    _clear(monkeypatch)
    for k in ("ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        llm,
        "_settings_llm",
        lambda: {
            "provider": "openai",
            "enabled": True,
            "api_key": "sk-stolen",
            "base_url": "https://evil.example/v1/chat/completions",
            "model": "gpt-4o-mini",
            "source": "settings",
        },
    )
    sent = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: sent.append(a) or 1 / 0)
    client.app.state.llm_factory = hub_main._default_llm_factory  # the real resolution path
    client.app.state.backend_factory = lambda token: FakeBackend()

    r = client.post("/agents/observer/wake", json={"input": "hi"})
    assert r.status_code == 202, r.text
    t0 = time.time()
    while time.time() - t0 < 5:
        hb = client.get(f"/heartbeats/{r.json()['id']}").json()
        if hb["status"] != "running":
            break
        time.sleep(0.02)
    assert hb["status"] == "failed"
    assert "egress refused" in hb["error"] and "evil.example" in hb["error"]
    assert hb["tokens_in"] == 0 and hb["steps"] == [] and sent == []
    assert "api.anthropic.com" in client.get("/health").json()["egress"]
