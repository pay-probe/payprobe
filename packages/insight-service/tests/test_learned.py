"""Learned categorizer — exercised only when the optional ml extra is present.

Without scikit-learn these tests SKIP (not fail): the learned layer is
optional by design and the baselines are the permanent fallback (ADR-0005).
"""
from __future__ import annotations

import pytest

from insight_service.categorize import HAVE_SKLEARN, LearnedCategorizer, categorize
from insight_service.normalize import normalize

pytestmark = pytest.mark.skipif(not HAVE_SKLEARN,
                                reason="scikit-learn not installed (optional)")


def _corpus() -> list[dict]:
    rows = []
    for i in range(30):
        rows.append({"norm": normalize(
            f"read timed out after {1000 + i} ms connecting to issuer-{i}:85{i:02d}"),
            "heuristic": "timeout"})
        rows.append({"norm": normalize(
            f"connect to 10.0.0.{i}:8583 failed: connection refused"),
            "heuristic": "unreachable"})
        rows.append({"norm": normalize(
            f"payShield stream error 0x{i:02X}: handler is closed (conn {i})"),
            "heuristic": "error"})
    return rows


def test_fit_refuses_tiny_corpus():
    lc = LearnedCategorizer()
    assert lc.fit([{"norm": "x", "heuristic": "error"}] * 10) is False
    assert lc.fitted is False


def test_fit_and_predict_stable_ids():
    lc = LearnedCategorizer(n_clusters=3)
    assert lc.fit(_corpus()) is True
    hit = lc.predict("read timed out after 9999 ms connecting to visa:8583")
    assert hit["category"].startswith("ins-cat-")
    assert hit["heuristic"] == "timeout"
    assert hit["model_version"] == lc.version
    # same meaning after a refit on the same corpus -> same stable id
    lc2 = LearnedCategorizer(n_clusters=3)
    lc2.fit(_corpus())
    hit2 = lc2.predict("read timed out after 1 ms connecting to amex:9999")
    assert hit2["category"] == hit["category"]


def test_novel_cluster_carries_novel_flag():
    lc = LearnedCategorizer(n_clusters=3)
    lc.fit(_corpus())
    hit = lc.predict("payShield stream error 0xFF: handler is closed (conn 9)")
    assert hit["novel"] is True  # majority heuristic in that cluster is "error"


def test_categorize_prefers_learned_when_fitted():
    lc = LearnedCategorizer(n_clusters=3)
    lc.fit(_corpus())
    cat = categorize("connection refused talking to 10.9.9.9:8583", learned=lc)
    assert cat["model_version"] == lc.version
    assert cat["heuristic"] == "unreachable"
