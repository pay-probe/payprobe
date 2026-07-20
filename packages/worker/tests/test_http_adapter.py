"""HttpAdapter: a generic REST connection resolves named actions against a base
URL and maps the response into a StepResult. Network is stubbed."""

from worker.adapters.http.adapter import HttpAdapter
import worker.engine.http_runner as http_runner
from worker.engine.http_runner import HttpResult


def _capture(monkeypatch):
    seen = {}

    async def fake_run_http(cfg, context):
        seen["cfg"] = cfg
        seen["context"] = context
        return HttpResult(
            ok=True,
            status_code=200,
            response={"status_code": 200, "body": {"ok": True}},
            request={"method": cfg["method"], "url": cfg["url"]},
        )

    monkeypatch.setattr(http_runner, "run_http", fake_run_http)
    return seen


async def test_action_resolves_method_path_and_body(monkeypatch):
    seen = _capture(monkeypatch)
    a = HttpAdapter(
        {
            "base_url": "https://api.example.com/v1/",
            "headers": {"X-Tenant": "acme"},
            "actions": {"authorize": {"method": "POST", "path": "/payments"}},
        }
    )
    await a.connect()
    res = await a.execute("authorize", {"amount": 100})
    assert res.success
    assert seen["cfg"]["method"] == "POST"
    assert seen["cfg"]["url"] == "https://api.example.com/v1/payments"  # base + path
    assert seen["cfg"]["sendBody"] is True and seen["cfg"]["body"] == {"amount": 100}
    assert {"name": "X-Tenant", "value": "acme"} in seen["cfg"]["headers"]
    assert res.response_payload["body"] == {"ok": True}


async def test_unknown_action_defaults_to_get(monkeypatch):
    seen = _capture(monkeypatch)
    a = HttpAdapter({"base_url": "https://api.example.com"})
    await a.connect()
    await a.execute("/health", {})
    assert seen["cfg"]["method"] == "GET"
    assert seen["cfg"]["url"] == "https://api.example.com/health"
    assert seen["cfg"]["sendBody"] is False


async def test_registry_maps_http_to_http_adapter():
    from worker.adapters.registry import ADAPTER_MAP

    assert ADAPTER_MAP.get("http") is HttpAdapter


async def test_missing_base_url_fails_fast_at_connect():
    """No base_url must be a clear connect-time config error, not httpx's
    'UnsupportedProtocol' at send time."""
    import pytest

    a = HttpAdapter({"actions": {"authorize": {"method": "POST"}}})
    with pytest.raises(ValueError, match="no usable base_url"):
        await a.connect()


async def test_schemeless_base_url_fails_fast_at_connect():
    import pytest

    a = HttpAdapter({"base_url": "api.example.com/v1"})
    with pytest.raises(ValueError, match="http:// or https://"):
        await a.connect()
