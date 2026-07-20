"""Core insight behaviors over the canned history (offline, fake-first)."""
from __future__ import annotations

from insight_service.categorize import categorize
from insight_service.explain import evidence_pack, render_text
from insight_service.normalize import normalize, signature
from insight_service.predict import predict_outcomes


# -- normalization / privacy -------------------------------------------------

def test_normalize_masks_instances_but_keeps_shape():
    a = normalize("connect to 10.1.2.3:8583 failed: connection refused")
    b = normalize("connect to 10.9.9.9:9090 failed: connection refused")
    assert a == b
    assert "10.1" not in a and "8583" not in a


def test_normalize_masks_pan_like_digit_runs():
    n = normalize("declined for card 4111111111111111 at terminal 42")
    assert "4111" not in n and "<" in n  # digits masked


def test_signature_groups_same_failure_shape():
    t1 = "read timed out after 5000 ms connecting to issuer-a:8583"
    t2 = "read timed out after 3200 ms connecting to issuer-b:9001"
    assert signature(t1) == signature(t2)


# -- categorization -----------------------------------------------------------

def test_heuristic_categories_from_shared_taxonomy():
    assert categorize("connection refused")["heuristic"] == "unreachable"
    assert categorize("read timed out")["heuristic"] == "timeout"
    assert categorize("expected response_code 00 got 05")["heuristic"] == "assertion"


def test_novel_failures_are_flagged():
    cat = categorize("payShield stream error 0xBEEF: handler is closed")
    assert cat["novel"] is True
    assert cat["heuristic"] in ("error", "unknown")
    assert cat["model_version"] == "heuristic-1"


# -- ingestion ----------------------------------------------------------------

def test_sync_ingests_runs_and_failures(ingestor, store):
    result = ingestor.sync()
    assert result["runs_ingested"] == 8
    assert result["failures_added"] > 0
    # incremental: nothing new the second time
    assert ingestor.sync()["runs_ingested"] == 0
    # outcomes recorded for prediction
    assert len(store.outcomes(scenario_id="sc-flaky")) == 8


def test_ingest_is_idempotent(ingestor, store):
    ingestor.sync()
    n = store.corpus_size()
    store.set_meta("last_run_created_at", "")  # force re-walk
    ingestor.sync()
    assert store.corpus_size() == n  # UNIQUE(run, scenario, step) held


# -- prediction ---------------------------------------------------------------

def test_prediction_orders_broken_over_flaky_over_stable(ingestor, store):
    ingestor.sync()
    p = {sid: predict_outcomes(store.outcomes(scenario_id=sid), scenario_id=sid)
         for sid in ("sc-broken", "sc-flaky", "sc-stable")}
    assert p["sc-broken"]["p_fail_next"] > p["sc-flaky"]["p_fail_next"] \
        > p["sc-stable"]["p_fail_next"]
    assert p["sc-flaky"]["p_flaky"] > p["sc-broken"]["p_flaky"]
    assert p["sc-broken"]["p_flaky"] == 0.0  # always-fail is regression, not flaky


def test_prediction_is_honest_about_thin_history():
    pred = predict_outcomes([{"status": "failed"}], scenario_id="sc-new")
    assert pred["n_history"] == 1
    assert 0.3 < pred["p_fail_next"] < 0.9  # prior-dominated, not certain
    assert any("prior" in f for f in pred["top_factors"])
    assert pred["basis"] == "frequency"


def test_prediction_flags_all_recent_failures(ingestor, store):
    ingestor.sync()
    pred = predict_outcomes(store.outcomes(scenario_id="sc-broken"),
                            scenario_id="sc-broken")
    assert any("all failed" in f for f in pred["top_factors"])


# -- explanation --------------------------------------------------------------

def _pack_for(store, ingestor, run_id, scenario_id):
    row = next(r for r in store.failures_for_run(run_id)
               if r["scenario_id"] == scenario_id)
    cat = categorize(row["norm"], ingestor.learned)
    history = store.outcomes(scenario_id=scenario_id)
    similar = store.similar_failures(row["signature"], exclude_run=run_id)
    return evidence_pack({**row, **cat}, history, similar)


def test_explanation_standing_regression(ingestor, store):
    ingestor.sync()
    pack = _pack_for(store, ingestor, "run-8", "sc-broken")
    assert pack["history"]["kind"] == "always_failing"
    assert pack["heuristic"] == "unreachable"
    assert len(pack["similar_failures"]) > 0  # same shape in earlier runs
    text = render_text(pack)
    assert "every recorded run" in text and "Suggested fix" in text


def test_explanation_recovered_signal(ingestor, store):
    ingestor.sync()
    # sc-decline failed runs 3-4 then recovered — the earlier identical
    # failure followed by passes is the "fixable" evidence.
    pack = _pack_for(store, ingestor, "run-4", "sc-decline")
    assert pack["recovered_after_similar"] is True
    assert pack["heuristic"] == "assertion"


def test_explanation_includes_passed_signals(ingestor, store):
    ingestor.sync()
    row = store.failures_for_run("run-8")[0]
    cat = categorize(row["norm"], ingestor.learned)
    pack = evidence_pack({**row, **cat}, [], [],
                         signals={"chaos_storm_active": "hsm outage"})
    assert "chaos_storm_active" in render_text(pack)
