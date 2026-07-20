"""Test-case packs: list/get/install + the built-in scenarios actually pass."""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api.main as m  # noqa: E402
from models.pack import BUILTIN_PACKS, list_packs  # noqa: E402


def _client():
    return TestClient(m.app)


def test_list_and_get_packs():
    with _client() as c:
        listed = c.get("/packs").json()
        assert {p["id"] for p in listed} == {p.id for p in BUILTIN_PACKS}
        first = listed[0]["id"]
        detail = c.get(f"/packs/{first}").json()
        assert detail["cases"] and "scenario" in detail["cases"][0]
        assert c.get("/packs/nope").status_code == 404


def test_install_creates_project_with_scenarios():
    with _client() as c:
        res = c.post("/packs/switch_settlement/install")
        assert res.status_code == 200
        body = res.json()
        assert body["imported"] == 2
        # scenarios now exist under the new project
        scenarios = c.get(f"/scenarios?project_id={body['project_id']}").json()
        names = {s["name"] for s in scenarios}
        assert names == set(body["scenario_names"])


@pytest.mark.parametrize(
    "pack", [p for p in list_packs() if p.mock_runnable], ids=lambda p: p.id
)
async def test_pack_scenarios_pass_in_mock(pack):
    """Every built-in pack case must run green in mock mode (runnable baseline).

    Provider packs (``mock_runnable=False``) assert on provider-shaped
    responses, so their runnable baseline is the matching provider simulator —
    see ``test_stripe_pack_against_simulator`` below."""
    from worker.engine import WorkerEngine, InMemorySink, PASSED

    scenarios = [{"id": case.id, **case.scenario} for case in pack.cases]
    engine = WorkerEngine({"mode": "mock", "adapters": {}}, InMemorySink())
    summary = await engine.run_scenario_batch(scenarios, run_id=f"pack-{pack.id}")
    statuses = {s["name"]: s["status"] for s in summary["scenarios"]}
    assert all(v == PASSED for v in statuses.values()), statuses


async def test_stripe_pack_against_simulator():
    """The Stripe pack's runnable baseline: every case green against a live
    StripeSimulator (ADR-0009) — the provider-pack analog of the mock gate.

    Connection re-pointing mirrors the orchestrator's ``_attach_connections``:
    the pack connection's config lands in ``env['adapters']`` (base_url swapped
    for the ephemeral test port) and each step's target becomes its chosen
    connection name."""
    from worker.adapters.scheme.stripe_sim import StripeSimulator
    from worker.engine import WorkerEngine, InMemorySink, PASSED

    pack = next(p for p in list_packs() if p.id == "stripe_provider")
    assert pack.mock_runnable is False

    sim = StripeSimulator({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    try:
        conn = dict(next(c for c in pack.connections if c["name"] == "stripe_simulator"))
        conn.pop("name")
        conn["base_url"] = f"http://127.0.0.1:{port}/v1"
        env = {"adapters": {"stripe_simulator": conn}}

        scenarios = []
        for case in pack.cases:
            sc = {"id": case.id, **case.scenario}
            for step in sc["steps"]:
                chosen = (step.get("config") or {}).get("connection")
                if chosen:
                    step["target"] = chosen
            scenarios.append(sc)

        engine = WorkerEngine(env, InMemorySink())
        summary = await engine.run_scenario_batch(scenarios, run_id="pack-stripe")
        statuses = {s["name"]: s["status"] for s in summary["scenarios"]}
        assert all(v == PASSED for v in statuses.values()), statuses
    finally:
        await sim.stop()


def test_stripe_pack_install_imports_connections_and_pool():
    with _client() as c:
        res = c.post("/packs/stripe_provider/install")
        assert res.status_code == 200
        body = res.json()
        assert body["imported"] == 4
        assert set(body["connection_names"]) == {
            "stripe_simulator", "stripe_sandbox", "merchant_webhooks_stripe"}
        # phase 4: the merchant webhook receiver flow installs with the pack
        assert body["participant_flow_ids"] == ["merchant_webhooks_stripe"]
        assert body["card_pool_names"] == ["stripe_test_cards"]

        # the sandbox preset carries the load-engine guardrail flag
        sandbox = c.get("/connections/stripe_sandbox").json()
        assert sandbox["external"] is True
        sim_conn = c.get("/connections/stripe_simulator").json()
        assert sim_conn.get("external", False) is False

        pool = c.get("/test-data/card-pools/stripe_test_cards").json()
        assert len(pool["cards"]) == 3

        # re-install: create-only — nothing new, nothing clobbered
        res2 = c.post("/packs/stripe_provider/install")
        assert res2.json()["connection_names"] == []
        assert res2.json()["card_pool_names"] == []


async def test_webhook_loop_sim_to_pack_receiver_flow():
    """The phase-4 loop, fully offline: the Stripe simulator emits a signed
    webhook at the pack's merchant receiver flow, which answers 2xx — so the
    delivery counts as sent, not failed. sim → signed event → receiver → 2xx."""
    from worker.adapters.http.flow_responder import HttpFlowResponder
    from worker.adapters.scheme.stripe_sim import StripeSimulator

    from models.pack import get_pack

    pack = get_pack("stripe_provider")
    flow = next(dict(f) for f in pack.participant_flows
                if f["id"] == "merchant_webhooks_stripe")
    receiver = HttpFlowResponder({"host": "127.0.0.1", "port": 0}, flow)
    rport = await receiver.start()

    sim = StripeSimulator({"host": "127.0.0.1", "port": 0,
                           "webhooks": {"url": f"http://127.0.0.1:{rport}/webhooks/stripe",
                                        "secret": "whsec_pack"}})
    sport = await sim.start()
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{sport}/v1/payment_intents",
                data={"amount": "1500", "currency": "usd", "confirm": "true",
                      "payment_method_data[card][number]": "4242424242424242"},
                headers={"Authorization": "Bearer sk_test_payprobe"})
        assert resp.json()["status"] == "succeeded"
        await sim.webhooks.drain()

        # the receiver flow saw the delivery and acknowledged it
        assert len(receiver.received) == 1
        assert receiver.received[0]["body"]["type"] == "payment_intent.succeeded"
        wh = sim.stats()["webhooks"]
        assert wh["sent"] == 1
        assert wh["failed"] == 0
    finally:
        await sim.stop()
        await receiver.stop()

async def _pack_baseline(pack_id: str, sim_cls, conn_name: str,
                         base_url_suffix: str = ""):
    """Shared provider-pack baseline: every case green against the matching
    live simulator, with the same connection re-pointing as the stripe test."""
    from worker.engine import WorkerEngine, InMemorySink, PASSED

    pack = next(p for p in list_packs() if p.id == pack_id)
    assert pack.mock_runnable is False

    sim = sim_cls({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    try:
        conn = dict(next(c for c in pack.connections if c["name"] == conn_name))
        conn.pop("name")
        conn["base_url"] = f"http://127.0.0.1:{port}{base_url_suffix}"
        env = {"adapters": {conn_name: conn}}

        scenarios = []
        for case in pack.cases:
            sc = {"id": case.id, **case.scenario}
            for step in sc["steps"]:
                chosen = (step.get("config") or {}).get("connection")
                if chosen:
                    step["target"] = chosen
            scenarios.append(sc)

        engine = WorkerEngine(env, InMemorySink())
        summary = await engine.run_scenario_batch(scenarios, run_id=f"pack-{pack_id}")
        statuses = {s["name"]: s["status"] for s in summary["scenarios"]}
        assert all(v == PASSED for v in statuses.values()), statuses
    finally:
        await sim.stop()


async def test_adyen_pack_against_simulator():
    """The Adyen pack's runnable baseline (ADR-0009 phase 2)."""
    from worker.adapters.scheme.adyen_sim import AdyenCheckoutSimulator

    await _pack_baseline("adyen_provider", AdyenCheckoutSimulator,
                         "adyen_simulator")


async def test_paypal_pack_against_simulator():
    """The PayPal pack's runnable baseline (ADR-0009 phase 2)."""
    from worker.adapters.scheme.paypal_sim import PayPalOrdersSimulator

    await _pack_baseline("paypal_provider", PayPalOrdersSimulator,
                         "paypal_simulator")

