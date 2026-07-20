"""CyberSource REST gateway simulator — driven over a real HTTP socket.

Validates the public REST contract the simulator stands in for: the
authorization decision ladder (approve / partial / decline by amount, BIN,
expiry, CVN), the follow-on services (capture / refund / reversal / void) and
their lifecycle ids, the credential gate, and that it stays drop-in compatible
with the responder surface the orchestrator + portal consume (port / protocol /
stats / peers). Also exercises the generic HttpResponder rules engine and chaos.
"""

import asyncio

import httpx

from worker.adapters.http.responder import HttpResponder
from worker.adapters.scheme.cybersource import CyberSourceSimulator

AUTH = {"Authorization": "Bearer test.jwt.token"}


def _payment(
    amount="10.00",
    pan="4111111111111111",
    cvn="123",
    capture=False,
    exp_year="2030",
    exp_month="12",
    ref="ORDER-1",
):
    return {
        "clientReferenceInformation": {"code": ref},
        "processingInformation": {"capture": capture},
        "paymentInformation": {
            "card": {
                "number": pan,
                "securityCode": cvn,
                "expirationYear": exp_year,
                "expirationMonth": exp_month,
            }
        },
        "orderInformation": {"amountDetails": {"totalAmount": amount, "currency": "USD"}},
    }


async def _start(cs_config=None, **extra):
    config = {"host": "127.0.0.1", "port": 0, "cybersource": cs_config or {}, **extra}
    sim = CyberSourceSimulator(config)
    port = await sim.start()
    return sim, port


def _base(port):
    return f"http://127.0.0.1:{port}"


# -- duck-typed responder surface (orchestrator/portal compatibility) -----------


async def test_responder_surface():
    sim, port = await _start()
    try:
        assert sim.protocol == "cybersource"
        assert sim.port == port and port > 0
        assert isinstance(sim.rules, list)
        assert sim.stats()["received"] == 0
        assert sim.peers() == []
    finally:
        await sim.stop()
    assert sim.port is None


# -- authorization decision ladder ---------------------------------------------


async def test_authorize_approved():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(_base(port) + "/pts/v2/payments", json=_payment("10.00"), headers=AUTH)
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "AUTHORIZED"
        assert body["processorInformation"]["responseCode"] == "100"
        assert body["processorInformation"]["approvalCode"]
        assert body["clientReferenceInformation"]["code"] == "ORDER-1"
        assert sim.stats()["received"] == 1
    finally:
        await sim.stop()


async def test_partial_authorization():
    sim, port = await _start({"partial_over": 500, "decline_over": 100000})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                _base(port) + "/pts/v2/payments", json=_payment("600.00"), headers=AUTH
            )
        body = r.json()
        assert body["status"] == "PARTIAL_AUTHORIZED"
        # partial approves a lower amount than requested
        assert float(body["orderInformation"]["amountDetails"]["authorizedAmount"]) < 600.0
    finally:
        await sim.stop()


async def test_decline_over_amount():
    sim, port = await _start({"decline_over": 1000})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                _base(port) + "/pts/v2/payments", json=_payment("2500.00"), headers=AUTH
            )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "DECLINED"
        assert body["errorInformation"]["reason"] == "INSUFFICIENT_FUND"
    finally:
        await sim.stop()


async def test_decline_blocked_bin():
    sim, port = await _start({"block_bins": ["400000"]})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                _base(port) + "/pts/v2/payments",
                json=_payment(pan="4000001234567899"),
                headers=AUTH,
            )
        body = r.json()
        assert body["status"] == "DECLINED"
        assert body["errorInformation"]["reason"] == "PROCESSOR_DECLINED"
    finally:
        await sim.stop()


async def test_decline_expired_card():
    sim, port = await _start({"expiry_check": True})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                _base(port) + "/pts/v2/payments",
                json=_payment(exp_year="2020", exp_month="01"),
                headers=AUTH,
            )
        body = r.json()
        assert body["status"] == "DECLINED"
        assert body["errorInformation"]["reason"] == "EXPIRED_CARD"
    finally:
        await sim.stop()


async def test_decline_cvn_mismatch():
    sim, port = await _start({"cvn_decline_values": ["999"]})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                _base(port) + "/pts/v2/payments", json=_payment(cvn="999"), headers=AUTH
            )
        body = r.json()
        assert body["status"] == "DECLINED"
        assert body["errorInformation"]["reason"] == "CVN_NOT_MATCH"
    finally:
        await sim.stop()


# -- credential gate ------------------------------------------------------------


async def test_missing_auth_rejected():
    sim, port = await _start({"require_auth": True})
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(_base(port) + "/pts/v2/payments", json=_payment())
        assert r.status_code == 401
        assert r.json()["status"] == "INVALID_REQUEST"
    finally:
        await sim.stop()


async def test_http_signature_credential_accepted():
    sim, port = await _start({"require_auth": True})
    try:
        headers = {"v-c-merchant-id": "payprobe_test", "signature": "keyid=...,signature=..."}
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(_base(port) + "/pts/v2/payments", json=_payment(), headers=headers)
        assert r.status_code == 201
        assert r.json()["status"] == "AUTHORIZED"
    finally:
        await sim.stop()


# -- follow-on services + lifecycle --------------------------------------------


async def test_auth_capture_refund_lifecycle():
    """auth -> capture -> refund the capture, then retrieve."""
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            auth = (
                await c.post(_base(port) + "/pts/v2/payments", json=_payment("40.00"), headers=AUTH)
            ).json()
            pid = auth["id"]

            cap = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/captures",
                json=_payment("40.00"),
                headers=AUTH,
            )
            assert cap.status_code == 201 and cap.json()["status"] == "PENDING"
            cap_id = cap.json()["id"]

            ref = await c.post(
                f"{_base(port)}/pts/v2/captures/{cap_id}/refunds",
                json=_payment("40.00"),
                headers=AUTH,
            )
            assert ref.json()["status"] == "PENDING"

            got = await c.get(f"{_base(port)}/pts/v2/payments/{pid}", headers=AUTH)
            assert got.status_code == 200 and got.json()["id"] == pid
    finally:
        await sim.stop()


async def test_auth_reversal_lifecycle():
    """An un-captured authorization can be reversed."""
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(_base(port) + "/pts/v2/payments", json=_payment("40.00"), headers=AUTH)
            ).json()["id"]
            rev = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/reversals",
                json=_payment("40.00"),
                headers=AUTH,
            )
            assert rev.json()["status"] == "REVERSED"
    finally:
        await sim.stop()


async def test_capture_then_void_lifecycle():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(_base(port) + "/pts/v2/payments", json=_payment("40.00"), headers=AUTH)
            ).json()["id"]
            cap_id = (
                await c.post(
                    f"{_base(port)}/pts/v2/payments/{pid}/captures",
                    json=_payment("40.00"),
                    headers=AUTH,
                )
            ).json()["id"]
            void = await c.post(
                f"{_base(port)}/pts/v2/captures/{cap_id}/voids", json={}, headers=AUTH
            )
            assert void.json()["status"] == "VOIDED"
    finally:
        await sim.stop()


# -- invalid state transitions are rejected ------------------------------------


async def test_double_capture_rejected():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(_base(port) + "/pts/v2/payments", json=_payment("40.00"), headers=AUTH)
            ).json()["id"]
            await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/captures",
                json=_payment("40.00"),
                headers=AUTH,
            )
            again = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/captures",
                json=_payment("40.00"),
                headers=AUTH,
            )
        assert again.status_code == 400
        assert again.json()["errorInformation"]["reason"] == "AUTH_ALREADY_CAPTURED"
    finally:
        await sim.stop()


async def test_reverse_after_capture_rejected():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(
                    _base(port) + "/pts/v2/payments",
                    json=_payment("40.00", capture=True),
                    headers=AUTH,
                )
            ).json()["id"]
            rev = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/reversals",
                json=_payment("40.00"),
                headers=AUTH,
            )
        assert rev.status_code == 400
        assert rev.json()["errorInformation"]["reason"] == "AUTH_ALREADY_CAPTURED"
    finally:
        await sim.stop()


async def test_refund_before_capture_rejected():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(_base(port) + "/pts/v2/payments", json=_payment("40.00"), headers=AUTH)
            ).json()["id"]
            ref = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/refunds", json=_payment("40.00"), headers=AUTH
            )
        assert ref.status_code == 400
        assert ref.json()["errorInformation"]["reason"] == "AUTH_NOT_CAPTURED"
    finally:
        await sim.stop()


async def test_double_void_rejected():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            pid = (
                await c.post(
                    _base(port) + "/pts/v2/payments",
                    json=_payment("40.00", capture=True),
                    headers=AUTH,
                )
            ).json()["id"]
            await c.post(f"{_base(port)}/pts/v2/payments/{pid}/voids", json={}, headers=AUTH)
            again = await c.post(
                f"{_base(port)}/pts/v2/payments/{pid}/voids", json={}, headers=AUTH
            )
        assert again.status_code == 400
        assert again.json()["errorInformation"]["reason"] == "TRANSACTION_ALREADY_VOIDED"
    finally:
        await sim.stop()


async def test_followon_unknown_id_404():
    sim, port = await _start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(
                f"{_base(port)}/pts/v2/payments/nope/captures", json=_payment(), headers=AUTH
            )
        assert r.status_code == 404
        assert r.json()["errorInformation"]["reason"] == "NOT_FOUND"
    finally:
        await sim.stop()


# -- explicit rules win over gateway logic -------------------------------------


async def test_explicit_rule_overrides_gateway():
    sim, port = await _start(
        {},
        rules=[
            {
                "when": {
                    "method": "POST",
                    "path": {"prefix": "/pts/v2/payments"},
                    "body": {"orderInformation.amountDetails.totalAmount": {"eq": "13.00"}},
                },
                "respond": {"status": 502, "json": {"status": "DECLINED", "note": "pinned"}},
            }
        ],
    )
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.post(_base(port) + "/pts/v2/payments", json=_payment("13.00"), headers=AUTH)
        assert r.status_code == 502
        assert r.json()["note"] == "pinned"
    finally:
        await sim.stop()


# -- generic HttpResponder (the reusable base) ---------------------------------


async def test_generic_http_responder_rules():
    sim = HttpResponder(
        {
            "host": "127.0.0.1",
            "port": 0,
            "rules": [
                {
                    "when": {"method": "GET", "path": {"eq": "/health"}},
                    "respond": {"status": 200, "json": {"status": "ok"}},
                },
                {
                    "when": {
                        "method": "POST",
                        "path": {"prefix": "/charge"},
                        "body": {"amount": {"gte": 1000}},
                    },
                    "respond": {"status": 402, "json": {"decision": "DECLINE"}},
                },
            ],
            "default": {"status": 404, "json": {"error": "not found"}},
        }
    )
    port = await sim.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            assert (await c.get(f"http://127.0.0.1:{port}/health")).json()["status"] == "ok"
            big = await c.post(f"http://127.0.0.1:{port}/charge", json={"amount": 5000})
            assert big.status_code == 402
            small = await c.post(f"http://127.0.0.1:{port}/charge", json={"amount": 5})
            assert small.status_code == 404  # falls through to default
        st = sim.stats()
        assert st["received"] == 3
        assert st["by_response_code"].get("200") == 1
    finally:
        await sim.stop()


async def test_chaos_latency_injects_delay():
    sim = HttpResponder(
        {
            "host": "127.0.0.1",
            "port": 0,
            "chaos": {"seed": 1, "latency_ms": {"min": 120, "max": 120, "pct": 100}},
            "default": {"status": 200, "json": {"ok": True}},
        }
    )
    port = await sim.start()
    try:
        loop = asyncio.get_event_loop()
        async with httpx.AsyncClient(trust_env=False) as c:
            t0 = loop.time()
            r = await c.get(f"http://127.0.0.1:{port}/x")
            elapsed = loop.time() - t0
        assert r.status_code == 200
        assert elapsed >= 0.10  # the injected ~120ms latency landed
        assert sim.faults >= 1
    finally:
        await sim.stop()
