"""Drift watch: health snapshots, the staleness rule, /status surfacing."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
from insight_service.ingest import Ingestor
from insight_service.rest import OrchestratorReader
from insight_service.store import InsightStore

from insight_fixtures import FakeOrchestrator, build_history, _run, _scenario, _step


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("INSIGHT_DATA_DIR", str(tmp_path / "models"))
    fake = FakeOrchestrator(build_history())
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    main_mod.app.state.store = store
    main_mod.app.state.reader = reader
    main_mod.app.state.ingestor = Ingestor(store, reader)
    return TestClient(main_mod.app)


def test_train_records_baseline_and_fresh_model_is_not_stale(client):
    client.post("/train")
    drift = client.get("/status").json()["categorizer"]["drift"]
    assert drift["stale"] is False
    assert drift["latest"]["corpus_size"] > 0
    assert drift["at_fit"]["novel_share"] >= 0


def test_novel_flood_flags_stale(client):
    client.post("/train")
    fake = main_mod.app.state.ingestor.reader._fetch.__self__
    # a wave of failures no rule anticipates (heuristic error/unknown)
    for i in range(3):
        rid = f"run-w{i}"
        fake.runs[rid] = _run(rid, f"2027-02-0{i + 1}T00:00:00+00:00", [
            _scenario(f"sc-weird-{i}-{j}", "failed",
                      [_step("s1", "failed",
                             f"quantum flux inverter {i}-{j} misaligned badly")])
            for j in range(6)
        ])
    main_mod.app.state.ingestor.sync()  # snapshots health, does NOT refit
    drift = client.get("/status").json()["categorizer"]["drift"]
    assert drift["stale"] is True
    assert "novel share" in drift["reason"]
    # retraining resets the baseline to the new landscape → healthy again
    client.post("/train")
    drift2 = client.get("/status").json()["categorizer"]["drift"]
    assert drift2["stale"] is False


def test_no_baseline_means_no_verdict(client):
    # snapshots exist (sync) but no fit yet → never cry stale without a baseline
    main_mod.app.state.ingestor.sync()
    drift = main_mod.app.state.ingestor.drift()
    assert drift["stale"] is False