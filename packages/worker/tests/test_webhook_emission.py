"""Simulator webhook emission — the provider calls the merchant back
(ADR-0009 phase 4).

Receiver = a plain HttpResponder capturing requests. Each provider simulator
emits on its state transitions with its provider-correct signature scheme,
which these tests verify by **recomputing** it over the raw received body:
Stripe's ``t=…,v1=HMAC`` header, Adyen's colon-joined NotificationRequestItem
HMAC (hex key, base64), PayPal's transmission headers (HMAC stand-in,
honestly labelled). Also covered: the events filter, chaos on the emission
leg (drop counts, malformed breaks the signature *after* signing), delivery
failure counting, and that the reply path is never delayed by a dead webhook
endpoint.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time

import httpx

from worker.adapters.http.responder import HttpResponder
from worker.adapters.http.webhooks import (
    WebhookEmitter,
    adyen_hmac_signature,
    stripe_signature,
)
from worker.adapters.scheme.adyen_sim import AdyenCheckoutSimulator
from worker.adapters.scheme.paypal_sim import PayPalOrdersSimulator
from worker.adapters.scheme.stripe_sim import StripeSimulator

AUTH = {"Authorization": "Bearer sk_test_payprobe"}
ADYEN_KEY = {"X-API-Key": "AQEyhmfxK_test"}
#: Adyen HMAC keys are hex strings in the real Customer Area
ADYEN_HMAC_HEX = binascii.hexlify(b"payprobe-hmac-key").decode()


async def _receiver():
    r = HttpResponder({"host": "127.0.0.1", "port": 0,
                       "default": {"status": 200, "json": {"received": True}}})
    port = await r.start()
    return r, f"http://127.0.0.1:{port}"


async def _post(base, path, *, json_body=None, data=None, headers=None):
    async with httpx.AsyncClient() as client:
        return await client.post(f"{base}{path}", json=json_body, data=data,
                                 headers=headers or {})


def _stripe_form(amount=1999, pan="4242424242424242", **extra):
    d = {
        "amount": str(amount), "currency": "usd", "confirm": "true",
        "payment_method_data[card][number]": pan,
    }
    d.update(extra)
    return d


# -- Stripe --------------------------------------------------------------------


async def test_stripe_succeeded_event_with_valid_signature():
    receiver, hook_url = await _receiver()
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": f"{hook_url}/webhooks/stripe",
                                        "secret": "whsec_test"}})
    port = await sim.start()
    try:
        resp = await _post(f"http://127.0.0.1:{port}", "/v1/payment_intents",
                           data=_stripe_form(), headers=AUTH)
        assert resp.json()["status"] == "succeeded"
        await sim.webhooks.drain()

        assert len(receiver.received) == 1
        hook = receiver.received[0]
        assert hook["path"] == "/webhooks/stripe"
        event = hook["body"]
        assert event["object"] == "event"
        assert event["type"] == "payment_intent.succeeded"
        assert event["data"]["object"]["id"] == resp.json()["id"]

        # recompute the documented signature over the RAW body
        sig = hook["headers"]["stripe-signature"]
        t = int(sig.split(",")[0].split("=")[1])
        v1 = sig.split("v1=")[1]
        expected = hmac.new(b"whsec_test", f"{t}.{hook['raw']}".encode(),
                            hashlib.sha256).hexdigest()
        assert v1 == expected
        assert abs(time.time() - t) < 30

        assert sim.stats()["webhooks"]["sent"] == 1
    finally:
        await sim.stop()
        await receiver.stop()


async def test_stripe_lifecycle_event_types():
    receiver, hook_url = await _receiver()
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": hook_url, "secret": "s"}})
    port = await sim.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # decline -> payment_failed
        await _post(base, "/v1/payment_intents",
                    data=_stripe_form(pan="4000000000009995"), headers=AUTH)
        # manual capture -> amount_capturable_updated, then succeeded
        pid = (await _post(base, "/v1/payment_intents",
                           data=_stripe_form(capture_method="manual"),
                           headers=AUTH)).json()["id"]
        await _post(base, f"/v1/payment_intents/{pid}/capture", data={}, headers=AUTH)
        # refund -> refund.created
        await _post(base, "/v1/refunds",
                    data={"payment_intent": pid, "amount": "100"}, headers=AUTH)
        await sim.webhooks.drain()

        types = [r["body"]["type"] for r in receiver.received]
        assert types == [
            "payment_intent.payment_failed",
            "payment_intent.amount_capturable_updated",
            "payment_intent.succeeded",
            "refund.created",
        ]
    finally:
        await sim.stop()
        await receiver.stop()


# -- Adyen ---------------------------------------------------------------------


async def test_adyen_notification_with_valid_hmac():
    receiver, hook_url = await _receiver()
    sim = AdyenCheckoutSimulator({"host": "127.0.0.1", "port": 0,
                                  "webhooks": {"url": hook_url,
                                               "secret": ADYEN_HMAC_HEX}})
    port = await sim.start()
    base = f"http://127.0.0.1:{port}"
    payment = {
        "amount": {"currency": "EUR", "value": 1000},
        "reference": "ORDER-7", "merchantAccount": "PayProbeECOM",
        "paymentMethod": {"type": "scheme", "number": "4111111111111111",
                          "holderName": "J. Smith"},
    }
    try:
        ref = (await _post(base, "/payments", json_body=payment,
                           headers=ADYEN_KEY)).json()["pspReference"]
        await _post(base, f"/payments/{ref}/refunds",
                    json_body={"merchantAccount": "PayProbeECOM",
                               "amount": {"currency": "EUR", "value": 300}},
                    headers=ADYEN_KEY)
        await sim.webhooks.drain()

        assert len(receiver.received) == 2
        auth_item = receiver.received[0]["body"]["notificationItems"][0][
            "NotificationRequestItem"]
        assert auth_item["eventCode"] == "AUTHORISATION"
        assert auth_item["success"] == "true"
        assert auth_item["pspReference"] == ref
        assert auth_item["merchantAccountCode"] == "PayProbeECOM"

        # recompute the documented colon-joined HMAC (hex key, base64 digest)
        payload = ":".join([
            auth_item["pspReference"], auth_item["originalReference"],
            auth_item["merchantAccountCode"], auth_item["merchantReference"],
            "1000", "EUR", "AUTHORISATION", "true",
        ])
        expected = base64.b64encode(
            hmac.new(binascii.unhexlify(ADYEN_HMAC_HEX), payload.encode(),
                     hashlib.sha256).digest()
        ).decode()
        assert auth_item["additionalData"]["hmacSignature"] == expected
        # helper agrees with the hand-rolled computation
        assert adyen_hmac_signature(ADYEN_HMAC_HEX, auth_item) == expected

        refund_item = receiver.received[1]["body"]["notificationItems"][0][
            "NotificationRequestItem"]
        assert refund_item["eventCode"] == "REFUND"
        assert refund_item["originalReference"] == ref
        assert refund_item["amount"] == {"currency": "EUR", "value": 300}
    finally:
        await sim.stop()
        await receiver.stop()


async def test_adyen_refusal_notification_carries_reason():
    receiver, hook_url = await _receiver()
    sim = AdyenCheckoutSimulator({"host": "127.0.0.1", "port": 0,
                                  "webhooks": {"url": hook_url,
                                               "secret": ADYEN_HMAC_HEX}})
    port = await sim.start()
    try:
        await _post(f"http://127.0.0.1:{port}", "/payments", json_body={
            "amount": {"currency": "EUR", "value": 1000},
            "reference": "R1", "merchantAccount": "PayProbeECOM",
            "paymentMethod": {"type": "scheme", "number": "4111111111111111",
                              "holderName": "NOT_ENOUGH_BALANCE"},
        }, headers=ADYEN_KEY)
        await sim.webhooks.drain()
        item = receiver.received[0]["body"]["notificationItems"][0][
            "NotificationRequestItem"]
        assert item["success"] == "false"
        assert item["reason"] == "Not enough balance"
    finally:
        await sim.stop()
        await receiver.stop()


# -- PayPal --------------------------------------------------------------------


async def test_paypal_capture_and_refund_events_with_transmission_headers():
    receiver, hook_url = await _receiver()
    sim = PayPalOrdersSimulator({"host": "127.0.0.1", "port": 0,
                                 "webhooks": {"url": hook_url, "secret": "pp"}})
    port = await sim.start()
    base = f"http://127.0.0.1:{port}"
    bearer = {"Authorization": "Bearer A21.t"}
    order = {"intent": "CAPTURE",
             "purchase_units": [{"amount": {"currency_code": "USD",
                                            "value": "42.00"}}]}
    try:
        oid = (await _post(base, "/v2/checkout/orders", json_body=order,
                           headers=bearer)).json()["id"]
        cap = (await _post(base, f"/v2/checkout/orders/{oid}/capture",
                           json_body={}, headers=bearer)).json()
        cap_id = cap["purchase_units"][0]["payments"]["captures"][0]["id"]
        await _post(base, f"/v2/payments/captures/{cap_id}/refund",
                    json_body={}, headers=bearer)
        await sim.webhooks.drain()

        assert len(receiver.received) == 2
        completed = receiver.received[0]
        event = completed["body"]
        assert event["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
        assert event["resource"]["id"] == cap_id
        assert event["resource"]["amount"]["value"] == "42.00"

        heads = completed["headers"]
        assert heads["paypal-auth-algo"] == "HMAC-SHA256"  # honest stand-in label
        signed = (f"{heads['paypal-transmission-id']}|"
                  f"{heads['paypal-transmission-time']}|{completed['raw']}")
        expected = hmac.new(b"pp", signed.encode(), hashlib.sha256).hexdigest()
        assert heads["paypal-transmission-sig"] == expected

        assert receiver.received[1]["body"]["event_type"] == "PAYMENT.CAPTURE.REFUNDED"
    finally:
        await sim.stop()
        await receiver.stop()


# -- emitter behaviour ---------------------------------------------------------


async def test_events_filter():
    receiver, hook_url = await _receiver()
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": hook_url, "secret": "s",
                                        "events": ["refund.created"]}})
    port = await sim.start()
    base = f"http://127.0.0.1:{port}"
    try:
        pid = (await _post(base, "/v1/payment_intents", data=_stripe_form(),
                           headers=AUTH)).json()["id"]
        await _post(base, "/v1/refunds", data={"payment_intent": pid},
                    headers=AUTH)
        await sim.webhooks.drain()
        assert [r["body"]["type"] for r in receiver.received] == ["refund.created"]
    finally:
        await sim.stop()
        await receiver.stop()


async def test_chaos_drop_on_the_emission_leg():
    receiver, hook_url = await _receiver()
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": hook_url, "secret": "s",
                                        "chaos": {"drop_pct": 100}}})
    port = await sim.start()
    try:
        resp = await _post(f"http://127.0.0.1:{port}", "/v1/payment_intents",
                           data=_stripe_form(), headers=AUTH)
        assert resp.status_code == 200  # the reply path is untouched
        await sim.webhooks.drain()
        assert receiver.received == []
        wh = sim.stats()["webhooks"]
        assert wh["dropped"] == 1
        assert wh["sent"] == 0
        assert wh["recent"][-1]["status"] == "(dropped)"
    finally:
        await sim.stop()
        await receiver.stop()


async def test_chaos_malformed_breaks_the_signature_after_signing():
    receiver, hook_url = await _receiver()
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": hook_url, "secret": "whsec_x",
                                        "chaos": {"malformed_pct": 100}}})
    port = await sim.start()
    try:
        await _post(f"http://127.0.0.1:{port}", "/v1/payment_intents",
                    data=_stripe_form(), headers=AUTH)
        await sim.webhooks.drain()
        hook = receiver.received[0]
        sig = hook["headers"]["stripe-signature"]
        t = int(sig.split(",")[0].split("=")[1])
        v1 = sig.split("v1=")[1]
        recomputed = hmac.new(b"whsec_x", f"{t}.{hook['raw']}".encode(),
                              hashlib.sha256).hexdigest()
        assert v1 != recomputed  # wire corruption -> a verifying receiver rejects
    finally:
        await sim.stop()
        await receiver.stop()


async def test_dead_endpoint_counts_failed_and_never_delays_replies():
    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": "http://127.0.0.1:1", "secret": "s",
                                        "timeout_sec": 1}})
    port = await sim.start()
    try:
        start = time.monotonic()
        resp = await _post(f"http://127.0.0.1:{port}", "/v1/payment_intents",
                           data=_stripe_form(), headers=AUTH)
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        assert elapsed < 0.9  # fire-and-forget: reply not held for the delivery
        await sim.webhooks.drain()
        wh = sim.stats()["webhooks"]
        assert wh["failed"] == 1
        assert wh["sent"] == 0
    finally:
        await sim.stop()


async def test_emitter_receiver_4xx_counts_failed():
    receiver = HttpResponder({"host": "127.0.0.1", "port": 0,
                              "default": {"status": 500, "json": {}}})
    port = await receiver.start()
    emitter = WebhookEmitter({"url": f"http://127.0.0.1:{port}", "secret": "s"})
    try:
        emitter.emit("x.y", json.dumps({"a": 1}), {})
        await emitter.drain()
        assert emitter.failed == 1
        assert emitter.sent == 0
        assert emitter.log[-1]["status"] == 500
    finally:
        await receiver.stop()


def test_signature_helpers_are_deterministic():
    assert stripe_signature("k", "{}", 123) == stripe_signature("k", "{}", 123)
    item = {"pspReference": "A", "originalReference": "", "merchantAccountCode": "M",
            "merchantReference": "r:1", "amount": {"value": 5, "currency": "EUR"},
            "eventCode": "AUTHORISATION", "success": "true"}
    sig = adyen_hmac_signature("6b6579", item)  # hex for b"key"
    payload = "A::M:r\\:1:5:EUR:AUTHORISATION:true"  # ':' escaped per the docs
    assert sig == base64.b64encode(
        hmac.new(b"key", payload.encode(), hashlib.sha256).digest()).decode()
