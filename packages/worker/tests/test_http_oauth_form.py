"""OAuth2 client-credentials + form-encoded bodies in the shared http runner.

ADR-0009 phase 0: the two provider-agnostic gaps between the generic ``http``
adapter and real PSP APIs. OAuth2 (PayPal's auth shape) is exercised against a
local HttpResponder standing in for the token endpoint — token attach, cache,
refresh-before-expiry, both credential styles, and loud failure. Form encoding
(Stripe's required body shape) is exercised end-to-end: a nested dict payload
must arrive url-encoded in provider bracket notation.
"""

from urllib.parse import parse_qs

import pytest

from worker.adapters.http.adapter import HttpAdapter
from worker.adapters.http.responder import HttpResponder
from worker.engine.http_runner import (
    clear_oauth_token_cache,
    form_flatten,
    run_http,
)


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    clear_oauth_token_cache()
    yield
    clear_oauth_token_cache()


async def _start(rules, default=None):
    r = HttpResponder(
        {
            "host": "127.0.0.1",
            "port": 0,
            "rules": rules,
            "default": default or {"status": 200, "json": {"ok": True}},
        }
    )
    port = await r.start()
    return r, port


def _token_rule(token="tok-1", expires_in=3600, status=200):
    return {
        "when": {"method": "POST", "path": {"eq": "/oauth/token"}},
        "respond": {
            "status": status,
            "json": {"access_token": token, "expires_in": expires_in},
        },
    }


def _oauth(port, **extra):
    return {
        "type": "oauth2_client_credentials",
        "token_url": f"http://127.0.0.1:{port}/oauth/token",
        "client_id": "cid",
        "client_secret": "shh",
        **extra,
    }


def _token_hits(responder):
    return sum(1 for r in responder.received if r["path"] == "/oauth/token")


# -- form_flatten (the Stripe bracket encoding) --------------------------------


def test_form_flatten_nested_dict_and_list():
    flat = form_flatten(
        {
            "amount": 1999,
            "currency": "usd",
            "capture": True,
            "card": {"number": "4242424242424242", "exp_month": 12},
            "items": [{"price": 5}, {"price": 7}],
            "skip": None,
        }
    )
    assert flat == {
        "amount": "1999",
        "currency": "usd",
        "capture": "true",
        "card[number]": "4242424242424242",
        "card[exp_month]": "12",
        "items[0][price]": "5",
        "items[1][price]": "7",
    }


def test_form_flatten_false_and_deep_nesting():
    flat = form_flatten({"a": {"b": {"c": False}}})
    assert flat == {"a[b][c]": "false"}


# -- form bodies end-to-end ----------------------------------------------------


async def test_form_body_dict_is_bracket_encoded_on_the_wire():
    responder, port = await _start([])
    try:
        res = await run_http(
            {
                "method": "POST",
                "url": f"http://127.0.0.1:{port}/v1/payment_intents",
                "sendBody": True,
                "contentType": "form",
                "body": {"amount": 100, "payment_method_data": {"card": {"number": "4242"}}},
            },
            {},
        )
        assert res.ok
        raw = responder.received[0]["raw"]
        parsed = parse_qs(raw)
        assert parsed["amount"] == ["100"]
        assert parsed["payment_method_data[card][number]"] == ["4242"]
        ctype = responder.received[0]["headers"].get("content-type", "")
        assert "application/x-www-form-urlencoded" in ctype
    finally:
        await responder.stop()


async def test_http_adapter_connection_level_form_content_type():
    responder, port = await _start([])
    try:
        adapter = HttpAdapter(
            {
                "adapter": "http",
                "base_url": f"http://127.0.0.1:{port}",
                "content_type": "form",
                "actions": {"create_intent": {"method": "POST", "path": "/v1/payment_intents"}},
            }
        )
        await adapter.connect()
        sr = await adapter.execute("create_intent", {"amount": 500, "card": {"cvc": "123"}})
        assert sr.success
        parsed = parse_qs(responder.received[0]["raw"])
        assert parsed["card[cvc]"] == ["123"]
    finally:
        await responder.stop()


async def test_http_adapter_per_action_content_type_overrides_connection():
    responder, port = await _start([])
    try:
        adapter = HttpAdapter(
            {
                "adapter": "http",
                "base_url": f"http://127.0.0.1:{port}",
                "content_type": "form",
                "actions": {
                    "as_json": {"method": "POST", "path": "/v2/orders", "content_type": "json"}
                },
            }
        )
        await adapter.connect()
        sr = await adapter.execute("as_json", {"intent": "CAPTURE"})
        assert sr.success
        assert responder.received[0]["body"] == {"intent": "CAPTURE"}
    finally:
        await responder.stop()


# -- assertion field extraction (list indices) ---------------------------------


def test_assertion_extract_field_supports_list_indices():
    # ADR-0009: provider payloads are list-shaped (PayPal details[0].issue,
    # purchase_units[0].payments.captures[0].id) — assertions must reach in
    # with the same bracket syntax ${...} interpolation uses.
    adapter = HttpAdapter({"base_url": "http://x"})
    data = {
        "body": {
            "details": [{"issue": "INSTRUMENT_DECLINED"}],
            "purchase_units": [{"payments": {"captures": [{"id": "CAP1"}]}}],
        }
    }
    assert adapter._extract_field(data, "body.details[0].issue") == "INSTRUMENT_DECLINED"
    assert adapter._extract_field(data, "body.purchase_units[0].payments.captures[0].id") == "CAP1"
    assert adapter._extract_field(data, "body.details[3].issue") is None
    assert adapter._extract_field(data, "body.details[0].nope") is None
    assert adapter._extract_field(data, "body.missing[0]") is None


# -- oauth2 client credentials -------------------------------------------------


async def test_oauth2_token_attached_as_bearer():
    responder, port = await _start([_token_rule("tok-abc")])
    try:
        res = await run_http(
            {
                "method": "GET",
                "url": f"http://127.0.0.1:{port}/v2/orders/1",
                "authentication": _oauth(port),
            },
            {},
        )
        assert res.ok
        token_req = responder.received[0]
        assert token_req["path"] == "/oauth/token"
        assert "grant_type=client_credentials" in token_req["raw"]
        # default style is HTTP Basic (the PayPal shape) — creds not in body
        assert "client_secret" not in token_req["raw"]
        assert token_req["headers"].get("authorization", "").startswith("Basic ")
        api_req = responder.received[1]
        assert api_req["headers"]["authorization"] == "Bearer tok-abc"
    finally:
        await responder.stop()


async def test_oauth2_token_is_cached_across_requests():
    responder, port = await _start([_token_rule()])
    try:
        cfg = {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/v2/orders/1",
            "authentication": _oauth(port),
        }
        assert (await run_http(cfg, {})).ok
        assert (await run_http(cfg, {})).ok
        assert _token_hits(responder) == 1
    finally:
        await responder.stop()


async def test_oauth2_short_expiry_refreshes():
    # expires_in below the 60 s refresh margin -> every call re-fetches
    responder, port = await _start([_token_rule(expires_in=30)])
    try:
        cfg = {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/v2/orders/1",
            "authentication": _oauth(port),
        }
        assert (await run_http(cfg, {})).ok
        assert (await run_http(cfg, {})).ok
        assert _token_hits(responder) == 2
    finally:
        await responder.stop()


async def test_oauth2_body_style_sends_credentials_in_form():
    responder, port = await _start([_token_rule()])
    try:
        res = await run_http(
            {
                "method": "GET",
                "url": f"http://127.0.0.1:{port}/v2/orders/1",
                "authentication": _oauth(port, auth_style="body"),
            },
            {},
        )
        assert res.ok
        parsed = parse_qs(responder.received[0]["raw"])
        assert parsed["client_id"] == ["cid"]
        assert parsed["client_secret"] == ["shh"]
    finally:
        await responder.stop()


async def test_oauth2_token_failure_is_a_loud_build_error():
    responder, port = await _start([_token_rule(status=500)])
    try:
        res = await run_http(
            {
                "method": "GET",
                "url": f"http://127.0.0.1:{port}/v2/orders/1",
                "authentication": _oauth(port),
            },
            {},
        )
        assert not res.ok
        assert "oauth2" in (res.error or "")
        # the API itself was never called with a broken token
        assert all(r["path"] == "/oauth/token" for r in responder.received)
    finally:
        await responder.stop()


async def test_health_any_status_counts_4xx_as_reachable():
    # a provider API root answers 401/404 anonymously — still "up" (ADR-0009)
    responder, port = await _start(
        [], default={"status": 401, "json": {"error": {"type": "invalid_request_error"}}}
    )
    try:
        base = {"adapter": "http", "base_url": f"http://127.0.0.1:{port}"}
        strict = HttpAdapter(dict(base))
        await strict.connect()
        assert await strict.health_check() is False  # unchanged default

        lenient = HttpAdapter({**base, "health_any_status": True})
        await lenient.connect()
        assert await lenient.health_check() is True

        offline = HttpAdapter(
            {"adapter": "http", "base_url": "http://127.0.0.1:1", "health_any_status": True}
        )
        await offline.connect()
        assert await offline.health_check() is False  # no answer is still down
    finally:
        await responder.stop()


async def test_oauth2_missing_token_url_fails_cleanly():
    res = await run_http(
        {
            "method": "GET",
            "url": "http://127.0.0.1:1/x",
            "authentication": {"type": "oauth2_client_credentials"},
        },
        {},
    )
    assert not res.ok
    assert "token_url" in (res.error or "")
