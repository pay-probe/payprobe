"""Opt-in Stripe *sandbox* smoke — the real external API (ADR-0009 phase 1).

Skipped unless ``STRIPE_TEST_KEY`` is set (a Stripe test-mode secret key,
``sk_test_…``). This is deliberately NOT part of the offline suite: it proves
the data plane — the generic ``http`` adapter with ``content_type: form`` and
the pack's action shapes — against Stripe's actual test mode, one lifecycle,
low volume. Load against this endpoint is refused by the ``external: true``
guardrail; this smoke is the only sanctioned real-API traffic in the repo.

Run:  STRIPE_TEST_KEY=sk_test_… pytest worker/tests/test_stripe_sandbox_smoke.py
"""

import os

import pytest

from worker.adapters.http.adapter import HttpAdapter

KEY = os.environ.get("STRIPE_TEST_KEY", "")

pytestmark = pytest.mark.skipif(
    not KEY.startswith("sk_test_"),
    reason="STRIPE_TEST_KEY not set (opt-in sandbox smoke; needs an sk_test_… key)",
)


def _adapter() -> HttpAdapter:
    # mirrors the pack's stripe_sandbox connection preset
    return HttpAdapter(
        {
            "adapter": "http",
            "base_url": "https://api.stripe.com/v1",
            "content_type": "form",
            "health_any_status": True,
            "authentication": {"type": "bearer", "token": KEY},
            "actions": {
                "create_payment_intent": {"method": "POST", "path": "/payment_intents"},
                "get_payment_intent": {
                    "method": "GET",
                    "path": "/payment_intents/${request.id}",
                },
                "create_refund": {"method": "POST", "path": "/refunds"},
            },
        }
    )


async def test_sandbox_payment_intent_lifecycle():
    adapter = _adapter()
    await adapter.connect()

    sr = await adapter.execute(
        "create_payment_intent",
        {
            "amount": 199,
            "currency": "usd",
            "confirm": True,
            "payment_method": "pm_card_visa",
            "payment_method_types": ["card"],
        },
    )
    assert sr.success, sr.error
    body = sr.response_payload["body"]
    assert body["object"] == "payment_intent"
    assert body["status"] == "succeeded"
    pid = body["id"]

    sr2 = await adapter.execute("create_refund", {"payment_intent": pid})
    assert sr2.success, sr2.error
    assert sr2.response_payload["body"]["status"] in ("succeeded", "pending")

    sr3 = await adapter.execute("get_payment_intent", {"id": pid})
    assert sr3.success, sr3.error
    assert sr3.response_payload["body"]["id"] == pid
