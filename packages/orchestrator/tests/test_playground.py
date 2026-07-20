"""Playground (ADR-0007): target catalog, execute-by-reference, masking,
history + re-fire + save-as-scenario, and the provenance tag on simulators.

Execution is delegated to the worker engine, so these tests run a real
TcpResponder and fire real ISO 8583 at it — no adapter mocks."""

import httpx
import pytest
from fastapi.testclient import TestClient

import orchestrator.api.main as m
from orchestrator.api.playground import (
    PlaygroundHistory,
    build_playground_scenario,
    mask_payload,
)

PAN = "4000001234567890"


@pytest.fixture()
def client():
    with TestClient(m.app) as c:
        yield c


@pytest.fixture()
async def aclient():
    """In-loop ASGI client: execute tests run a live responder on the SAME
    event loop, which TestClient's portal thread would starve."""
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh_history():
    """Each test gets an empty ring and clean tag counters."""
    m.playground_history._rings.clear()
    m.playground_history._seq = 0
    m.PLAYGROUND_HITS.clear()
    m.PLAYGROUND_HIT_LOG.clear()
    yield


@pytest.fixture()
async def responder():
    """A live rules-driven ISO 8583 host simulator on an ephemeral port."""
    from worker.adapters.tcp.responder import TcpResponder

    r = TcpResponder({
        "protocol": "iso8583",
        "default": {"echo": ["11"], "set": {"39": "00", "38": "AUTH01"}},
    })
    port = await r.start()
    yield r, port
    await r.stop()


# -- masking (unit) ------------------------------------------------------------

def test_mask_payload_redacts_de_maps_and_flat_keys():
    masked = mask_payload({
        "mti": "0200",
        "values": {"2": PAN, "4": "000000001000", "35": "track-data"},
        "pan": PAN,
        "pin_block": "0123456789ABCDEF",
        "nested": [{"card_number": PAN}],
    })
    assert masked["values"]["2"] == "400000******7890"
    assert masked["values"]["4"] == "000000001000"          # non-sensitive kept
    assert set(masked["values"]["35"]) == {"*"}
    assert masked["pan"] == "400000******7890"
    assert set(masked["pin_block"]) == {"*"}
    assert masked["nested"][0]["card_number"] == "400000******7890"


def test_mask_payload_passes_non_dicts_through():
    assert mask_payload("plain") == "plain"
    assert mask_payload(None) is None


# -- target catalog --------------------------------------------------------------

async def test_targets_merges_registries(client, responder, monkeypatch):
    _r, _port = responder
    sid = "simX"
    m.SIMULATORS[sid] = _r
    try:
        async def fake_get(url):
            assert url.endswith("/connections")
            return [{"name": "switch_visa", "adapter": "tcp",
                     "protocol": "iso8583", "host": "10.0.0.1", "port": 7001,
                     "environment_overrides": {"staging": {"host": "10.0.0.2"}}}]

        monkeypatch.setattr(m, "_http_get_json", fake_get)
        monkeypatch.setattr(m, "_load_groups_index", _empty_groups)
        body = client.get("/playground/targets").json()
    finally:
        m.SIMULATORS.pop(sid, None)

    conn = body["connections"][0]
    assert conn["name"] == "switch_visa"
    assert conn["environments"] == ["staging"]
    # each target carries the server-resolved sample family (ADR-0007 follow-up)
    assert conn["sample_family"] == "iso8583"
    assert body["environments"] == ["staging"]
    assert any(s["id"] == sid for s in body["simulators"])
    assert "kcv" in body["functions"]["crypto"]
    assert "tcp" in body["raw"]["adapters"]
    # the samples block: wire families + one runnable set per crypto op
    assert body["samples"]["wire"]["iso8583"]
    assert any(s["action"] == "kcv"
               for s in body["samples"]["functions"]["crypto"])


async def _empty_groups():
    return {}


async def test_targets_survive_scenario_service_down(client, monkeypatch):
    async def boom(url):
        raise RuntimeError("down")

    monkeypatch.setattr(m, "_http_get_json", boom)
    monkeypatch.setattr(m, "_load_groups_index", _empty_groups)
    body = client.get("/playground/targets").json()
    assert body["connections"] == []
    assert "functions" in body


# -- execute: raw target ---------------------------------------------------------

async def test_execute_raw_target_round_trip_masked(aclient, responder):
    _r, port = responder
    resp = await aclient.post("/playground/execute", json={
        "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                   "protocol": "iso8583"},
        "action": "send_message",
        "payload": {"mti": "0200", "values": {"2": PAN, "4": "000000001000"}},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["status"] == "passed"
    assert body["response"]["response_code"] == "00"
    # invariant #8: the echoed request/response is masked
    assert body["request"]["values"]["2"] == "400000******7890"
    assert PAN not in str(body)
    assert body["history_seq"] == 1


async def test_execute_raw_needs_host_port(client):
    resp = client.post("/playground/execute", json={
        "target": {"kind": "raw"}, "action": "send_message", "payload": {}})
    assert resp.status_code == 400


async def test_execute_unreachable_target_is_a_result_not_an_error(client):
    resp = client.post("/playground/execute", json={
        "target": {"kind": "raw", "host": "127.0.0.1", "port": 1,
                   "protocol": "iso8583"},
        "action": "send_message", "payload": {"mti": "0800"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


# -- execute: by connection reference (through the override matrix) --------------

def _conn_doc(port):
    return {
        "name": "switch_visa", "adapter": "tcp", "protocol": "iso8583",
        "mode": "outbound", "host": "203.0.113.9", "port": 1,   # base: wrong
        "sign_on": {"enabled": False}, "reconnect": {"enabled": False},
        "connect_timeout_sec": 2, "response_timeout_sec": 2,
        "environment_overrides": {"staging": {"host": "127.0.0.1", "port": port}},
    }


async def test_execute_by_connection_resolves_through_matrix(
        aclient, responder, monkeypatch):
    _r, port = responder

    async def fake_get(url):
        assert url.endswith("/connections/switch_visa")
        return _conn_doc(port)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    resp = await aclient.post("/playground/execute", json={
        "target": {"kind": "connection", "id": "switch_visa",
                   "environment": "staging"},
        "action": "send_message",
        "payload": {"mti": "0200", "values": {"2": PAN, "4": "000000002000"}},
    })
    body = resp.json()
    assert body["ok"] is True, body
    assert body["target"] == {"kind": "connection", "id": "switch_visa",
                              "environment": "staging", "adapter": "tcp",
                              "host": "127.0.0.1", "port": port,
                              "base_url": None}


async def test_execute_unknown_connection_404(client, monkeypatch):
    import httpx

    async def fake_get(url):
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", url),
            response=httpx.Response(404, request=httpx.Request("GET", url)))

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    resp = client.post("/playground/execute", json={
        "target": {"kind": "connection", "id": "ghost"},
        "action": "send_message", "payload": {}})
    assert resp.status_code == 404


async def test_execute_disabled_connection_409(client, responder, monkeypatch):
    _r, port = responder

    async def fake_get(url):
        return {**_conn_doc(port), "disabled": True}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    resp = client.post("/playground/execute", json={
        "target": {"kind": "connection", "id": "switch_visa"},
        "action": "send_message", "payload": {}})
    assert resp.status_code == 409


async def test_execute_unknown_simulator_404(client):
    resp = client.post("/playground/execute", json={
        "target": {"kind": "simulator", "id": "ghost"},
        "action": "send_message", "payload": {}})
    assert resp.status_code == 404


async def test_execute_refuses_code_kind(client):
    """The Playground is not a second node runner — code stays on
    /nodes/execute, where the sandbox gate lives (gate inheritance by
    construction: there is no code path to bypass)."""
    resp = client.post("/playground/execute", json={
        "target": {"kind": "code"}, "action": "run", "payload": {}})
    assert resp.status_code == 400
    assert "/nodes/execute" in resp.json()["detail"]


# -- execute: simulator by reference + provenance tag ----------------------------

async def test_execute_simulator_by_reference_tags_stats(aclient, responder):
    r, _port = responder
    sid = "simY"
    r._label = "visa-sim"
    m.SIMULATORS[sid] = r
    try:
        resp = await aclient.post("/playground/execute", json={
            "target": {"kind": "simulator", "id": sid},
            "action": "send_message",
            "payload": {"mti": "0200", "values": {"4": "000000003000"}}})
        body = resp.json()
        assert body["ok"] is True
        assert m.PLAYGROUND_HITS[sid] == 1
        assert len(m.PLAYGROUND_HIT_LOG[sid]) == 1          # windowed log too
        sims = (await aclient.get("/simulators")).json()
        me = next(s for s in sims if s["id"] == sid)
        assert me["playground_hits"] == 1
    finally:
        m.SIMULATORS.pop(sid, None)


# -- provenance: contamination inside a run's window -----------------------------

def test_playground_traffic_counts_only_in_window():
    import time as _t

    now = _t.time()
    m.PLAYGROUND_HIT_LOG["simA"] = [now - 100, now - 50, now - 10]
    m.PLAYGROUND_HIT_LOG["simB"] = [now - 500]  # before the window
    from datetime import datetime, timezone

    def iso(offset):
        return datetime.fromtimestamp(now + offset, timezone.utc).isoformat()

    detail = {"started_at": iso(-120), "completed_at": iso(-5)}
    out = m._playground_traffic(detail)
    assert out["total"] == 3
    assert out["by_simulator"] == {"simA": 3}   # simB's hit is out of window
    assert out["window"]["from"] == detail["started_at"]


def test_playground_traffic_empty_without_started_at():
    m.PLAYGROUND_HIT_LOG["simA"] = [__import__("time").time()]
    out = m._playground_traffic({"completed_at": None})
    assert out == {"total": 0, "by_simulator": {}, "window": None}


def test_playground_traffic_in_flight_uses_now_as_end():
    import time as _t
    from datetime import datetime, timezone

    now = _t.time()
    m.PLAYGROUND_HIT_LOG["simA"] = [now - 1]
    started = datetime.fromtimestamp(now - 10, timezone.utc).isoformat()
    out = m._playground_traffic({"started_at": started})  # no completed_at
    assert out["total"] == 1


# -- execute: local crypto functions ---------------------------------------------

async def test_execute_crypto_function(client):
    pytest.importorskip("Crypto")   # pycryptodome — env dep, not a regression
    resp = client.post("/playground/execute", json={
        "target": {"kind": "function"}, "action": "kcv",
        "payload": {"key": "0123456789ABCDEFFEDCBA9876543210"}})
    body = resp.json()
    assert body["ok"] is True
    assert body["response"]["kcv"]


async def test_execute_unknown_crypto_function_is_error_result(client):
    resp = client.post("/playground/execute", json={
        "target": {"kind": "function"}, "action": "frobnicate", "payload": {}})
    body = resp.json()
    assert body["ok"] is False
    assert "unknown crypto operation" in body["error"]


# -- history: ring, masking, re-fire, clear --------------------------------------

async def test_history_records_masked_and_refires(aclient, responder):
    _r, port = responder
    payload = {"mti": "0200", "values": {"2": PAN, "4": "000000004000"}}
    first = (await aclient.post("/playground/execute", json={
        "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                   "protocol": "iso8583"},
        "action": "send_message", "payload": payload})).json()
    assert first["ok"] is True

    hist = (await aclient.get("/playground/history")).json()
    assert hist["count"] == 1
    row = hist["rows"][0]
    assert row["source"] == "playground"
    assert row["request"]["values"]["2"] == "400000******7890"
    assert "_payload" not in row                      # raw never leaves memory

    refired = (await aclient.post(
        f"/playground/history/{row['seq']}/refire")).json()
    assert refired["ok"] is True
    # the re-fire used the RAW pan (it reached the host and was approved),
    # while its own echo is masked again.
    assert refired["request"]["values"]["2"] == "400000******7890"
    assert (await aclient.get("/playground/history")).json()["count"] == 2

    assert (await aclient.delete("/playground/history")).status_code == 204
    assert (await aclient.get("/playground/history")).json()["count"] == 0


async def test_refire_unknown_seq_404(client):
    assert client.post("/playground/history/99/refire").status_code == 404


def test_history_ring_is_bounded_and_per_user():
    h = PlaygroundHistory(max_entries=2)
    for i in range(3):
        h.add("alice", target={"kind": "raw"}, action="a", payload={"i": i},
              message_format_id=None, ok=True, status="passed",
              request={"i": i}, response={}, error=None, duration_ms=1)
    h.add("bob", target={"kind": "raw"}, action="a", payload={},
          message_format_id=None, ok=True, status="passed",
          request={}, response={}, error=None, duration_ms=1)
    alice = h.rows("alice")
    assert len(alice) == 2                       # oldest evicted
    assert [r["request"]["i"] for r in alice] == [2, 1]   # newest first
    assert len(h.rows("bob")) == 1
    assert h.get("alice", alice[0]["seq"])["_payload"] == {"i": 2}


# -- save-as-scenario --------------------------------------------------------------

def test_build_playground_scenario_shapes_steps():
    entries = [
        {"seq": 2, "action": "kcv", "target": {"kind": "function"},
         "request": {"key": "K"}, "response": {"kcv": "AB"}},
        {"seq": 1, "action": "send_message",
         "target": {"kind": "connection", "id": "switch_visa",
                    "environment": "staging"},
         "message_format_id": "visa-base1",
         "request": {"mti": "0200", "values": {"2": "400000******7890"}},
         "response": {"response_code": "00"}},
    ]
    draft = build_playground_scenario(entries, "explored")
    assert draft["tags"] == ["captured", "playground"]
    assert len(draft["steps"]) == 2
    s1, s2 = draft["steps"]                       # replayed oldest-first
    assert s1["target"] == "switch_visa"
    assert s1["config"] == {"connection": "switch_visa"}
    assert s1["environment_override"] == "staging"
    assert s1["payload"]["message_format_id"] == "visa-base1"
    assert s1["assertions"] == [
        {"field": "response_code", "operator": "eq", "expected": "00"}]
    assert s2["kind"] == "crypto"
    assert s2["config"]["operation"] == "kcv"


async def test_save_as_scenario_posts_draft(aclient, responder, monkeypatch):
    _r, port = responder
    await aclient.post("/playground/execute", json={
        "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                   "protocol": "iso8583"},
        "action": "send_message",
        "payload": {"mti": "0200", "values": {"2": PAN}}})

    posted = {}

    async def fake_post(url, body):
        posted["url"], posted["body"] = url, body
        return {"id": "sc-123"}

    monkeypatch.setattr(m, "_http_post_json", fake_post)
    resp = await aclient.post("/playground/save-as-scenario",
                              json={"name": "poked", "project_id": "p1"})
    assert resp.status_code == 201
    assert resp.json() == {"scenario_id": "sc-123", "name": "poked", "steps": 1}
    assert posted["url"].endswith("/scenarios")
    draft = posted["body"]["scenario"]
    assert posted["body"]["project_id"] == "p1"
    # the promoted step carries the MASKED values (a template, like proxy capture)
    assert draft["steps"][0]["payload"]["values"]["2"] == "400000******7890"


async def test_save_as_scenario_empty_history_400(client):
    assert client.post("/playground/save-as-scenario",
                       json={"name": "x"}).status_code == 400


async def test_save_as_scenario_promotes_only_selected_seqs(
        aclient, responder, monkeypatch):
    """Selective promote: only the ticked history entries become steps."""
    _r, port = responder
    for amt in ("000000001000", "000000002000", "000000003000"):
        await aclient.post("/playground/execute", json={
            "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                       "protocol": "iso8583"},
            "action": "send_message",
            "payload": {"mti": "0200", "values": {"4": amt}}})

    posted = {}

    async def fake_post(url, body):
        posted["body"] = body
        return {"id": "sc-sel"}

    monkeypatch.setattr(m, "_http_post_json", fake_post)
    # promote only seqs 1 and 3 (skip the middle interaction)
    resp = await aclient.post("/playground/save-as-scenario",
                              json={"name": "curated", "seqs": [1, 3]})
    assert resp.status_code == 201
    assert resp.json()["steps"] == 2
    steps = posted["body"]["scenario"]["steps"]
    assert len(steps) == 2
    # replayed oldest-first: seq 1's amount then seq 3's
    assert steps[0]["payload"]["values"]["4"] == "000000001000"
    assert steps[1]["payload"]["values"]["4"] == "000000003000"


# -- samples: ready-made example interactions per element family -----------------

def test_samples_cover_every_crypto_operation_and_run_green():
    """Every OPERATIONS entry has a sample, and each sample executes without
    error — a sample that doesn't fire green as-is is a bug, not a template."""
    pytest.importorskip("Crypto")   # pycryptodome — env dep, not a regression
    from worker.engine.crypto_tools import OPERATIONS, run_crypto

    from orchestrator.api.playground_samples import CRYPTO_SAMPLES

    assert set(CRYPTO_SAMPLES) == set(OPERATIONS)
    for op, params in CRYPTO_SAMPLES.items():
        res = run_crypto(op, dict(params))
        assert "error" not in res, f"sample for '{op}' failed: {res.get('error')}"


def test_samples_catalog_shape():
    """Wire samples exist for every documented element family and are
    composer-shaped: name + action + dict payload (+ optional notes)."""
    from orchestrator.api.playground_samples import samples_catalog

    cat = samples_catalog()
    wire = cat["wire"]
    for family in ("iso8583", "visa", "header_echo", "payshield",
                   "http", "cybersource", "nats", "grpc"):
        assert wire.get(family), f"no samples for family '{family}'"
        for s in wire[family]:
            assert s["name"] and s["action"]
            assert isinstance(s["payload"], dict)
    ops = {s["action"] for s in cat["functions"]["crypto"]}
    from worker.engine.crypto_tools import OPERATIONS
    assert ops == set(OPERATIONS)


async def test_targets_carry_samples(client, monkeypatch):
    async def fake_get(url):
        return []

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    body = client.get("/playground/targets").json()
    assert "iso8583" in body["samples"]["wire"]
    assert body["samples"]["functions"]["crypto"]


async def test_iso8583_sample_fires_against_live_responder(aclient, responder):
    """The 0200 sample is not just a doc — fired verbatim at the bundled host
    simulator it comes back approved (DE 39 = 00)."""
    from orchestrator.api.playground_samples import WIRE_SAMPLES

    _, port = responder
    sample = next(s for s in WIRE_SAMPLES["iso8583"]
                  if s["action"] == "send_0200")
    resp = await aclient.post("/playground/execute", json={
        "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                   "protocol": "iso8583"},
        "action": sample["action"], "payload": sample["payload"]})
    body = resp.json()
    assert body["ok"] is True
    assert body["response"]["response_code"] == "00"


def test_sample_family_resolution():
    """Protocol wins over adapter; aliases fold shared wire shapes; unknown → None."""
    from orchestrator.api.playground_samples import sample_family

    assert sample_family("visa", "tcp") == "visa"
    assert sample_family(None, "tcp_iso8583") == "iso8583"
    assert sample_family("header_echo", "tcp") == "header_echo"
    assert sample_family(None, "hsm_client") == "payshield"
    assert sample_family("cybersource", "http") == "cybersource"
    assert sample_family("", "") is None
    assert sample_family("carrier_pigeon", None) is None


async def test_targets_rows_carry_sample_family(client, responder, monkeypatch):
    """Every target row names its samples.wire family server-side — one
    resolver, so the portal and agents don't re-derive the alias table."""
    _r, _port = responder
    m.SIMULATORS["simF"] = _r
    try:
        async def fake_get(url):
            return [{"name": "visa-link", "adapter": "tcp_iso8583",
                     "protocol": "visa", "environment_overrides": {}}]

        monkeypatch.setattr(m, "_http_get_json", fake_get)
        monkeypatch.setattr(m, "_load_groups_index", _empty_groups)
        body = client.get("/playground/targets").json()
    finally:
        m.SIMULATORS.pop("simF", None)
    conn = next(c for c in body["connections"] if c["name"] == "visa-link")
    assert conn["sample_family"] == "visa"
    sim = next(s for s in body["simulators"] if s["id"] == "simF")
    assert sim["sample_family"] == "iso8583"


# -- live-fire: samples vs the bundled simulators ---------------------------------


async def test_payshield_nc_sample_fires_against_hsm_simulator(aclient):
    """The payShield 'Diagnostics (NC)' sample, fired by simulator reference,
    answers error_code 00 from the bundled HSM simulator."""
    pytest.importorskip("Crypto")   # pycryptodome — env dep, not a regression
    from orchestrator.api.playground_samples import WIRE_SAMPLES

    started = await m.start_simulator(
        m.SimulatorDraft(label="hsm-pg", config={"protocol": "payshield"}))
    sid = started["id"]
    try:
        sample = next(s for s in WIRE_SAMPLES["payshield"]
                      if s["action"] == "nc")
        resp = await aclient.post("/playground/execute", json={
            "target": {"kind": "simulator", "id": sid},
            "action": sample["action"], "payload": sample["payload"]})
        body = resp.json()
        assert body["ok"] is True
        assert body["response"]["error_code"] == "00"
    finally:
        await m.stop_simulator(sid)


async def test_visa_auth_sample_fires_against_visa_simulator(aclient):
    """The VISA 0100 sample comes back approved from the bundled Base I sim."""
    from orchestrator.api.playground_samples import WIRE_SAMPLES

    started = await m.start_simulator(
        m.SimulatorDraft(label="visa-pg", config={"protocol": "visa"}))
    sid = started["id"]
    try:
        sample = next(s for s in WIRE_SAMPLES["visa"]
                      if s["action"] == "send_0100")
        resp = await aclient.post("/playground/execute", json={
            "target": {"kind": "simulator", "id": sid},
            "action": sample["action"], "payload": sample["payload"]})
        body = resp.json()
        assert body["ok"] is True
        assert body["response"]["response_code"] == "00"
    finally:
        await m.stop_simulator(sid)


async def test_cybersource_auth_sample_fires_against_gateway_sim(aclient):
    """The CyberSource card-authorization sample is AUTHORIZED by the bundled
    gateway sim — including its auth gate, which the derived client config
    passes with a bearer credential automatically."""
    pytest.importorskip("aiohttp")  # HTTP responder base — env dep
    import json as _json

    from orchestrator.api.playground_samples import WIRE_SAMPLES

    started = await m.start_simulator(
        m.SimulatorDraft(label="cybs-pg", config={"protocol": "cybersource"}))
    sid = started["id"]
    try:
        sample = next(s for s in WIRE_SAMPLES["cybersource"]
                      if s["action"] == "/pts/v2/payments")
        resp = await aclient.post("/playground/execute", json={
            "target": {"kind": "simulator", "id": sid},
            "action": sample["action"], "payload": sample["payload"]})
        body = resp.json()
        assert body["ok"] is True
        assert "AUTHORIZED" in _json.dumps(body["response"])
    finally:
        await m.stop_simulator(sid)


async def test_raw_payshield_target_fires_nc(aclient):
    """A raw host/port target with adapter=payshield (the portal's 'payShield
    host commands' raw option) reaches an HSM simulator with the NC sample."""
    pytest.importorskip("Crypto")   # pycryptodome — env dep, not a regression
    started = await m.start_simulator(
        m.SimulatorDraft(label="hsm-raw", config={"protocol": "payshield"}))
    sid, port = started["id"], started["port"]
    try:
        resp = await aclient.post("/playground/execute", json={
            "target": {"kind": "raw", "host": "127.0.0.1", "port": port,
                       "adapter": "payshield"},
            "action": "nc", "payload": {}})
        body = resp.json()
        assert body["ok"] is True
        assert body["response"]["error_code"] == "00"
    finally:
        await m.stop_simulator(sid)
