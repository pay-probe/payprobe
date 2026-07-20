"""Settings-stored LLM config fallback (one key location: Settings → AI
assistant, pulled from scenario-service over /assist/config/material).

Precedence contract: ASSIST_LLM_* env wins when a key is set; otherwise the
Settings config is used; any fetch failure degrades to env-only (disabled).
Conftest sets ASSIST_SETTINGS_LLM=0 for hermetic runs — these tests re-enable
it and monkeypatch the transport.
"""
import pytest

from assistant_service import config


MATERIAL = {
    "provider": "anthropic",
    "enabled": True,
    "api_key": "sk-from-settings",
    "base_url": "https://api.anthropic.com/v1/messages",
    "model": "claude-haiku-4-5-20251001",
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ASSIST_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "ASSIST_LLM_MODEL", "ASSIST_LLM_PROVIDER", "ASSIST_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSIST_SETTINGS_LLM", "1")
    # empty the TTL cache between tests
    config._settings_cache.update(at=0.0, llm=None)
    yield
    config._settings_cache.update(at=0.0, llm=None)


def test_settings_config_used_when_no_env_key(monkeypatch):
    calls = []

    def fake_request(method, url, body=None, raw=False, timeout=30):
        calls.append((method, url))
        return dict(MATERIAL)

    monkeypatch.setattr("assistant_service.rest.request", fake_request)
    llm = config.resolve_llm()
    assert llm["enabled"] and llm["api_key"] == "sk-from-settings"
    assert llm["provider"] == "anthropic"
    assert llm["model"] == "claude-haiku-4-5-20251001"
    assert llm["source"] == "settings"
    assert calls and calls[0][1].endswith("/assist/config/material")


def test_env_key_wins_and_skips_fetch(monkeypatch):
    def boom(*a, **kw):  # the fetch must not even be attempted
        raise AssertionError("should not fetch settings when env key set")

    monkeypatch.setattr("assistant_service.rest.request", boom)
    monkeypatch.setenv("ASSIST_LLM_API_KEY", "sk-env")
    llm = config.resolve_llm()
    assert llm["api_key"] == "sk-env" and llm["source"] == "env"


def test_fetch_failure_degrades_to_disabled(monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("cannot reach scenario-service")

    monkeypatch.setattr("assistant_service.rest.request", fail)
    llm = config.resolve_llm()
    assert not llm["enabled"] and llm["api_key"] == ""


def test_settings_toggle_off_is_respected(monkeypatch):
    # Settings saved but "Use the LLM provider" unchecked ⇒ stays unconfigured
    monkeypatch.setattr(
        "assistant_service.rest.request",
        lambda *a, **kw: {**MATERIAL, "enabled": False})
    llm = config.resolve_llm()
    assert not llm["enabled"]


def test_kill_switch_disables_fetch(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("ASSIST_SETTINGS_LLM=0 must skip the fetch")

    monkeypatch.setattr("assistant_service.rest.request", boom)
    monkeypatch.setenv("ASSIST_SETTINGS_LLM", "0")
    llm = config.resolve_llm()
    assert not llm["enabled"]


def test_result_is_cached_for_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "assistant_service.rest.request",
        lambda *a, **kw: calls.append(1) or dict(MATERIAL))
    config.resolve_llm()
    config.resolve_llm()
    assert len(calls) == 1  # second resolve inside the TTL hits the cache
