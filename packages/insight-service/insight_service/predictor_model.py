"""Learned outcome predictor — activates ONLY by beating the baseline.

The frequency baseline (:mod:`predict`) is the permanent floor. This module
trains a logistic regression over per-scenario history features and compares
it against that baseline on a **chronological holdout** (the newest 20% of
examples — no peeking at the future). The model is kept, persisted, and used
(``basis: "model"``) only when its holdout Brier score is strictly better;
otherwise training reports why and the baseline keeps answering. The same
comparison re-runs on every retrain, so a model that decays gets demoted
automatically (ADR-0005 §2.3 / §4).

Training examples come from sliding over each scenario's outcome sequence:
every outcome with at least ``MIN_PRIOR`` prior runs becomes one example
(features = the history before it, label = did it fail).
"""
from __future__ import annotations

import math
import pickle
from datetime import datetime, timezone
from pathlib import Path

from .predict import ALPHA, BETA, DECAY, predict_outcomes

try:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    HAVE_SKLEARN = True
except ImportError:  # pragma: no cover
    HAVE_SKLEARN = False

#: training rows needed before we carve out a calibration split
MIN_FOR_CALIBRATION = 40

#: minimum prior runs before an outcome can be a training example
MIN_PRIOR = 3
#: minimum training examples before fitting is worth attempting
MIN_EXAMPLES = 30
HOLDOUT = 0.2

FEATURE_NAMES = [
    "weighted_fail_rate", "flip_rate", "last_failed", "last2_failed",
    "fail_streak", "duration_drift", "log_history",
    # feature-analysis additions: short-window signal + run cadence
    "recent_fail_rate_5", "log_gap_hours",
]

PREDICTOR_ARTIFACT = "learned-predictor.pkl"


def features(prior: list[dict]) -> list[float]:
    """Feature vector for 'what happens next' given the prior outcomes."""
    statuses = [o["status"] for o in prior]
    n = len(statuses)
    w_fail = w_total = 0.0
    for age, s in enumerate(reversed(statuses)):
        w = DECAY ** age
        w_total += w
        if s == "failed":
            w_fail += w
    weighted = (w_fail + ALPHA) / (w_total + ALPHA + BETA)
    flips = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
    flip_rate = flips / (n - 1) if n > 1 else 0.0
    streak = 0
    for s in reversed(statuses):
        if s == "failed":
            streak += 1
        else:
            break
    durs = [o.get("duration_ms", 0) for o in prior
            if o["status"] == "passed" and o.get("duration_ms")]
    drift = 0.0
    if len(durs) >= 4:
        half = len(durs) // 2
        early = sum(durs[:half]) / half
        late = sum(durs[half:]) / (len(durs) - half)
        if early > 0:
            drift = max(-1.0, min(3.0, (late - early) / early))
    recent5 = statuses[-5:]
    recent_fail_rate = recent5.count("failed") / len(recent5)
    gap_hours = _gap_hours(prior)
    return [
        weighted,
        flip_rate,
        1.0 if statuses[-1] == "failed" else 0.0,
        1.0 if n >= 2 and statuses[-2] == "failed" else 0.0,
        float(min(streak, 10)),
        drift,
        math.log(n + 1),
        recent_fail_rate,
        math.log(1.0 + min(gap_hours, 24 * 30)),
    ]


def _gap_hours(prior: list[dict]) -> float:
    """Hours between the two most recent runs — a stale scenario carries
    more uncertainty than one exercised hourly. 0 when unparseable."""
    from datetime import datetime

    times = []
    for o in prior[-2:]:
        raw = str(o.get("created_at") or "")
        try:
            times.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return 0.0
    if len(times) < 2:
        return 0.0
    return max(0.0, (times[1] - times[0]).total_seconds() / 3600.0)


def build_examples(sequences: dict[str, list[dict]]) -> tuple[list, list, list]:
    """Slide over every scenario's history. Returns (X, y, baseline_p) where
    ``baseline_p`` is the frequency baseline's p_fail for the same moment —
    the honest opponent the model must beat. Chronologically ordered."""
    rows = []  # (created_at, features, label, baseline_p)
    for seq in sequences.values():
        seq = [o for o in seq if o.get("status") in ("passed", "failed")]
        for i in range(MIN_PRIOR, len(seq)):
            prior = seq[:i]
            rows.append((
                seq[i].get("created_at", ""),
                features(prior),
                1.0 if seq[i]["status"] == "failed" else 0.0,
                predict_outcomes(prior)["p_fail_next"],
            ))
    rows.sort(key=lambda r: r[0])
    return ([r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows])


def _brier(probs: list[float], labels: list[float]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(labels)


class LearnedPredictor:
    def __init__(self) -> None:
        self.fitted = False
        self.version = ""
        self.metrics: dict = {}
        self._clf = None
        #: isotonic calibrator fitted on a held-out slice of the TRAINING
        #: data (never the evaluation holdout) — "0.7 means 70%" (ADR §2.3)
        self._iso = None

    def fit(self, sequences: dict[str, list[dict]]) -> dict:
        """Train + gate. Returns the verdict dict either way; ``fitted`` is
        True only when the model beat the baseline on the holdout."""
        self.fitted = False
        if not HAVE_SKLEARN:
            return {"fitted": False, "reason": "scikit-learn not installed"}
        X, y, base_p = build_examples(sequences)
        if len(X) < MIN_EXAMPLES:
            return {"fitted": False,
                    "reason": f"only {len(X)} examples (< {MIN_EXAMPLES})"}
        if len(set(y)) < 2:
            return {"fitted": False, "reason": "single-class history"}
        cut = max(1, int(len(X) * HOLDOUT))
        Xt, yt = X[:-cut], y[:-cut]
        Xh, yh, bh = X[-cut:], y[-cut:], base_p[-cut:]
        if len(set(yt)) < 2:
            return {"fitted": False, "reason": "single-class training split"}

        # Calibration split: carve the newest slice of the TRAINING data for
        # isotonic calibration (the evaluation holdout stays untouched, so
        # the gate judges exactly what will ship — calibrated probabilities).
        iso = None
        cal_cut = (max(5, int(len(Xt) * 0.2))
                   if len(Xt) >= MIN_FOR_CALIBRATION else 0)
        if cal_cut and len(set(yt[:-cal_cut])) == 2 and len(set(yt[-cal_cut:])) == 2:
            fit_X, fit_y = Xt[:-cal_cut], yt[:-cal_cut]
            cal_X, cal_y = Xt[-cal_cut:], yt[-cal_cut:]
        else:
            fit_X, fit_y, cal_X, cal_y = Xt, yt, [], []

        clf = LogisticRegression(max_iter=1000)
        clf.fit(fit_X, fit_y)
        pos = list(clf.classes_).index(1.0)
        if cal_X:
            raw_cal = [float(p) for p in clf.predict_proba(cal_X)[:, pos]]
            iso = IsotonicRegression(y_min=0.0, y_max=1.0,
                                     out_of_bounds="clip").fit(raw_cal, cal_y)

        def _final_p(raw: float) -> float:
            return float(iso.predict([raw])[0]) if iso is not None else raw

        model_p = [_final_p(float(p))
                   for p in clf.predict_proba(Xh)[:, pos]]
        model_brier = round(_brier(model_p, yh), 4)
        base_brier = round(_brier(bh, yh), 4)
        self.metrics = {
            "examples": len(X), "holdout": cut,
            "calibrated": iso is not None,
            "model_brier": model_brier, "baseline_brier": base_brier,
            "beats_baseline": model_brier < base_brier,
        }
        if model_brier >= base_brier:
            # the honesty gate: no better than frequency ⇒ frequency answers
            return {"fitted": False, "reason": "did not beat baseline",
                    **self.metrics}
        self._clf = clf
        self._iso = iso
        self.version = "pred-{}-n{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%d"), len(X))
        self.fitted = True
        return {"fitted": True, **self.metrics}

    def predict(self, prior: list[dict]) -> dict | None:
        """p_fail for the next run given prior outcomes, with the factors that
        drove it (coefficient × feature contributions, largest first)."""
        prior = [o for o in prior if o.get("status") in ("passed", "failed")]
        if not self.fitted or len(prior) < MIN_PRIOR:
            return None
        x = features(prior)
        p = float(self._clf.predict_proba([x])[0][
            list(self._clf.classes_).index(1.0)])
        if self._iso is not None:
            p = float(self._iso.predict([p])[0])
        coefs = self._clf.coef_[0]
        contribs = sorted(
            ((FEATURE_NAMES[i], coefs[i] * x[i]) for i in range(len(x))),
            key=lambda t: abs(t[1]), reverse=True)
        factors = [f"{name} pushing {'up' if c > 0 else 'down'}"
                   for name, c in contribs[:2] if abs(c) > 0.05]
        return {"p_fail_next": round(p, 4), "basis": "model",
                "model_version": self.version, "model_factors": factors}

    # -- artifact persistence ---------------------------------------------

    def save(self, directory: Path | None) -> str | None:
        if directory is None or not self.fitted:
            return None
        path = directory / PREDICTOR_ARTIFACT
        try:
            with open(path, "wb") as fh:
                pickle.dump(self, fh)
            return str(path)
        except OSError:
            return None

    @staticmethod
    def load(directory: Path | None) -> "LearnedPredictor | None":
        if directory is None:
            return None
        path = directory / PREDICTOR_ARTIFACT
        if not path.exists():
            return None
        try:
            with open(path, "rb") as fh:
                obj = pickle.load(fh)
            if not (isinstance(obj, LearnedPredictor) and obj.fitted):
                return None
            # dimension guard: an artifact from an older feature vector must
            # not predict garbage — discard it (next train refits + regates)
            if obj._clf is not None and \
                    obj._clf.coef_.shape[1] != len(FEATURE_NAMES):
                return None
            return obj
        except (OSError, pickle.UnpicklingError, AttributeError, EOFError):
            return None
