"""Human-in-the-loop corrections: dataset feed + ground-truth relabeling."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
from insight_service.categorize import HAVE_SKLEARN
from insight_service.ingest import Ingestor
from insight_service.rest import OrchestratorReader
from insight_service.store import InsightStore

from insight_fixtures import FakeOrchestrator, build_history


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("INSIGHT_DATA_DIR", str(tmp_path / "models"))
    fake = FakeOrchestrator(build_history())
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    main_mod.app.state.store = store
    main_mod.app.state.reader = reader
    main_mod.app.state.ingestor = Ingestor(store, reader)
    c = TestClient(main_mod.app)
    c.post("/train")  # ingest the canned history
    return c


def _correct(c: TestClient, label: str = "hsm-socket-drop") -> dict:
    return c.post("/insights/corrections", json={
        "run_id": "run-8", "scenario_id": "sc-hsm",
        "step_id": "s1", "label": label}).json()


def test_correction_feeds_reserved_dataset(client):
    out = _correct(client)
    assert out["dataset_id"] == "ds-corrections"
    assert out["corrections_total"] == 1
    ds = client.get("/datasets/ds-corrections").json()
    assert ds["name"] == "Operator corrections"
    assert ds["labels"] == {"hsm-socket-drop": 1}
    # a second correction appends, not replaces
    _correct(client, "hsm-key-problem")
    assert client.get("/datasets/ds-corrections").json()["row_count"] == 2


def test_correction_relabels_failure_as_ground_truth(client):
    _correct(client)
    body = client.get("/insights/failures/run-8").json()
    hsm = next(f for f in body["failures"] if f["scenario_id"] == "sc-hsm")
    assert hsm["category"] == "hsm-socket-drop"
    assert hsm["model_version"] == "human-correction"
    assert hsm["confidence"] == 1.0 and hsm["novel"] is False
    # and it shows in the taxonomy counts
    cats = {c["category"] for c in
            client.get("/insights/categories").json()["categories"]}
    assert "hsm-socket-drop" in cats


@pytest.mark.skipif(not HAVE_SKLEARN, reason="sklearn optional")
def test_retrain_never_overwrites_human_labels(client):
    _correct(client)
    # force a learned refit over the corpus — the corrected row must survive
    ing = main_mod.app.state.ingestor
    ing.learned.MIN_CORPUS = 1  # tiny corpus, force a fit
    client.post("/train")
    store: InsightStore = main_mod.app.state.store
    row = next(r for r in store.failures_for_run("run-8")
               if r["scenario_id"] == "sc-hsm")
    assert row["category"] == "hsm-socket-drop"
    assert row["model_version"] == "human-correction"


def test_correction_validation(client):
    r = client.post("/insights/corrections", json={
        "run_id": "run-8", "scenario_id": "sc-hsm", "step_id": "s1",
        "label": "  "})
    assert r.status_code == 422
    r = client.post("/insights/corrections", json={
        "run_id": "run-8", "scenario_id": "nope", "step_id": "s1",
        "label": "x"})
    assert r.status_code == 404
