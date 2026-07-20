"""Adyen Checkout simulator — driven over a real HTTP socket (ADR-0009 phase 2).

Validates the public contract the simulator stands in for: the resultCode
ladder (Authorised / Refused via the documented holderName and
RequestedTestAcquirerResponseCode test triggers / ChallengeShopper + details
completion), async-style refunds (status "received"), the Adyen error envelope
and X-API-Key credential gate, version-prefix tolerance, and the duck-typed
responder surface the orchestrator + portal consume.
"""

import httpx

from worker.adapters.scheme.adyen_sim import AdyenCheckoutSimulator

KEY = {"X-API-Key": "AQEyhmfxK_test"}


async def _sim(config=None):
    sim = AdyenCheckoutSimulator({"host": "127.0.0.1", "port": 0, **(config or {})})
    port = await sim.start()
    return sim, f"http://127.0.0.1:{port}"


def _payment(value=1000, holder="J. Smith", pan="4111111111111111", **extra):
    return {
        "amount": {"currency": "EUR", "value": value},
        "reference": "ORDER-42",
        "merchantAccount": "PayProbeECOM",
        "paymentMethod": {
            "type": "scheme",
            "number": pan,
            "expiryMonth": "03",
            "expiryYear": "2030",
            "cvc": "737",
            "holderName": holder,
        },
        **extra,
    }


async def _post(base, path, json, headers=KEY):
    async with httpx.AsyncClient() as client:
        return await client.post(f"{base}{path}", json=json, headers=headers)


# -- happy path ----------------------------------------------------------------


async def test_payment_authorised():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/payments", _payment())
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["resultCode"] == "Authorised"
        assert len(doc["pspReference"]) == 16
        assert doc["amount"] == {"currency": "EUR", "value": 1000}
        assert doc["merchantReference"] == "ORDER-42"
    finally:
        await sim.stop()


async def test_version_prefix_tolerated():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/v71/payments", _payment())
        assert resp.json()["resultCode"] == "Authorised"
    finally:
        await sim.stop()


# -- documented refusal triggers -----------------------------------------------


async def test_holder_name_triggers():
    sim, base = await _sim()
    try:
        for holder, reason, code in [
            ("DECLINED", "Refused", "2"),
            ("CARD_EXPIRED", "Expired Card", "6"),
            ("NOT_ENOUGH_BALANCE", "Not enough balance", "12"),
            ("CVC_DECLINED", "CVC Declined", "24"),
        ]:
            doc = (await _post(base, "/payments", _payment(holder=holder))).json()
            assert doc["resultCode"] == "Refused", holder
            assert doc["refusalReason"] == reason
            assert doc["refusalReasonCode"] == code
    finally:
        await sim.stop()


async def test_requested_acquirer_response_code_trigger():
    sim, base = await _sim()
    try:
        body = _payment(additionalData={"RequestedTestAcquirerResponseCode": "12"})
        doc = (await _post(base, "/payments", body)).json()
        assert doc["resultCode"] == "Refused"
        assert doc["refusalReasonCode"] == "12"
    finally:
        await sim.stop()


async def test_config_decline_over():
    sim, base = await _sim({"adyen": {"decline_over": 50000}})
    try:
        ok = (await _post(base, "/payments", _payment(value=49999))).json()
        assert ok["resultCode"] == "Authorised"
        refused = (await _post(base, "/payments", _payment(value=50000))).json()
        assert refused["resultCode"] == "Refused"
        assert refused["refusalReason"] == "Not enough balance"
    finally:
        await sim.stop()


# -- 3DS challenge -------------------------------------------------------------


async def test_challenge_card_then_details_completes():
    sim, base = await _sim()
    try:
        doc = (await _post(base, "/payments", _payment(pan="4212345678901237"))).json()
        assert doc["resultCode"] == "ChallengeShopper"
        action = doc["action"]
        assert action["type"] == "threeDS2"
        token = action["paymentData"]

        done = (
            await _post(base, "/payments/details",
                        {"paymentData": token, "details": {"threeDSResult": "Y"}})
        ).json()
        assert done["resultCode"] == "Authorised"
        assert done["merchantReference"] == "ORDER-42"

        # a token completes exactly once
        again = await _post(base, "/payments/details", {"paymentData": token})
        assert again.status_code == 422
        assert again.json()["errorCode"] == "704"
    finally:
        await sim.stop()


# -- refunds -------------------------------------------------------------------


async def test_refund_received_and_unknown_reference():
    sim, base = await _sim()
    try:
        ref = (await _post(base, "/payments", _payment())).json()["pspReference"]
        resp = await _post(
            base, f"/payments/{ref}/refunds",
            {"merchantAccount": "PayProbeECOM",
             "amount": {"currency": "EUR", "value": 400}},
        )
        assert resp.status_code == 201
        doc = resp.json()
        assert doc["status"] == "received"  # Adyen refunds validate async
        assert doc["paymentPspReference"] == ref
        assert doc["pspReference"] != ref
        assert doc["amount"] == {"currency": "EUR", "value": 400}

        missing = await _post(
            base, "/payments/NOPE/refunds", {"merchantAccount": "PayProbeECOM"}
        )
        assert missing.status_code == 422
        assert missing.json()["errorCode"] == "731"
    finally:
        await sim.stop()


# -- gates + envelope ----------------------------------------------------------


async def test_api_key_gate():
    sim, base = await _sim()
    try:
        resp = await _post(base, "/payments", _payment(), headers={})
        assert resp.status_code == 401
        assert resp.json()["errorType"] == "security"
        # gate off -> anonymous passes
        await sim.stop()
        sim2, base2 = await _sim({"adyen": {"require_auth": False}})
        try:
            assert (await _post(base2, "/payments", _payment(), headers={})).status_code == 200
        finally:
            await sim2.stop()
    finally:
        await sim.stop()


async def test_merchant_account_checks():
    sim, base = await _sim({"adyen": {"merchant_account": "PayProbeECOM"}})
    try:
        body = _payment()
        body.pop("merchantAccount")
        resp = await _post(base, "/payments", body)
        assert resp.status_code == 403
        assert resp.json()["errorCode"] == "901"

        wrong = _payment()
        wrong["merchantAccount"] = "SomeoneElse"
        assert (await _post(base, "/payments", wrong)).status_code == 403
    finally:
        await sim.stop()


async def test_missing_amount_and_unknown_path():
    sim, base = await _sim()
    try:
        body = _payment()
        body.pop("amount")
        resp = await _post(base, "/payments", body)
        assert resp.status_code == 422
        assert resp.json()["errorCode"] == "100"

        assert (await _post(base, "/nope", {})).status_code == 404
    finally:
        await sim.stop()


async def test_rules_override_and_decision_buckets():
    sim, base = await _sim(
        {
            "rules": [
                {"when": {"method": "POST", "path": {"prefix": "/payments"},
                          "body": {"reference": {"eq": "CHAOS"}}},
                 "respond": {"status": 503, "json": {"errorCode": "api_down"}}}
            ]
        }
    )
    try:
        body = _payment()
        body["reference"] = "CHAOS"
        assert (await _post(base, "/payments", body)).status_code == 503

        await _post(base, "/payments", _payment())
        await _post(base, "/payments", _payment(holder="NOT_ENOUGH_BALANCE"))
        stats = sim.stats()
        assert stats["by_response_code"].get("Authorised") == 1
        assert stats["by_response_code"].get("Not enough balance") == 1
        assert sim.protocol == "adyen"
        assert sim.port is not None
    finally:
        await sim.stop()
