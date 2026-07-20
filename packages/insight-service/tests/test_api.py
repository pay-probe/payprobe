"""API surface: endpoints, on-demand ingest, auth gate, self-scoring."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
from insight_service.ingest import Ingestor
from insight_service.rest import OrchestratorReader
from insight_service.store import InsightStore

from insight_fixtures import FakeOrchestrator, build_history


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    fake = FakeOrchestrator(build_history())
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    monkeypatch.setattr(main_mod.app.state, "store", store, raising=False)
    monkeypatch.setattr(main_mod.app.state, "reader", reader, raising=False)
    monkeypatch.setattr(main_mod.app.state, "ingestor",
                        Ingestor(store, reader), raising=False)
    return TestClient(main_mod.app)


def test_health_and_status(client):
    assert client.get("/health").json()["status"] == "ok"
    s = client.get("/status").json()
    assert s["advisory_only"] is True
    assert s["predictor"]["model_version"]


def test_train_then_predictions(client):
    r = client.post("/train").json()
    assert r["sync"]["runs_ingested"] == 8
    preds = client.get("/insights/predictions").json()["predictions"]
    ids = [p["scenario_id"] for p in preds]
    # riskiest first
    assert ids.index("sc-broken") < ids.index("sc-stable")
    one = client.get("/insights/predictions/sc-flaky").json()
    assert one["p_flaky"] > 0.5


def test_failures_endpoint_ingests_on_demand(client):
    body = client.get("/insights/failures/run-8").json()
    assert body["advisory_only"] is True
    cats = {f["heuristic"] for f in body["failures"]}
    assert "unreachable" in cats  # sc-broken
    hsm = next(f for f in body["failures"] if f["scenario_id"] == "sc-hsm")
    assert hsm["novel"] is True
    assert "Suggested fix" in hsm["explanation"]


def test_unknown_run_404(client):
    assert client.get("/insights/failures/nope").status_code == 404
    assert client.get("/insights/predictions/nope").status_code == 404


def test_rename_category(client):
    client.post("/train")
    r = client.post("/insights/categories/error/rename",
                    json={"label": "payShield socket drop"})
    assert r.json()["label"] == "payShield socket drop"
    cats = client.get("/insights/categories").json()["categories"]
    by_id = {c["category"]: c for c in cats}
    if "error" in by_id:
        assert by_id["error"]["label"] == "payShield socket drop"


def test_self_scoring_brier_uses_first_subsequent_run(client):
    client.post("/train")
    client.get("/insights/predictions")  # logs predictions (created_at = now)
    # runs that PRECEDE the predictions must NOT score them…
    st = main_mod.app.state.store
    st.set_meta("last_run_created_at", "")
    main_mod.app.state.ingestor.sync()
    assert client.get("/status").json()["predictor"]["brier"] is None
    # …only a run that arrives AFTER the prediction does
    from insight_fixtures import _run, _scenario, _step
    fake = main_mod.app.state.ingestor.reader._fetch.__self__
    future = _run("run-9", "2027-01-01T00:00:00+00:00",
                  [_scenario("sc-broken", "failed",
                             [_step("s1", "failed", "connection refused")])])
    fake.runs["run-9"] = future
    main_mod.app.state.ingestor.sync()
    assert client.get("/status").json()["predictor"]["brier"] is not None
    # the calibration trend buckets those scored predictions by day
    cal = client.get("/insights/predictions/calibration").json()
    assert cal["overall_brier"] is not None and cal["trend"]
    bucket = cal["trend"][-1]
    assert set(bucket) == {"day", "brier", "n", "basis_model_share"}
    assert bucket["n"] >= 1 and 0.0 <= bucket["brier"] <= 1.0


def test_calibration_empty_and_day_cap(client):
    cal = client.get("/insights/predictions/calibration?days=9999").json()
    assert cal["days"] == 365  # capped
    assert cal["trend"] == [] and cal["overall_brier"] is None


def test_self_train_loop_ticks(monkeypatch):
    """With INSIGHT_TRAIN_INTERVAL_SEC set, the lifespan loop ingests without
    any explicit POST /train."""
    import time

    monkeypatch.setenv("INSIGHT_TRAIN_INTERVAL_SEC", "0.05")
    fake = FakeOrchestrator(build_history())
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    main_mod.app.state.store = store
    main_mod.app.state.reader = reader
    main_mod.app.state.ingestor = Ingestor(store, reader)
    with TestClient(main_mod.app) as c:  # context manager runs the lifespan
        deadline = time.time() + 3
        while time.time() < deadline and store.corpus_size() == 0:
            time.sleep(0.05)
        assert store.corpus_size() > 0  # a tick ingested the history
        assert c.get("/status").json()["corpus_size"] > 0


def test_infer_categorize_and_predict(client):
    client.post("/train")  # build history so predict_outcome has data
    cat = client.post("/infer", json={
        "kind": "categorize", "text": "connection refused by 10.0.0.1:8583"})
    assert cat.json()["heuristic"] == "unreachable"
    pred = client.post("/infer", json={
        "kind": "predict_outcome", "scenario_id": "sc-broken"})
    assert pred.json()["p_fail_next"] > 0.5
    # side-effect-free: scenario control flow must not pollute self-scoring
    assert client.get("/status").json()["predictor"]["brier"] is None


def test_infer_explain_carries_reason_and_fix(client):
    out = client.post("/infer", json={
        "kind": "explain", "text": "read timed out after 5000 ms"}).json()
    assert out["heuristic"] == "timeout"
    assert out["why"] and out["fix"]
    assert "Suggested fix" in out["explanation"]


def test_infer_model_pinning_404s_on_unknown(client):
    r = client.post("/infer", json={
        "kind": "categorize", "text": "x", "model_id": "cm-nope"})
    assert r.status_code == 404


def test_infer_validation(client):
    assert client.post("/infer", json={"kind": "categorize"}).status_code == 422
    assert client.post("/infer", json={
        "kind": "predict_outcome"}).status_code == 422
    assert client.post("/infer", json={
        "kind": "predict_outcome", "scenario_id": "nope"}).status_code == 404
    assert client.post("/infer", json={"kind": "wat"}).status_code == 422


def test_auth_gate_fails_closed(client, monkeypatch):
    monkeypatch.setenv("PAYPROBE_ENV", "prod")
    r = client.get("/insights/categories")
    assert r.status_code == 503  # nothing configured -> refuse to serve
    monkeypatch.setenv("API_TOKEN", "sekret")
    assert client.get("/insights/categories").status_code == 401
    ok = client.get("/insights/categories",
                    headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
    # health + status stay public for the dashboard probes
    assert client.get("/health").status_code == 200
    assert client.get("/status").status_code == 200
