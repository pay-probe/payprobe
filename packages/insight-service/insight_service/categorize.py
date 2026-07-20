"""Failure categorization — heuristic floor, optional learned layer.

The floor is ``report_service.diagnose.classify_error`` (the shared 10-category
regex taxonomy) — deterministic, always available, the permanent fallback.

The learned layer (scikit-learn, OPTIONAL dependency) discovers categories by
clustering the normalized failure corpus (TF-IDF + KMeans). Each cluster gets a
**stable id** derived from its top terms (``ins-cat-<hash>``) so retraining on
a grown corpus keeps ids for clusters that kept their meaning. Every label
carries ``model_version``; heuristic labels use version ``"heuristic-1"``.

A failure is ``novel`` when the heuristic ladder couldn't name it
(``error`` / ``unknown``) — the count of these is the metric that justifies
(or kills) the learned layer per ADR-0005's phase gate.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from report_service.diagnose import classify_error, _CATEGORY_HELP

from .featurize import training_text
from .normalize import normalize

HEURISTIC_VERSION = "heuristic-1"
#: heuristic buckets that mean "no rule anticipated this shape"
NOVEL_HEURISTICS = ("error", "unknown")

try:  # the learned layer is optional by design (ADR-0005)
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    HAVE_SKLEARN = True
except ImportError:  # pragma: no cover - exercised via LearnedCategorizer guard
    HAVE_SKLEARN = False


def category_help(heuristic: str) -> dict:
    """Static explanation + fix hint for a heuristic category."""
    what, fix = _CATEGORY_HELP.get(heuristic, _CATEGORY_HELP["unknown"])
    return {"what": what, "fix": fix}


def categorize(error: str | None, learned: "LearnedCategorizer | None" = None,
               custom=None, feats: str = "") -> dict:
    """Categorize one failure. Precedence: an ACTIVE custom supervised model
    (user-trained on their own taxonomy) → the unsupervised cluster layer →
    the heuristic floor. The heuristic taxonomy is always computed and
    returned, whoever answers."""
    heuristic = classify_error(error)
    novel = heuristic in NOVEL_HEURISTICS
    base = {
        "category": heuristic,
        "label": heuristic,
        "heuristic": heuristic,
        "confidence": 1.0 if not novel else 0.3,
        "model_version": HEURISTIC_VERSION,
        "novel": novel,
    }
    enriched = training_text(feats, error or "") if feats else error
    if custom is not None:
        hit = custom.predict(enriched)
        if hit is not None:
            base.update(hit)
            return base
    if learned is not None and learned.fitted:
        hit = learned.predict(enriched)
        if hit is not None:
            base.update(hit)
    return base


class LearnedCategorizer:
    """TF-IDF + KMeans over the normalized corpus. Deliberately small: the
    feature extractor and the stable-id scheme are the point, not the model."""

    #: below this corpus size clustering is noise — stay on the heuristics
    MIN_CORPUS = 50

    def __init__(self, n_clusters: int = 12) -> None:
        self.n_clusters = n_clusters
        self.fitted = False
        self.version = ""
        self._vectorizer = None
        self._model = None
        self._cluster_ids: dict[int, str] = {}
        self._cluster_labels: dict[int, str] = {}
        self._cluster_heuristic: dict[int, str] = {}

    def fit(self, corpus: list[dict]) -> bool:
        """corpus rows: {"norm": str, "heuristic": str}. Returns True if a
        model was fitted (enough data + sklearn present)."""
        if not HAVE_SKLEARN or len(corpus) < self.MIN_CORPUS:
            return False
        texts = [training_text(row.get("feats", ""), row["norm"])
                 for row in corpus]
        # Normalization collapses instances into shapes — the number of
        # UNIQUE shapes, not rows, bounds how many clusters make sense.
        # Fewer clusters than shapes makes assignment of look-alike shapes
        # nondeterministic across fits; fewer than 2 shapes isn't clusterable.
        uniq = len(set(texts))
        if uniq < 2:
            return False
        n = max(2, min(self.n_clusters, uniq))
        self._vectorizer = TfidfVectorizer(max_features=2000, ngram_range=(1, 2))
        X = self._vectorizer.fit_transform(texts)
        self._model = KMeans(n_clusters=n, n_init=5, random_state=7)
        assignments = self._model.fit_predict(X)

        terms = self._vectorizer.get_feature_names_out()
        for c in range(n):
            centroid = self._model.cluster_centers_[c]
            top = [terms[i] for i in centroid.argsort()[::-1][:4]]
            # Stable id: hash of the cluster's defining terms, not its index —
            # a retrain that keeps the meaning keeps the id.
            digest = hashlib.sha1(" ".join(sorted(top)).encode()).hexdigest()[:8]
            self._cluster_ids[c] = f"ins-cat-{digest}"
            self._cluster_labels[c] = " / ".join(top[:3])
            members = [corpus[i]["heuristic"]
                       for i, a in enumerate(assignments) if a == c]
            self._cluster_heuristic[c] = (
                max(set(members), key=members.count) if members else "unknown")

        self.version = "cat-{}-n{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%d"), len(corpus))
        self.fitted = True
        return True

    def predict(self, error: str | None) -> dict | None:
        if not self.fitted or not error:
            return None
        X = self._vectorizer.transform([normalize(error)])
        cluster = int(self._model.predict(X)[0])
        # confidence: inverse distance to the assigned centroid, squashed
        dist = float(self._model.transform(X)[0][cluster])
        confidence = round(1.0 / (1.0 + dist), 3)
        heuristic = self._cluster_heuristic[cluster]
        return {
            "category": self._cluster_ids[cluster],
            "label": self._cluster_labels[cluster],
            "heuristic": heuristic,
            "confidence": confidence,
            "model_version": self.version,
            "novel": heuristic in NOVEL_HEURISTICS,
        }
