"""Feature channels: token derivation, normalize-proofing, load-run corpus."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
from insight_service.categorize import HAVE_SKLEARN, LearnedCategorizer
from insight_service.featurize import context_tokens, training_text
from insight_service.ingest import Ingestor
from insight_service.normalize import normalize
from insight_service.rest import OrchestratorReader
from insight_service.store import InsightStore

from insight_fixtures import FakeOrchestrator, build_history


# -- token derivation ----------------------------------------------------------

def test_channels_from_a_failed_step():
    step = {
        "target": "hsm-primary", "action": "send_command",
        "duration_ms": 4200,
        "response": {"response_code": "05"},
        "assertions": [{"field": "response_code", "passed": False},
                       {"field": "rrn", "passed": True}],
    }
    toks = context_tokens(step, environment="staging")
    assert "tgt_hsm_primary" in toks
    assert "act_send_command" in toks
    assert "rc_af" in toks            # "05" digit-transliterated (0→a, 5→f)
    assert "env_staging" in toks
    assert "dur_slow" in toks
    assert "asrt_response_code" in toks
    assert "asrt_rrn" not in toks     # passing assertions are not channels


def test_channels_survive_normalize_unchanged():
    """The whole point of digit transliteration: normalize() must not mangle
    a single channel token, or train/predict texts diverge."""
    step = {"target": "gw1", "duration_ms": 50,
            "response": {"status_code": 503, "body": {"response_code": "91"}}}
    toks = context_tokens(step)
    assert normalize(toks) == toks.lower()


def test_http_class_and_nested_response_code():
    toks = context_tokens({"response": {"status_code": 404,
                                        "body": {"response_code": "05"}}})
    assert "http_exx" in toks  # 4xx → digit-transliterated class (4→e)
    assert "rc_af" in toks


def test_extra_context_wins_and_source_token():
    toks = context_tokens({"target": "a"},
                          extra={"target": "b", "source": "load"})
    assert "tgt_b" in toks and "tgt_a" not in toks
    assert "src_load" in toks


# -- channels reach the corpus + models ------------------------------------------

def _client(monkeypatch, tmp_path, load_runs=None):
    monkeypatch.setenv("INSIGHT_DATA_DIR", str(tmp_path / "models"))
    fake = FakeOrchestrator(build_history(), load_runs=load_runs)
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    main_mod.app.state.store = store
    main_mod.app.state.reader = reader
    main_mod.app.state.ingestor = Ingestor(store, reader)
    return TestClient(main_mod.app)


def test_ingest_stores_channels(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/train")
    store: InsightStore = main_mod.app.state.store
    row = next(r for r in store.corpus() if r["feats"])
    assert "tgt_" in row["feats"] and "env_staging" in row["feats"]


@pytest.mark.skipif(not HAVE_SKLEARN, reason="sklearn optional")
def test_channels_separate_identical_text():
    """Same error text, different targets → separable clusters ONLY because
    of the channel tokens."""
    corpus = []
    for i in range(30):
        corpus.append({"norm": "read timed out after <n> ms",
                       "feats": "tgt_hsm act_send_command",
                       "heuristic": "timeout"})
        corpus.append({"norm": "read timed out after <n> ms",
                       "feats": "tgt_rest_gateway act_send_auth_request",
                       "heuristic": "timeout"})
    lc = LearnedCategorizer(n_clusters=2)
    assert lc.fit(corpus) is True
    a = lc.predict(training_text("tgt_hsm act_send_command",
                                 "read timed out after <n> ms"))
    b = lc.predict(training_text("tgt_rest_gateway act_send_auth_request",
                                 "read timed out after <n> ms"))
    assert a["category"] != b["category"]


def test_infer_accepts_context(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    out = c.post("/infer", json={
        "kind": "categorize", "text": "read timed out",
        "context": {"target": "hsm", "response_code": "05",
                    "environment": "prod"}}).json()
    assert out["heuristic"] == "timeout"  # channels never break the floor


# -- load-run ingestion -----------------------------------------------------------

def test_load_runs_feed_taxonomy_with_weights(monkeypatch, tmp_path):
    load_run = {
        "id": "lr-1", "status": "completed", "label": "soak-nightly",
        "created_at": "2026-07-09T00:00:00+00:00",
        "target": "switch_visa",
        "error_categories": {"connect/TimeoutError": 40000, "send/BrokenPipe": 7},
    }
    c = _client(monkeypatch, tmp_path, load_runs=[load_run])
    r = c.post("/train").json()
    assert r["sync"]["load_failure_rows"] == 2
    # impact-weighted counts flow into the taxonomy…
    cats = c.get("/insights/categories").json()["categories"]
    total = sum(cat["count"] for cat in cats)
    assert total >= 40007
    # …and into the label queue. Ranking stays honest: the NOVEL BrokenPipe
    # shape outranks the 40K-count TimeoutError (heuristic already names it
    # confidently — nothing to learn from labeling it, so it scores 0).
    q = c.get("/insights/label-queue").json()["queue"]
    top_texts = " ".join(e["sample_text"] for e in q[:2])
    assert "brokenpipe" in top_texts
    big = next(e for e in q if "timeouterror" in e["sample_text"])
    assert big["count"] == 40000 and big["score"] == 0
    assert "src_load" in main_mod.app.state.store.failures_for_run("lr-1")[0]["feats"]
    # idempotent: second sync adds nothing
    assert c.post("/train").json()["sync"]["load_failure_rows"] == 0