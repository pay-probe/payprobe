"""Stripe PaymentIntents simulator — driven over a real HTTP socket (ADR-0009).

Validates the public contract the simulator stands in for: the documented
test-card decision ladder (succeed / decline codes / 3DS requires_action), the
PaymentIntent status machine (confirm / manual capture / cancel / refund with
lifecycle checks), form-encoded request bodies with bracket notation (what
Stripe actually requires on the wire — sent here through the real ``http``
adapter with ``content_type: form``), the error envelope, the credential gate,
and the duck-typed responder surface the orchestrator + portal consume.
"""

import httpx

from worker.adapters.http.adapter import HttpAdapter
from worker.adapters.scheme.stripe_sim import StripeSimulator, _unflatten_form

AUTH = {"Authorization": "Bearer sk_test_payprobe"}


async def _sim(config=None):
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0, **(config or {})})
    port = await sim.start()
    return sim, f"http://127.0.0.1:{port}"


def _intent_form(amount=1999, pan="4242424242424242", confirm=True, **extra):
    data = {
        "amount": str(amount),
        "currency": "usd",
        "confirm": "true" if confirm else "false",
        "payment_method_data[type]": "card",
        "payment_method_data[card][number]": pan,
        "payment_method_data[card][exp_month]": "12",
        "payment_method_data[card][exp_year]": "2030",
        "payment_method_data[card][cvc]": "123",
    }
    data.update(extra)
    return data


async def _post(base, path, data, headers=AUTH):
    async with httpx.AsyncClient() as client:
        return await client.post(f"{base}{path}", data=data, headers=headers)


async def _get(base, path, headers=AUTH):
    async with httpx.AsyncClient() as client:
        return await client.get(f"{base}{path}", headers=headers)


# -- form decoding -------------------------------------------------------------


def test_unflatten_form_brackets():
    body = _unflatten_form("amount=100&card[number]=4242&items[0][price]=5")
    assert body == {
        "amount": "100",
        "card": {"number": "4242"},
        "items": {"0": {"price": "5"}},
    }


# -- happy path ----------------------------------------------------------------


async def test_create_confirm_succeeds_with_4242():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v1/payment_intents", _intent_form())
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["object"] == "payment_intent"
        assert doc["status"] == "succeeded"
        assert doc["amount"] == 1999
        assert doc["amount_received"] == 1999
        assert doc["id"].startswith("pi_")
        assert doc["latest_charge"].startswith("ch_")
        assert "client_secret" in doc
    finally:
        await sim.stop()


async def test_two_step_confirm_and_retrieve():
    sim, base = await _sim()
    try:
        created = (await _post(base, "/v1/payment_intents", _intent_form(confirm=False))).json()
        assert created["status"] == "requires_confirmation"
        pid = created["id"]
        confirmed = (await _post(base, f"/v1/payment_intents/{pid}/confirm", {})).json()
        assert confirmed["status"] == "succeeded"
        got = (await _get(base, f"/v1/payment_intents/{pid}")).json()
        assert got["status"] == "succeeded"
    finally:
        await sim.stop()


async def test_manual_capture_flow():
    sim, base = await _sim()
    try:
        doc = (
            await _post(
                base, "/v1/payment_intents", _intent_form(capture_method="manual")
            )
        ).json()
        assert doc["status"] == "requires_capture"
        assert doc["amount_received"] == 0
        pid = doc["id"]
        captured = (await _post(base, f"/v1/payment_intents/{pid}/capture", {})).json()
        assert captured["status"] == "succeeded"
        assert captured["amount_received"] == 1999
        # a second capture is an invalid state transition
        resp = await _post(base, f"/v1/payment_intents/{pid}/capture", {})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "payment_intent_unexpected_state"
    finally:
        await sim.stop()


# -- the test-card decline ladder ----------------------------------------------


async def test_generic_decline_is_402_card_error_with_embedded_intent():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v1/payment_intents", _intent_form(pan="4000000000000002"))
        assert resp.status_code == 402
        err = resp.json()["error"]
        assert err["type"] == "card_error"
        assert err["code"] == "card_declined"
        assert err["decline_code"] == "generic_decline"
        intent = err["payment_intent"]
        assert intent["status"] == "requires_payment_method"
        assert intent["last_payment_error"]["decline_code"] == "generic_decline"
    finally:
        await sim.stop()


async def test_insufficient_funds_and_expired_and_cvc():
    sim, base = await _sim()
    try:
        for pan, code, decline in [
            ("4000000000009995", "card_declined", "insufficient_funds"),
            ("4000000000000069", "expired_card", None),
            ("4000000000000127", "incorrect_cvc", None),
        ]:
            err = (await _post(base, "/v1/payment_intents", _intent_form(pan=pan))).json()[
                "error"
            ]
            assert err["code"] == code
            assert err.get("decline_code") == decline
    finally:
        await sim.stop()


async def test_3ds_card_parks_in_requires_action():
    sim, base = await _sim()
    try:
        doc = (
            await _post(base, "/v1/payment_intents", _intent_form(pan="4000002500003155"))
        ).json()
        assert doc["status"] == "requires_action"
        assert doc["next_action"] == {"type": "use_stripe_sdk"}
    finally:
        await sim.stop()


async def test_pm_token_mapping():
    sim, base = await _sim()
    try:
        data = {
            "amount": "500",
            "currency": "usd",
            "confirm": "true",
            "payment_method": "pm_card_chargeDeclinedInsufficientFunds",
        }
        err = (await _post(base, "/v1/payment_intents", data)).json()["error"]
        assert err["decline_code"] == "insufficient_funds"

        data["payment_method"] = "pm_card_visa"
        doc = (await _post(base, "/v1/payment_intents", data)).json()
        assert doc["status"] == "succeeded"
    finally:
        await sim.stop()


async def test_config_decline_over_amount():
    sim, base = await _sim({"stripe": {"decline_over": 100000}})
    try:
        ok = (await _post(base, "/v1/payment_intents", _intent_form(amount=99999))).json()
        assert ok["status"] == "succeeded"
        err = (await _post(base, "/v1/payment_intents", _intent_form(amount=100000))).json()[
            "error"
        ]
        assert err["decline_code"] == "insufficient_funds"
    finally:
        await sim.stop()


# -- refunds -------------------------------------------------------------------


async def test_refund_full_partial_and_over():
    sim, base = await _sim()
    try:
        pid = (await _post(base, "/v1/payment_intents", _intent_form(amount=1000))).json()["id"]
        part = (await _post(base, "/v1/refunds", {"payment_intent": pid, "amount": "400"})).json()
        assert part["object"] == "refund"
        assert part["status"] == "succeeded"
        assert part["amount"] == 400
        assert part["id"].startswith("re_")
        # remaining 600 refunds by default
        rest = (await _post(base, "/v1/refunds", {"payment_intent": pid})).json()
        assert rest["amount"] == 600
        # nothing left -> amount_too_large
        resp = await _post(base, "/v1/refunds", {"payment_intent": pid, "amount": "1"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "amount_too_large"
        # refunds are retrievable
        got = (await _get(base, f"/v1/refunds/{part['id']}")).json()
        assert got["amount"] == 400
    finally:
        await sim.stop()


async def test_refund_of_uncaptured_intent_refused():
    sim, base = await _sim()
    try:
        pid = (
            await _post(
                base, "/v1/payment_intents", _intent_form(capture_method="manual")
            )
        ).json()["id"]
        resp = await _post(base, "/v1/refunds", {"payment_intent": pid})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "charge_not_captured"
    finally:
        await sim.stop()


# -- lifecycle + errors --------------------------------------------------------


async def test_cancel_and_confirm_after_terminal_state():
    sim, base = await _sim()
    try:
        pid = (await _post(base, "/v1/payment_intents", _intent_form(confirm=False))).json()["id"]
        canceled = (await _post(base, f"/v1/payment_intents/{pid}/cancel", {})).json()
        assert canceled["status"] == "canceled"
        resp = await _post(base, f"/v1/payment_intents/{pid}/confirm", {})
        assert resp.status_code == 400
        # a succeeded intent cannot be canceled
        pid2 = (await _post(base, "/v1/payment_intents", _intent_form())).json()["id"]
        resp2 = await _post(base, f"/v1/payment_intents/{pid2}/cancel", {})
        assert resp2.status_code == 400
    finally:
        await sim.stop()


async def test_unknown_intent_is_resource_missing_404():
    sim, base = await _sim()
    try:
        resp = await _get(base, "/v1/payment_intents/pi_nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "resource_missing"
    finally:
        await sim.stop()


async def test_missing_amount_is_parameter_missing():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v1/payment_intents", {"currency": "usd"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "parameter_missing"
    finally:
        await sim.stop()


async def test_auth_gate_401_without_key():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v1/payment_intents", _intent_form(), headers={})
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "invalid_request_error"
        # gate off -> anonymous calls pass
        await sim.stop()
        sim2, base2 = await _sim({"stripe": {"require_auth": False}})
        try:
            resp2 = await _post(base2, "/v1/payment_intents", _intent_form(), headers={})
            assert resp2.status_code == 200
        finally:
            await sim2.stop()
    finally:
        await sim.stop()


async def test_json_body_tolerated():
    sim, base = await _sim()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/v1/payment_intents",
                json={
                    "amount": 700,
                    "currency": "usd",
                    "confirm": True,
                    "payment_method_data": {"card": {"number": "4242424242424242"}},
                },
                headers=AUTH,
            )
        assert resp.json()["status"] == "succeeded"
    finally:
        await sim.stop()


async def test_rules_override_gateway_logic():
    sim, base = await _sim(
        {
            "rules": [
                {
                    "when": {"method": "POST", "path": {"prefix": "/v1/payment_intents"},
                             "body": {"amount": {"eq": "13"}}},
                    "respond": {"status": 503, "json": {"error": {"type": "api_error"}}},
                }
            ]
        }
    )
    try:
        resp = await _post(base, "/v1/payment_intents", _intent_form(amount=13))
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "api_error"
    finally:
        await sim.stop()


# -- driven through the real http adapter (the packed scenarios' path) ---------


async def test_end_to_end_via_http_adapter_form_encoding():
    sim, base = await _sim()
    try:
        adapter = HttpAdapter(
            {
                "adapter": "http",
                "base_url": base,
                "content_type": "form",
                "headers": {"Authorization": "Bearer sk_test_payprobe"},
                "actions": {
                    "create_payment_intent": {"method": "POST", "path": "/v1/payment_intents"},
                    "create_refund": {"method": "POST", "path": "/v1/refunds"},
                    "get_payment_intent": {
                        "method": "GET",
                        "path": "/v1/payment_intents/${request.id}",
                    },
                },
            }
        )
        await adapter.connect()
        sr = await adapter.execute(
            "create_payment_intent",
            {
                "amount": 2500,
                "currency": "usd",
                "confirm": True,
                "payment_method_data": {"card": {"number": "4242424242424242"}},
            },
        )
        assert sr.success
        body = sr.response_payload["body"]
        assert body["status"] == "succeeded"

        sr2 = await adapter.execute(
            "create_refund", {"payment_intent": body["id"], "amount": 500}
        )
        assert sr2.success
        assert sr2.response_payload["body"]["amount"] == 500

        sr3 = await adapter.execute("get_payment_intent", {"id": body["id"]})
        assert sr3.success
        assert sr3.response_payload["body"]["id"] == body["id"]
    finally:
        await sim.stop()


# -- responder surface (orchestrator/portal contract) --------------------------


async def test_stats_peers_and_decision_buckets():
    sim, base = await _sim()
    try:
        await _post(base, "/v1/payment_intents", _intent_form())
        await _post(base, "/v1/payment_intents", _intent_form(pan="4000000000009995"))
        assert sim.port is not None
        assert sim.protocol == "stripe"
        stats = sim.stats()
        assert stats["received"] == 2
        assert "POST /v1/payment_intents" in stats["by_mti"]
        assert stats["by_response_code"].get("succeeded") == 1
        assert stats["by_response_code"].get("insufficient_funds") == 1
        assert sim.peers()  # at least the test client's session row
    finally:
        await sim.stop()


async def test_chaos_drop_applies():
    sim, base = await _sim({"chaos": {"drop_pct": 100}})
    try:
        try:
            resp = await _post(base, "/v1/payment_intents", _intent_form())
            assert resp.status_code == 504  # dropped reply surfaces as 504
        except httpx.HTTPError:
            pass  # force-closed connection is an acceptable drop symptom
        assert sim.faults == 1
    finally:
        await sim.stop()
