"""Active-learning label queue + bulk-by-signature corrections."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
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
    c.post("/train")
    return c


def test_queue_ranks_novel_shapes_first_and_dedupes(client):
    q = client.get("/insights/label-queue").json()
    assert q["advisory_only"] is True
    queue = q["queue"]
    # canned history: 3 distinct shapes (timeout / refused / payShield novel)
    # + the assertion-mismatch shape from sc-decline
    sigs = [e["signature"] for e in queue]
    assert len(sigs) == len(set(sigs))  # one entry per shape
    # the payShield failure is the only NOVEL shape → must rank first
    assert queue[0]["current"]["novel"] is True
    assert "payshield" in queue[0]["sample_text"]
    # frequent shapes carry their impact count (refused appears every run)
    refused = next(e for e in queue if "refused" in e["sample_text"])
    assert refused["count"] == 8


def test_bulk_correction_clears_the_shape_from_queue(client):
    q = client.get("/insights/label-queue").json()["queue"]
    top = q[0]
    out = client.post("/insights/corrections", json={
        "run_id": top["run_id"], "scenario_id": top["scenario_id"],
        "step_id": top["step_id"], "label": "hsm-socket-drop",
        "apply_to_signature": True}).json()
    assert out["failures_relabelled"] >= 1
    # every instance of the shape now carries the human label…
    store: InsightStore = main_mod.app.state.store
    rows = [r for r in store.corpus() if r["signature"] == top["signature"]]
    assert rows and all(r["category"] == "hsm-socket-drop" for r in rows)
    assert all(r["model_version"] == "human-correction" for r in rows)
    # …and the shape leaves the label queue
    sigs = [e["signature"] for e in
            client.get("/insights/label-queue").json()["queue"]]
    assert top["signature"] not in sigs


def test_queue_limit_clamped(client):
    assert len(client.get("/insights/label-queue?limit=1").json()["queue"]) == 1