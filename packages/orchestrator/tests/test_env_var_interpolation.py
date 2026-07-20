"""Environment ${vars.*} interpolation (connection params from global variables)."""
import pytest

from orchestrator.api import main as m


def test_interp_whole_token_preserves_type():
    vars_ = {"port": 7001, "url": "https://h/api"}
    assert m._interp({"port": "${vars.port}", "url": "${vars.url}"}, vars_) == {
        "port": 7001,  # int kept, not stringified
        "url": "https://h/api",
    }


def test_interp_in_string_and_unknown_left_alone():
    vars_ = {"host": "h"}
    out = m._interp({"u": "http://${vars.host}:8080", "x": "${vars.missing}"}, vars_)
    assert out["u"] == "http://h:8080"
    assert out["x"] == "${vars.missing}"  # unknown name untouched


def test_interp_recurses_nested():
    vars_ = {"k": "secret"}
    node = {"adapters": {"http": {"api_key": "${vars.k}", "n": [1, "${vars.k}"]}}}
    out = m._interp(node, vars_)
    assert out["adapters"]["http"]["api_key"] == "secret"
    assert out["adapters"]["http"]["n"] == [1, "secret"]


async def test_interpolate_env_vars_fetches_and_substitutes(monkeypatch):
    async def fake_get(url):
        assert url.endswith("/variables/global")
        return {"variables": {"http_base_url": "https://live/api",
                              "http_timeout_ms": 5000}}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    env = {"adapters": {"http": {
        "base_url": "${vars.http_base_url}",
        "timeout_ms": "${vars.http_timeout_ms}",
    }}}
    out = await m._interpolate_env_vars(env)
    assert out["adapters"]["http"]["base_url"] == "https://live/api"
    assert out["adapters"]["http"]["timeout_ms"] == 5000


async def test_interpolate_env_vars_noop_without_tokens(monkeypatch):
    async def boom(url):  # must not be called when no ${vars. token present
        raise AssertionError("should not fetch")

    monkeypatch.setattr(m, "_http_get_json", boom)
    env = {"mode": "mock", "adapters": {"http": {"mock": True}}}
    assert await m._interpolate_env_vars(env) == env


async def test_interpolate_env_vars_best_effort_on_fetch_failure(monkeypatch):
    async def boom(url):
        raise RuntimeError("variables service down")

    monkeypatch.setattr(m, "_http_get_json", boom)
    env = {"adapters": {"http": {"base_url": "${vars.http_base_url}"}}}
    # fetch fails → env returned unchanged (no crash)
    assert await m._interpolate_env_vars(env) == env


# -- environment resolution: registry-first, bundled fallback ----------------

async def test_load_env_prefers_registry(monkeypatch):
    async def fake_get(url):
        assert url.endswith("/environments/staging")
        return {"key": "staging", "name": "Staging", "adapters": {"http": {"base_url": "live"}}}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    env = await m._load_env_by_name("staging")
    assert env["adapters"]["http"]["base_url"] == "live"
    assert "key" not in env  # registry-internal field stripped


async def test_load_env_falls_back_to_bundled(monkeypatch):
    async def miss(url):
        raise RuntimeError("not in registry / service down")

    monkeypatch.setattr(m, "_http_get_json", miss)
    # 'mock' isn't in the registry here but is a bundled file → resolves
    env = await m._load_env_by_name("mock")
    assert env.get("name") == "mock" and env.get("mode") == "mock"


async def test_load_env_unknown_raises(monkeypatch):
    from fastapi import HTTPException

    async def miss(url):
        raise RuntimeError("down")

    monkeypatch.setattr(m, "_http_get_json", miss)
    try:
        await m._load_env_by_name("does-not-exist-anywhere")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
