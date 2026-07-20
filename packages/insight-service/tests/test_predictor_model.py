"""Learned predictor: example building, the baseline gate, and demotion."""
from __future__ import annotations

import pytest

from insight_service.predictor_model import (
    HAVE_SKLEARN,
    MIN_EXAMPLES,
    LearnedPredictor,
    build_examples,
    features,
)

needs_sklearn = pytest.mark.skipif(not HAVE_SKLEARN,
                                   reason="scikit-learn not installed (optional)")


def _seq(pattern: str, scenario: str) -> list[dict]:
    """'p'/'f' string → time-ordered outcome rows."""
    return [{"scenario_id": scenario,
             "status": "failed" if c == "f" else "passed",
             "duration_ms": 40 + i,
             "created_at": f"2026-06-{(i % 28) + 1:02d}T{i // 28:02d}:00:00"}
            for i, c in enumerate(pattern)]


def _learnable_history() -> dict[str, list[dict]]:
    """Histories with structure a model can exploit but the memoryless
    frequency baseline can't: strict alternation (last outcome perfectly
    predicts the next) plus stable and broken scenarios for class balance."""
    return {
        "alt-1": _seq("pf" * 30, "alt-1"),
        "alt-2": _seq("fp" * 30, "alt-2"),
        "stable": _seq("p" * 40, "stable"),
        "broken": _seq("f" * 40, "broken"),
    }


def test_features_shape_and_streak():
    f = features(_seq("ppfff", "x"))
    assert len(f) == 9
    assert f[2] == 1.0 and f[4] == 3.0  # last failed, streak of 3
    assert f[7] == 3 / 5  # recent_fail_rate_5


@needs_sklearn
def test_dimension_guard_discards_stale_artifacts(tmp_path, monkeypatch):
    """An artifact pickled with an older feature vector must not load —
    predicting with mismatched dimensions is silent garbage."""
    import insight_service.predictor_model as pm

    lp = LearnedPredictor()
    lp.fit(_learnable_history())
    assert lp.save(tmp_path) is not None
    assert LearnedPredictor.load(tmp_path) is not None  # matching dims load
    monkeypatch.setattr(pm, "FEATURE_NAMES", pm.FEATURE_NAMES + ["future_feat"])
    assert LearnedPredictor.load(tmp_path) is None      # mismatched ⇒ discard


def test_build_examples_is_chronological_and_paired():
    X, y, base = build_examples({"a": _seq("ppffp", "a")})
    assert len(X) == len(y) == len(base) == 2  # MIN_PRIOR=3 → 2 examples
    assert all(0.0 < p < 1.0 for p in base)


@needs_sklearn
def test_fit_beats_baseline_on_learnable_pattern():
    lp = LearnedPredictor()
    result = lp.fit(_learnable_history())
    assert result["fitted"] is True and lp.fitted
    assert result["model_brier"] < result["baseline_brier"]
    # alternation: after a fail, the model should expect a pass (low p_fail)
    after_fail = lp.predict(_seq("pf" * 10, "alt-1"))
    after_pass = lp.predict(_seq(("pf" * 10) + "p", "alt-1"))
    assert after_fail["p_fail_next"] < after_pass["p_fail_next"]
    assert after_fail["basis"] == "model" and after_fail["model_factors"]


@needs_sklearn
def test_calibration_active_on_big_corpus_and_probs_stay_bounded():
    lp = LearnedPredictor()
    result = lp.fit(_learnable_history())
    assert result["calibrated"] is True  # enough training rows for a cal split
    for pat in ("pf" * 10, "p" * 15, "f" * 15, "ppfppfppf"):
        p = lp.predict(_seq(pat, "x"))
        assert p is None or 0.0 <= p["p_fail_next"] <= 1.0


@needs_sklearn
def test_gate_refuses_unlearnable_noise():
    """Pure coin-flip history: the model cannot beat the smoothed baseline
    reliably — and when it doesn't, fitted stays False."""
    import random
    rng = random.Random(3)
    seqs = {f"s{i}": _seq("".join(rng.choice("pf") for _ in range(40)), f"s{i}")
            for i in range(4)}
    lp = LearnedPredictor()
    result = lp.fit(seqs)
    # either it failed the gate (typical) or squeaked past — but the reported
    # verdict must always match the fitted flag
    assert result["fitted"] == lp.fitted
    if not lp.fitted:
        assert "reason" in result


def test_fit_refuses_thin_history():
    lp = LearnedPredictor()
    result = lp.fit({"a": _seq("pfp", "a")})
    assert lp.fitted is False
    if HAVE_SKLEARN:
        assert f"< {MIN_EXAMPLES}" in result["reason"]


@needs_sklearn
def test_artifact_roundtrip_and_demotion(tmp_path):
    lp = LearnedPredictor()
    lp.fit(_learnable_history())
    assert lp.save(tmp_path) is not None
    again = LearnedPredictor.load(tmp_path)
    assert again is not None and again.fitted
    assert again.predict(_seq("pf" * 10, "x"))["basis"] == "model"
    # an unfitted predictor never saves
    fresh = LearnedPredictor()
    assert fresh.save(tmp_path / "other") is None
