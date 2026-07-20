"""Test-case packs — curated, installable regression suites per scheme/processor.

A *pack* bundles a set of cases, each mapping a certification requirement to a
runnable scenario. Installing a pack imports its scenarios into a new project so
you can run them as a suite; a certification report (in the orchestrator) then
scores a run against the pack's cases.

The built-in packs here use the same mock-friendly targets as the bundled
example scenarios, so they pass end-to-end in mock mode out of the box — and
therefore make a meaningful (runnable) certification baseline.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PackCase(BaseModel):
    id: str                      # stable case id (== scenario name)
    requirement: str = ""        # human requirement this case proves
    scenario: dict               # a full scenario document (ScenarioDraft shape)


class Pack(BaseModel):
    id: str
    scheme: str = "generic"      # visa | mastercard | generic | <processor>
    label: str
    description: str = ""
    version: str = "1"
    cases: list[PackCase] = Field(default_factory=list)
    #: Provider packs (ADR-0009) also ship the config their cases run against.
    #: Each entry is create-only on install — an existing document with the
    #: same name is never clobbered (the user's edits win).
    connections: list[dict] = Field(default_factory=list)   # {name, ...ConnectionDraft}
    card_pools: list[dict] = Field(default_factory=list)    # {name, ...CardPoolDraft}
    #: Merchant-side webhook receiver flows (ADR-0009 phase 4): minimal
    #: trigger→reply participant flows bound to the pack's inbound
    #: ``merchant_webhooks_*`` connection, so "point the simulator's webhooks
    #: at a merchant" works out of the box. Create-only **by id**.
    participant_flows: list[dict] = Field(default_factory=list)  # {id, ...ParticipantFlowDraft}
    #: The built-in mock gate (``test_pack_scenarios_pass_in_mock``) only
    #: applies to packs whose cases are meaningful against MockAdapter.
    #: Provider packs assert on provider-shaped responses, so their runnable
    #: baseline is the matching provider *simulator* instead (see
    #: ``test_stripe_pack_against_simulator``) — never the real sandbox.
    mock_runnable: bool = True


def _assert(field: str, operator: str, expected=None) -> dict:
    a = {"field": field, "operator": operator}
    if expected is not None:
        a["expected"] = expected
    return a


def _step(sid: str, target: str, action: str, payload: dict | None = None,
          assertions: list | None = None, config: dict | None = None) -> dict:
    step = {"id": sid, "kind": "action", "target": target, "action": action,
            "payload": payload or {}, "assertions": assertions or []}
    if config:
        step["config"] = config
    return step


def _chain(*ids: str) -> list[dict]:
    return [{"source": a, "source_port": "out", "target": b}
            for a, b in zip(ids, ids[1:])]


# --------------------------------------------------------------------------- #
# Built-in packs
# --------------------------------------------------------------------------- #

_AUTH_SETTLE = {
    "name": "pack_auth_then_settled", "test_class": "e2e",
    "description": "Authorization is reflected in the downstream record.",
    "steps": [
        _step("auth", "http", "send_auth_request", {"amount": 10000},
              [_assert("response_code", "eq", "00")]),
        _step("settle", "db_probe_core", "query_transaction",
              {"rrn": "${auth.response.rrn}"},
              [_assert("status", "eq", "APPROVED"), _assert("amount", "eq", 10000)]),
    ],
    "edges": _chain("auth", "settle"),
}

_ECHO = {
    "name": "pack_network_echo", "test_class": "component",
    "description": "Host reachable — network echo succeeds.",
    "steps": [
        _step("echo", "http", "echo_test", {}, [_assert("status", "eq", "ok")]),
    ],
}


# --------------------------------------------------------------------------- #
# Stripe provider pack (ADR-0009 phase 1)
# --------------------------------------------------------------------------- #
#
# Cases run against the **Stripe simulator** connection (installed with the
# pack; matching saved-simulator preset kind "stripe", default port 8085).
# The sandbox connection is installed alongside, marked ``external: true`` so
# the load engine refuses it — scenarios/functional runs may re-point at it by
# swapping the step connection once a test key is set on it.

_STRIPE_ACTIONS = {
    "create_payment_intent": {"method": "POST", "path": "/payment_intents"},
    "create_payment_intent_expecting_decline": {
        "method": "POST", "path": "/payment_intents",
        # a provider decline (HTTP 402) is the outcome under test, not an error
        "accept_statuses": [200, 402],
    },
    "confirm_payment_intent": {"method": "POST", "path": "/payment_intents/${request.id}/confirm"},
    "capture_payment_intent": {"method": "POST", "path": "/payment_intents/${request.id}/capture"},
    "cancel_payment_intent": {"method": "POST", "path": "/payment_intents/${request.id}/cancel"},
    "get_payment_intent": {"method": "GET", "path": "/payment_intents/${request.id}"},
    "create_refund": {"method": "POST", "path": "/refunds"},
}

_STRIPE_SIM_CONN = "stripe_simulator"


def _stripe_card(number: str) -> dict:
    return {
        "type": "card",
        "card": {"number": number, "exp_month": 12, "exp_year": 2033, "cvc": "123"},
    }


def _stripe_intent(amount: int, number: str, **extra) -> dict:
    return {
        "amount": amount, "currency": "usd", "confirm": True,
        "payment_method_data": _stripe_card(number), **extra,
    }


_STRIPE_CONN_CFG = {"connection": _STRIPE_SIM_CONN}

_STRIPE_AUTH_REFUND = {
    "name": "stripe_auth_then_refund", "test_class": "e2e",
    "description": "A confirmed PaymentIntent succeeds and is partially refundable.",
    "steps": [
        _step("auth", "http", "create_payment_intent",
              _stripe_intent(2500, "4242424242424242"),
              [_assert("status_code", "eq", 200),
               _assert("body.status", "eq", "succeeded"),
               _assert("body.amount_received", "eq", 2500),
               _assert("body.latest_charge", "present")],
              config=_STRIPE_CONN_CFG),
        _step("refund", "http", "create_refund",
              {"payment_intent": "${auth.response.body.id}", "amount": 500},
              [_assert("body.status", "eq", "succeeded"),
               _assert("body.amount", "eq", 500)],
              config=_STRIPE_CONN_CFG),
    ],
    "edges": _chain("auth", "refund"),
}

_STRIPE_MANUAL_CAPTURE = {
    "name": "stripe_manual_capture", "test_class": "e2e",
    "description": "capture_method=manual parks the approval; capture completes it.",
    "steps": [
        _step("auth", "http", "create_payment_intent",
              _stripe_intent(1000, "4242424242424242", capture_method="manual"),
              [_assert("body.status", "eq", "requires_capture"),
               _assert("body.amount_received", "eq", 0)],
              config=_STRIPE_CONN_CFG),
        _step("capture", "http", "capture_payment_intent",
              {"id": "${auth.response.body.id}"},
              [_assert("body.status", "eq", "succeeded"),
               _assert("body.amount_received", "eq", 1000)],
              config=_STRIPE_CONN_CFG),
    ],
    "edges": _chain("auth", "capture"),
}

_STRIPE_DECLINE = {
    "name": "stripe_decline_insufficient_funds", "test_class": "e2e",
    "description": "The documented insufficient-funds test card declines with "
                   "a card_error the merchant can branch on.",
    "steps": [
        _step("decline", "http", "create_payment_intent_expecting_decline",
              _stripe_intent(2500, "4000000000009995"),
              [_assert("status_code", "eq", 402),
               _assert("body.error.type", "eq", "card_error"),
               _assert("body.error.code", "eq", "card_declined"),
               _assert("body.error.decline_code", "eq", "insufficient_funds"),
               _assert("body.error.payment_intent.status", "eq",
                       "requires_payment_method")],
              config=_STRIPE_CONN_CFG),
    ],
}

_STRIPE_3DS = {
    "name": "stripe_3ds_requires_action", "test_class": "e2e",
    "description": "A 3DS test card parks the intent in requires_action with a "
                   "next_action for the merchant to complete.",
    "steps": [
        _step("auth", "http", "create_payment_intent",
              _stripe_intent(2500, "4000002500003155"),
              [_assert("body.status", "eq", "requires_action"),
               _assert("body.next_action.type", "eq", "use_stripe_sdk")],
              config=_STRIPE_CONN_CFG),
    ],
}




# --------------------------------------------------------------------------- #
# Merchant webhook receivers (ADR-0009 phase 4)
# --------------------------------------------------------------------------- #
#
# Each provider pack ships an inbound (server-mode) http connection plus a
# minimal trigger→reply participant flow bound to it, so the simulator's
# ``webhooks.url`` has a merchant to call the moment the pack is installed.
# Ports are fixed (9101/9102/9103) so the sim presets can point at them.
# The Adyen receiver honours the documented ``[accepted]`` response contract.


def _webhook_receiver_conn(provider: str, port: int) -> dict:
    return {
        "name": f"merchant_webhooks_{provider}",
        "adapter": "http",
        "mode": "inbound",
        "description": f"Merchant-side webhook receiver for the {provider} "
                       "simulator (ADR-0009 phase 4). The pack's receiver flow "
                       "listens here; point the simulator's webhooks.url at it.",
        "host": "127.0.0.1",
        "port": port,
    }


def _webhook_receiver_flow(provider: str, reply_payload: dict) -> dict:
    return {
        "id": f"merchant_webhooks_{provider}",
        "name": f"Merchant webhooks ({provider})",
        "description": f"Acknowledges {provider} webhook deliveries — the "
                       "minimal merchant: receive, 2xx. Extend with logic "
                       "nodes to test verification / retries / dedup.",
        "trigger": {"connection": f"merchant_webhooks_{provider}"},
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "ack", "kind": "reply", "payload": reply_payload},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "ack"}],
    }


_STRIPE_WEBHOOK_CONN = _webhook_receiver_conn("stripe", 9101)
_STRIPE_WEBHOOK_FLOW = _webhook_receiver_flow("stripe", {"status": 200, "json": {"received": True}})
_ADYEN_WEBHOOK_CONN = _webhook_receiver_conn("adyen", 9102)
#: Adyen's documented webhook acknowledgement is the literal body ``[accepted]``.
_ADYEN_WEBHOOK_FLOW = _webhook_receiver_flow("adyen", {"status": 200, "body": "[accepted]"})
_PAYPAL_WEBHOOK_CONN = _webhook_receiver_conn("paypal", 9103)
_PAYPAL_WEBHOOK_FLOW = _webhook_receiver_flow("paypal", {"status": 200, "json": {}})


# --------------------------------------------------------------------------- #
# Adyen provider pack (ADR-0009 phase 2)
# --------------------------------------------------------------------------- #
#
# Cases run against the **Adyen Checkout simulator** connection (saved-simulator
# preset kind "adyen", default port 8086); the checkout-test sandbox connection
# installs alongside, marked ``external: true`` so the load engine refuses it.

_ADYEN_ACTIONS = {
    "create_payment": {"method": "POST", "path": "/payments"},
    "submit_payment_details": {"method": "POST", "path": "/payments/details"},
    "refund_payment": {"method": "POST", "path": "/payments/${request.ref}/refunds",
                       "accept_statuses": [200, 201]},
}

_ADYEN_SIM_CONN = "adyen_simulator"
_ADYEN_CONN_CFG = {"connection": _ADYEN_SIM_CONN}
_ADYEN_MERCHANT = "PayProbeECOM"


def _adyen_payment(value: int, number: str, holder: str = "J. Smith", **extra) -> dict:
    return {
        "amount": {"currency": "EUR", "value": value},
        "reference": "PACK-${seq}",
        "merchantAccount": _ADYEN_MERCHANT,
        "paymentMethod": {"type": "scheme", "number": number,
                          "holderName": holder},
        **extra,
    }


_ADYEN_AUTH_REFUND = {
    "name": "adyen_auth_then_refund", "test_class": "e2e",
    "description": "An authorised payment is refundable (async accept).",
    "steps": [
        _step("auth", "http", "create_payment",
              _adyen_payment(1000, "4111111111111111"),
              [_assert("status_code", "eq", 200),
               _assert("body.resultCode", "eq", "Authorised"),
               _assert("body.pspReference", "present")],
              config=_ADYEN_CONN_CFG),
        _step("refund", "http", "refund_payment",
              {"ref": "${auth.response.body.pspReference}",
               "merchantAccount": _ADYEN_MERCHANT,
               "amount": {"currency": "EUR", "value": 300}},
              [_assert("body.status", "eq", "received"),
               _assert("body.paymentPspReference", "present")],
              config=_ADYEN_CONN_CFG),
    ],
    "edges": _chain("auth", "refund"),
}

_ADYEN_REFUSAL = {
    "name": "adyen_refusal_reason", "test_class": "e2e",
    "description": "The documented holderName trigger refuses with an "
                   "actionable refusalReason (HTTP 200 — the verdict is "
                   "resultCode, exactly like the real test environment).",
    "steps": [
        _step("refused", "http", "create_payment",
              _adyen_payment(1000, "4111111111111111",
                             holder="NOT_ENOUGH_BALANCE"),
              [_assert("status_code", "eq", 200),
               _assert("body.resultCode", "eq", "Refused"),
               _assert("body.refusalReason", "eq", "Not enough balance")],
              config=_ADYEN_CONN_CFG),
    ],
}

_ADYEN_3DS2 = {
    "name": "adyen_3ds2_challenge", "test_class": "e2e",
    "description": "The 3DS2 test card parks in ChallengeShopper; submitting "
                   "details completes the authorisation.",
    "steps": [
        _step("challenge", "http", "create_payment",
              _adyen_payment(1000, "4212345678901237"),
              [_assert("body.resultCode", "eq", "ChallengeShopper"),
               _assert("body.action.type", "eq", "threeDS2")],
              config=_ADYEN_CONN_CFG),
        _step("complete", "http", "submit_payment_details",
              {"paymentData": "${challenge.response.body.action.paymentData}"},
              [_assert("body.resultCode", "eq", "Authorised")],
              config=_ADYEN_CONN_CFG),
    ],
    "edges": _chain("challenge", "complete"),
}


# --------------------------------------------------------------------------- #
# PayPal provider pack (ADR-0009 phase 2)
# --------------------------------------------------------------------------- #
#
# Cases run against the **PayPal v2 Orders simulator** connection
# (saved-simulator preset kind "paypal", default port 8087); the sandbox
# connection (OAuth2 client-credentials via the shared http runner) installs
# alongside, ``external: true``.

_PAYPAL_ACTIONS = {
    "create_order": {"method": "POST", "path": "/v2/checkout/orders",
                     "accept_statuses": [200, 201]},
    "capture_order": {"method": "POST",
                      "path": "/v2/checkout/orders/${request.id}/capture",
                      "accept_statuses": [200, 201]},
    # PayPal's documented negative-testing header — forces the business error
    # on the mutating call (works against the real sandbox too; ADR-0009).
    "capture_order_expecting_decline": {
        "method": "POST",
        "path": "/v2/checkout/orders/${request.id}/capture",
        "accept_statuses": [200, 201, 422],
        "headers": {"PayPal-Mock-Response":
                    '{"mock_application_codes": "INSTRUMENT_DECLINED"}'},
    },
    "refund_capture": {"method": "POST",
                       "path": "/v2/payments/captures/${request.id}/refund",
                       "accept_statuses": [200, 201]},
}

_PAYPAL_SIM_CONN = "paypal_simulator"
_PAYPAL_CONN_CFG = {"connection": _PAYPAL_SIM_CONN}

_PAYPAL_ORDER = {
    "intent": "CAPTURE",
    "purchase_units": [{"amount": {"currency_code": "USD", "value": "42.00"}}],
}

_PAYPAL_CAPTURE_REFUND = {
    "name": "paypal_order_capture_refund", "test_class": "e2e",
    "description": "CREATED → COMPLETED → refunded: the standard Orders v2 "
                   "happy path with the capture id dug out of purchase_units.",
    "steps": [
        _step("order", "http", "create_order", dict(_PAYPAL_ORDER),
              [_assert("status_code", "eq", 201),
               _assert("body.status", "eq", "CREATED")],
              config=_PAYPAL_CONN_CFG),
        _step("capture", "http", "capture_order",
              {"id": "${order.response.body.id}"},
              [_assert("body.status", "eq", "COMPLETED"),
               _assert("body.purchase_units[0].payments.captures[0].id",
                       "present")],
              config=_PAYPAL_CONN_CFG),
        _step("refund", "http", "refund_capture",
              {"id": "${capture.response.body.purchase_units[0].payments"
                     ".captures[0].id}"},
              [_assert("body.status", "eq", "COMPLETED")],
              config=_PAYPAL_CONN_CFG),
    ],
    "edges": _chain("order", "capture", "refund"),
}

_PAYPAL_DECLINED = {
    "name": "paypal_instrument_declined", "test_class": "e2e",
    "description": "PayPal's documented negative-testing header forces "
                   "INSTRUMENT_DECLINED on capture — the 422 every merchant "
                   "must branch on (works against the real sandbox too).",
    "steps": [
        _step("order", "http", "create_order", dict(_PAYPAL_ORDER),
              [_assert("body.status", "eq", "CREATED")],
              config=_PAYPAL_CONN_CFG),
        _step("declined", "http", "capture_order_expecting_decline",
              {"id": "${order.response.body.id}"},
              [_assert("status_code", "eq", 422),
               _assert("body.details[0].issue", "eq", "INSTRUMENT_DECLINED")],
              config=_PAYPAL_CONN_CFG),
    ],
    "edges": _chain("order", "declined"),
}


BUILTIN_PACKS: list[Pack] = [
    Pack(
        id="stripe_provider",
        scheme="stripe",
        label="Stripe provider (PaymentIntents)",
        description="Merchant-side Stripe integration cases: authorize + refund, "
                    "manual capture, the documented decline ladder, and 3DS "
                    "requires_action — runnable offline against the Stripe "
                    "simulator (ADR-0009).",
        mock_runnable=False,
        cases=[
            PackCase(id="stripe_auth_then_refund",
                     requirement="Successful charge is refundable (full lifecycle)",
                     scenario=_STRIPE_AUTH_REFUND),
            PackCase(id="stripe_manual_capture",
                     requirement="Two-step auth/capture flow completes",
                     scenario=_STRIPE_MANUAL_CAPTURE),
            PackCase(id="stripe_decline_insufficient_funds",
                     requirement="Declines carry an actionable error envelope",
                     scenario=_STRIPE_DECLINE),
            PackCase(id="stripe_3ds_requires_action",
                     requirement="3DS challenge path is surfaced, not swallowed",
                     scenario=_STRIPE_3DS),
        ],
        connections=[
            {
                "name": _STRIPE_SIM_CONN,
                "adapter": "http",
                "description": "Local Stripe simulator (saved-simulator preset "
                               "kind 'stripe', default port 8085). Safe for load "
                               "runs and chaos.",
                "host": "127.0.0.1",
                "port": 8085,
                "base_url": "http://127.0.0.1:8085/v1",
                "content_type": "form",
                "health_any_status": True,
                "headers": {"Authorization": "Bearer sk_test_payprobe"},
                "actions": _STRIPE_ACTIONS,
            },
            {
                "name": "stripe_sandbox",
                "adapter": "http",
                "description": "Stripe test mode (REAL external API — set your "
                               "sk_test key in authentication.token; stored "
                               "encrypted). external: true — the load engine "
                               "refuses it by design (ADR-0009).",
                "host": "api.stripe.com",
                "port": 443,
                "base_url": "https://api.stripe.com/v1",
                "content_type": "form",
                "health_any_status": True,
                "authentication": {"type": "bearer", "token": ""},
                "actions": _STRIPE_ACTIONS,
                "external": True,
            },
            _STRIPE_WEBHOOK_CONN,
        ],
        participant_flows=[_STRIPE_WEBHOOK_FLOW],
        card_pools=[
            {
                "name": "stripe_test_cards",
                "description": "Stripe's documented always-succeed test cards "
                               "(load-safe against the simulator). Decline "
                               "cards live in the pack's negative cases, not "
                               "the pool.",
                "cards": [
                    {"pan": "4242424242424242", "expiry": "1233", "cvv": "123",
                     "type": "visa"},
                    {"pan": "4000056655665556", "expiry": "1233", "cvv": "123",
                     "type": "visa_debit"},
                    {"pan": "5555555555554444", "expiry": "1233", "cvv": "123",
                     "type": "mastercard"},
                ],
            },
        ],
    ),
    Pack(
        id="adyen_provider",
        scheme="adyen",
        label="Adyen provider (Checkout)",
        description="Merchant-side Adyen Checkout cases: authorise + refund, "
                    "the documented refusal ladder, and the 3DS2 challenge "
                    "path — runnable offline against the Adyen simulator "
                    "(ADR-0009).",
        mock_runnable=False,
        cases=[
            PackCase(id="adyen_auth_then_refund",
                     requirement="Authorised payment is refundable "
                                 "(async accept contract)",
                     scenario=_ADYEN_AUTH_REFUND),
            PackCase(id="adyen_refusal_reason",
                     requirement="Refusals carry refusalReason on HTTP 200",
                     scenario=_ADYEN_REFUSAL),
            PackCase(id="adyen_3ds2_challenge",
                     requirement="3DS2 challenge is surfaced and completable",
                     scenario=_ADYEN_3DS2),
        ],
        connections=[
            {
                "name": _ADYEN_SIM_CONN,
                "adapter": "http",
                "description": "Local Adyen Checkout simulator (saved-simulator "
                               "preset kind 'adyen', default port 8086). Safe "
                               "for load runs and chaos.",
                "host": "127.0.0.1",
                "port": 8086,
                "base_url": "http://127.0.0.1:8086",
                "health_any_status": True,
                "headers": {"X-API-Key": "AQEyhmfxK_test"},
                "actions": _ADYEN_ACTIONS,
            },
            {
                "name": "adyen_sandbox",
                "adapter": "http",
                "description": "Adyen test environment (REAL external API — set "
                               "your X-API-Key; stored encrypted). external: "
                               "true — the load engine refuses it (ADR-0009).",
                "host": "checkout-test.adyen.com",
                "port": 443,
                "base_url": "https://checkout-test.adyen.com/v71",
                "health_any_status": True,
                "headers": {"X-API-Key": ""},
                "actions": _ADYEN_ACTIONS,
                "external": True,
            },
            _ADYEN_WEBHOOK_CONN,
        ],
        participant_flows=[_ADYEN_WEBHOOK_FLOW],
    ),
    Pack(
        id="paypal_provider",
        scheme="paypal",
        label="PayPal provider (Orders v2)",
        description="Merchant-side PayPal Orders v2 cases: create + capture + "
                    "refund and the INSTRUMENT_DECLINED negative path (via "
                    "PayPal's documented mock-response header) — runnable "
                    "offline against the PayPal simulator (ADR-0009).",
        mock_runnable=False,
        cases=[
            PackCase(id="paypal_order_capture_refund",
                     requirement="Orders v2 happy path completes end to end",
                     scenario=_PAYPAL_CAPTURE_REFUND),
            PackCase(id="paypal_instrument_declined",
                     requirement="INSTRUMENT_DECLINED on capture is surfaced "
                                 "as the documented 422",
                     scenario=_PAYPAL_DECLINED),
        ],
        connections=[
            {
                "name": _PAYPAL_SIM_CONN,
                "adapter": "http",
                "description": "Local PayPal Orders v2 simulator (saved-simulator "
                               "preset kind 'paypal', default port 8087). Safe "
                               "for load runs and chaos.",
                "host": "127.0.0.1",
                "port": 8087,
                "base_url": "http://127.0.0.1:8087",
                "health_any_status": True,
                "headers": {"Authorization": "Bearer A21.payprobe-sim"},
                "actions": _PAYPAL_ACTIONS,
            },
            {
                "name": "paypal_sandbox",
                "adapter": "http",
                "description": "PayPal sandbox (REAL external API — set your "
                               "client credentials; OAuth2 handled by the "
                               "shared http runner). external: true — the load "
                               "engine refuses it (ADR-0009).",
                "host": "api-m.sandbox.paypal.com",
                "port": 443,
                "base_url": "https://api-m.sandbox.paypal.com",
                "health_any_status": True,
                "oauth2": {
                    "token_url": "https://api-m.sandbox.paypal.com/v1/oauth2/token",
                    "client_id": "",
                    "client_secret": "",
                    "style": "basic",
                },
                "actions": _PAYPAL_ACTIONS,
                "external": True,
            },
            _PAYPAL_WEBHOOK_CONN,
        ],
        participant_flows=[_PAYPAL_WEBHOOK_FLOW],
    ),
    Pack(
        id="switch_settlement",
        scheme="generic",
        label="Switch & Settlement",
        description="Authorization reaches the switch and settles downstream; "
                    "host reachability.",
        cases=[
            PackCase(id="pack_auth_then_settled",
                     requirement="Authorization reflected in downstream settlement",
                     scenario=_AUTH_SETTLE),
            PackCase(id="pack_network_echo",
                     requirement="Network echo / host reachable",
                     scenario=_ECHO),
        ],
    ),
]


def list_packs() -> list[Pack]:
    return list(BUILTIN_PACKS)


def get_pack(pack_id: str) -> Pack | None:
    return next((p for p in BUILTIN_PACKS if p.id == pack_id), None)
