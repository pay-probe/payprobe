"""Custom model training + artifact persistence (ADR-0005 extension).

Users provide labeled datasets (rows of ``{text, label}`` — typically raw
error/decline text and *their* taxonomy) and train a **supervised**
categorizer from them. It outranks the unsupervised cluster layer, which
outranks the heuristic floor. Everything stays advisory.

Artifacts are pickled to ``INSIGHT_DATA_DIR`` (default: beside the SQLite db)
and reloaded at boot, so both the custom models AND the auto-trained cluster
model finally survive restarts — this module owns that persistence.

Dataset text passes through :func:`normalize` before training and storage,
same PAN-safety rule as the ingested corpus.
"""
from __future__ import annotations

import os
import pickle
import random
from datetime import datetime, timezone
from pathlib import Path

from .normalize import normalize

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    HAVE_SKLEARN = True
except ImportError:  # pragma: no cover
    HAVE_SKLEARN = False

#: minimum rows / distinct labels a training set must have
MIN_ROWS = 20
MIN_LABELS = 2
HOLDOUT = 0.2


def data_dir() -> Path | None:
    """Artifact directory: ``INSIGHT_DATA_DIR`` env, else beside a file-backed
    ``INSIGHT_DB``, else None (in-memory dev/tests: no persistence)."""
    if d := os.environ.get("INSIGHT_DATA_DIR"):
        p = Path(d)
    else:
        db = os.environ.get("INSIGHT_DB", ":memory:")
        if db == ":memory:":
            return None
        p = Path(db).expanduser().parent / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


class SupervisedCategorizer:
    """TF-IDF + logistic regression over user-labeled failure text."""

    def __init__(self, model_id: str, name: str) -> None:
        self.model_id = model_id
        self.name = name
        self.version = "{}-{}".format(
            model_id, datetime.now(timezone.utc).strftime("%Y%m%d"))
        self._vectorizer = None
        self._clf = None
        self.labels: list[str] = []
        self.metrics: dict = {}

    def fit(self, rows: list[dict]) -> dict:
        """``rows``: [{text, label}]. Trains on a shuffled 80/20 split and
        reports holdout accuracy (the honesty number shown in the registry).
        Raises ValueError on an unusable dataset."""
        if not HAVE_SKLEARN:
            raise ValueError("scikit-learn is not installed — the service "
                             "runs baselines only (install the [ml] extra)")
        rows = [{"text": normalize(r.get("text")), "label": str(r.get("label", "")).strip()}
                for r in rows if r.get("text") and str(r.get("label", "")).strip()]
        if len(rows) < MIN_ROWS:
            raise ValueError(f"need at least {MIN_ROWS} labeled rows, got {len(rows)}")
        labels = sorted({r["label"] for r in rows})
        if len(labels) < MIN_LABELS:
            raise ValueError(f"need at least {MIN_LABELS} distinct labels, got {len(labels)}")

        rng = random.Random(7)
        rng.shuffle(rows)
        cut = max(1, int(len(rows) * HOLDOUT))
        hold, train = rows[:cut], rows[cut:]
        # every label must appear in the training split
        train_labels = {r["label"] for r in train}
        hold = [r for r in hold if r["label"] in train_labels] or hold
        if {r["label"] for r in train} != set(labels):
            train, hold = rows, rows[:cut]  # tiny dataset: train on all

        self._vectorizer = TfidfVectorizer(max_features=4000, ngram_range=(1, 2))
        X = self._vectorizer.fit_transform([r["text"] for r in train])
        self._clf = LogisticRegression(max_iter=1000)
        self._clf.fit(X, [r["label"] for r in train])
        self.labels = list(self._clf.classes_)

        Xh = self._vectorizer.transform([r["text"] for r in hold])
        correct = sum(1 for pred, r in zip(self._clf.predict(Xh), hold)
                      if pred == r["label"])
        self.metrics = {
            "rows": len(rows), "labels": labels,
            "train_rows": len(train), "holdout_rows": len(hold),
            "holdout_accuracy": round(correct / len(hold), 4) if hold else None,
        }
        return self.metrics

    def predict(self, error: str | None) -> dict | None:
        if not error or self._clf is None:
            return None
        X = self._vectorizer.transform([normalize(error)])
        label = str(self._clf.predict(X)[0])
        proba = float(max(self._clf.predict_proba(X)[0]))
        return {
            "category": label,
            "label": label,
            "confidence": round(proba, 3),
            "model_version": self.version,
            "novel": False,
            "custom": True,
        }

    # -- artifact persistence ---------------------------------------------

    def save(self, directory: Path) -> str:
        path = directory / f"{self.model_id}.pkl"
        with open(path, "wb") as fh:
            pickle.dump(self, fh)
        return str(path)

    @staticmethod
    def load(path: str) -> "SupervisedCategorizer | None":
        try:
            with open(path, "rb") as fh:
                obj = pickle.load(fh)
            return obj if isinstance(obj, SupervisedCategorizer) else None
        except (OSError, pickle.UnpicklingError, AttributeError, EOFError):
            return None


# -- cluster-model persistence (the pre-existing learned layer) ----------------

CLUSTER_ARTIFACT = "cluster-categorizer.pkl"


def save_cluster_model(learned, directory: Path | None) -> str | None:
    """Pickle the auto-trained LearnedCategorizer so a restart doesn't lose it."""
    if directory is None or not getattr(learned, "fitted", False):
        return None
    path = directory / CLUSTER_ARTIFACT
    try:
        with open(path, "wb") as fh:
            pickle.dump(learned, fh)
        return str(path)
    except OSError:
        return None


def load_cluster_model(directory: Path | None):
    if directory is None:
        return None
    path = directory / CLUSTER_ARTIFACT
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        return obj if getattr(obj, "fitted", False) else None
    except (OSError, pickle.UnpicklingError, AttributeError, EOFError):
        return None
