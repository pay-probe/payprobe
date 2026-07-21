"""PayPal v2 Orders simulator — driven over a real HTTP socket (ADR-0009 phase 2).

Validates the public contract the simulator stands in for: the OAuth2
client-credentials token endpoint (exercised end-to-end through the shared
http runner's ``oauth2_client_credentials`` strategy — the whole point of this
sim), the order lifecycle (CREATED → capture → COMPLETED with captures under
purchase_units[].payments), the INSTRUMENT_DECLINED 422 merchants must retry
on, refund lifecycle checks, the PayPal error envelope, and the duck-typed
responder surface the orchestrator + portal consume.
"""

import httpx
import pytest

from worker.adapters.http.adapter import HttpAdapter
from worker.adapters.scheme.paypal_sim import PayPalOrdersSimulator
from worker.engine.http_runner import clear_oauth_token_cache

AUTH = {"Authorization": "Bearer A21.manual-test-token"}


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    clear_oauth_token_cache()
    yield
    clear_oauth_token_cache()


async def _sim(config=None):
    sim = PayPalOrdersSimulator({"host": "127.0.0.1", "port": 0, **(config or {})})
    port = await sim.start()
    return sim, f"http://127.0.0.1:{port}"


def _order(value="100.00", currency="USD"):
    return {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
    }


async def _post(base, path, json=None, headers=AUTH, data=None, auth=None):
    async with httpx.AsyncClient() as client:
        return await client.post(f"{base}{path}", json=json, headers=headers, data=data, auth=auth)


async def _get(base, path, headers=AUTH):
    async with httpx.AsyncClient() as client:
        return await client.get(f"{base}{path}", headers=headers)


# -- token endpoint ------------------------------------------------------------


async def test_token_endpoint_issues_bearer():
    sim, base = await _sim()
    try:
        resp = await _post(
            base,
            "/v1/oauth2/token",
            headers={},
            data={"grant_type": "client_credentials"},
            auth=("cid", "shh"),
        )
        assert resp.status_code == 200
        tok = resp.json()
        assert tok["token_type"] == "Bearer"
        assert tok["access_token"].startswith("A21.")
        assert tok["expires_in"] == 32400
    finally:
        await sim.stop()


async def test_token_endpoint_requires_credentials():
    sim, base = await _sim()
    try:
        resp = await _post(
            base, "/v1/oauth2/token", headers={}, data={"grant_type": "client_credentials"}
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"
    finally:
        await sim.stop()


# -- order lifecycle -----------------------------------------------------------


async def test_create_capture_retrieve():
    sim, base = await _sim()
    try:
        created = await _post(base, "/v2/checkout/orders", _order())
        assert created.status_code == 201
        order = created.json()
        assert order["status"] == "CREATED"
        assert len(order["id"]) == 17
        oid = order["id"]

        captured = await _post(base, f"/v2/checkout/orders/{oid}/capture", {})
        assert captured.status_code == 201
        doc = captured.json()
        assert doc["status"] == "COMPLETED"
        cap = doc["purchase_units"][0]["payments"]["captures"][0]
        assert cap["status"] == "COMPLETED"
        assert cap["amount"] == {"currency_code": "USD", "value": "100.00"}
        assert cap["final_capture"] is True

        got = (await _get(base, f"/v2/checkout/orders/{oid}")).json()
        assert got["status"] == "COMPLETED"

        # double capture is a business validation error
        again = await _post(base, f"/v2/checkout/orders/{oid}/capture", {})
        assert again.status_code == 422
        assert again.json()["details"][0]["issue"] == "ORDER_ALREADY_CAPTURED"
    finally:
        await sim.stop()


async def test_instrument_declined_trigger():
    sim, base = await _sim({"paypal": {"decline_over": 500}})
    try:
        ok_id = (await _post(base, "/v2/checkout/orders", _order("499.99"))).json()["id"]
        ok = await _post(base, f"/v2/checkout/orders/{ok_id}/capture", {})
        assert ok.json()["status"] == "COMPLETED"

        bad_id = (await _post(base, "/v2/checkout/orders", _order("500.00"))).json()["id"]
        declined = await _post(base, f"/v2/checkout/orders/{bad_id}/capture", {})
        assert declined.status_code == 422
        err = declined.json()
        assert err["name"] == "UNPROCESSABLE_ENTITY"
        assert err["details"][0]["issue"] == "INSTRUMENT_DECLINED"
        assert "debug_id" in err
        # a declined order is retryable — still CREATED, capture works under limit
        still = (await _get(base, f"/v2/checkout/orders/{bad_id}")).json()
        assert still["status"] == "CREATED"
    finally:
        await sim.stop()


async def test_mock_response_header_forces_issue():
    """PayPal's documented sandbox negative-testing mechanism works on the sim."""
    sim, base = await _sim()
    try:
        oid = (await _post(base, "/v2/checkout/orders", _order())).json()["id"]
        declined = await _post(
            base,
            f"/v2/checkout/orders/{oid}/capture",
            {},
            headers={
                **AUTH,
                "PayPal-Mock-Response": '{"mock_application_codes": "INSTRUMENT_DECLINED"}',
            },
        )
        assert declined.status_code == 422
        assert declined.json()["details"][0]["issue"] == "INSTRUMENT_DECLINED"
        # without the header the same capture succeeds
        ok = await _post(base, f"/v2/checkout/orders/{oid}/capture", {})
        assert ok.json()["status"] == "COMPLETED"
    finally:
        await sim.stop()


async def test_create_order_validation_and_missing_order():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v2/checkout/orders", {"intent": "CAPTURE"})
        assert resp.status_code == 400
        assert resp.json()["details"][0]["issue"] == "MISSING_REQUIRED_PARAMETER"

        missing = await _post(base, "/v2/checkout/orders/NOPE12345/capture", {})
        assert missing.status_code == 404
        assert missing.json()["details"][0]["issue"] == "INVALID_RESOURCE_ID"
    finally:
        await sim.stop()


# -- refunds -------------------------------------------------------------------


async def test_refund_partial_full_and_exceeded():
    sim, base = await _sim()
    try:
        oid = (await _post(base, "/v2/checkout/orders", _order("50.00"))).json()["id"]
        doc = (await _post(base, f"/v2/checkout/orders/{oid}/capture", {})).json()
        cap_id = doc["purchase_units"][0]["payments"]["captures"][0]["id"]

        part = await _post(
            base,
            f"/v2/payments/captures/{cap_id}/refund",
            {"amount": {"currency_code": "USD", "value": "20.00"}},
        )
        assert part.status_code == 201
        assert part.json()["status"] == "COMPLETED"
        assert part.json()["amount"]["value"] == "20.00"

        over = await _post(
            base,
            f"/v2/payments/captures/{cap_id}/refund",
            {"amount": {"currency_code": "USD", "value": "30.01"}},
        )
        assert over.status_code == 422
        assert over.json()["details"][0]["issue"] == "REFUND_AMOUNT_EXCEEDED"

        rest = await _post(base, f"/v2/payments/captures/{cap_id}/refund", {})
        assert rest.json()["amount"]["value"] == "30.00"

        drained = await _post(base, f"/v2/payments/captures/{cap_id}/refund", {})
        assert drained.status_code == 422
        assert drained.json()["details"][0]["issue"] == "CAPTURE_FULLY_REFUNDED"
    finally:
        await sim.stop()


# -- auth gate -----------------------------------------------------------------


async def test_orders_require_bearer():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v2/checkout/orders", _order(), headers={})
        assert resp.status_code == 401
        assert resp.json()["name"] == "AUTHENTICATION_FAILURE"
    finally:
        await sim.stop()


async def test_strict_tokens_only_accepts_issued_ones():
    sim, base = await _sim({"paypal": {"strict_tokens": True}})
    try:
        # a made-up bearer is refused
        resp = await _post(base, "/v2/checkout/orders", _order())
        assert resp.status_code == 401
        # a token from the sim's own endpoint passes
        tok = (
            await _post(
                base,
                "/v1/oauth2/token",
                headers={},
                data={"grant_type": "client_credentials"},
                auth=("cid", "shh"),
            )
        ).json()["access_token"]
        ok = await _post(
            base, "/v2/checkout/orders", _order(), headers={"Authorization": f"Bearer {tok}"}
        )
        assert ok.status_code == 201
    finally:
        await sim.stop()


# -- end-to-end through the http adapter's oauth2 strategy ---------------------


async def test_oauth2_strategy_end_to_end_via_http_adapter():
    """The shape the PayPal pack ships: the adapter fetches its token from the
    sim's own /v1/oauth2/token, attaches it as Bearer, and drives the order
    lifecycle. strict_tokens proves the token really came from the exchange."""
    sim, base = await _sim({"paypal": {"strict_tokens": True}})
    try:
        adapter = HttpAdapter(
            {
                "adapter": "http",
                "base_url": base,
                "authentication": {
                    "type": "oauth2_client_credentials",
                    "token_url": f"{base}/v1/oauth2/token",
                    "client_id": "sandbox-cid",
                    "client_secret": "sandbox-secret",
                },
                "actions": {
                    "create_order": {"method": "POST", "path": "/v2/checkout/orders"},
                    "capture_order": {
                        "method": "POST",
                        "path": "/v2/checkout/orders/${request.id}/capture",
                    },
                    "refund_capture": {
                        "method": "POST",
                        "path": "/v2/payments/captures/${request.id}/refund",
                    },
                },
            }
        )
        await adapter.connect()
        sr = await adapter.execute("create_order", _order("75.00"))
        assert sr.success, sr.error
        oid = sr.response_payload["body"]["id"]

        sr2 = await adapter.execute("capture_order", {"id": oid})
        assert sr2.success, sr2.error
        body = sr2.response_payload["body"]
        assert body["status"] == "COMPLETED"
        cap_id = body["purchase_units"][0]["payments"]["captures"][0]["id"]

        sr3 = await adapter.execute("refund_capture", {"id": cap_id})
        assert sr3.success, sr3.error
        assert sr3.response_payload["body"]["status"] == "COMPLETED"
    finally:
        await sim.stop()


async def test_stats_decision_buckets():
    sim, base = await _sim({"paypal": {"decline_over": 500}})
    try:
        oid1 = (await _post(base, "/v2/checkout/orders", _order("10.00"))).json()["id"]
        await _post(base, f"/v2/checkout/orders/{oid1}/capture", {})
        oid2 = (await _post(base, "/v2/checkout/orders", _order("900.00"))).json()["id"]
        await _post(base, f"/v2/checkout/orders/{oid2}/capture", {})
        stats = sim.stats()
        assert stats["by_response_code"].get("COMPLETED") == 1
        assert stats["by_response_code"].get("INSTRUMENT_DECLINED") == 1
        assert stats["by_response_code"].get("CREATED") == 2
        assert sim.protocol == "paypal"
    finally:
        await sim.stop()
