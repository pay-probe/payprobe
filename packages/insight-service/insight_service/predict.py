"""Run-outcome / flakiness prediction — the honest frequency baseline.

Recency-weighted Beta-smoothed failure frequency per (scenario, environment),
blended with the flip score the platform already computes descriptively
(``RunStore.flakiness``). Pure functions over outcome lists — no I/O.

``basis`` is always reported (``"frequency"`` here; a future learned model
reports ``"model"`` and must beat this baseline on the predictions_log before
it replaces it — ADR-0005 §4). Below ``MIN_RUNS`` the answer is mostly prior
and says so via ``n_history`` + a factor line.
"""
from __future__ import annotations

PREDICTOR_VERSION = "freq-1"
#: Beta prior — one imaginary pass + one imaginary fail (uniform).
ALPHA = 1.0
BETA = 1.0
#: geometric recency decay per step back in history
DECAY = 0.92
#: below this history size predictions are explicitly low-signal
MIN_RUNS = 5
WINDOW = 50


def predict_outcomes(outcomes: list[dict], scenario_id: str = "",
                     environment: str = "") -> dict:
    """``outcomes``: time-ordered rows with ``status`` ('passed'|'failed')
    and optional ``duration_ms``. Returns the ADR-0005 prediction shape."""
    seq = [o for o in outcomes if o.get("status") in ("passed", "failed")]
    seq = seq[-WINDOW:]
    n = len(seq)

    # Recency-weighted failure mass.
    w_fail = w_total = 0.0
    for age, o in enumerate(reversed(seq)):
        w = DECAY ** age
        w_total += w
        if o["status"] == "failed":
            w_fail += w
    p_fail = (w_fail + ALPHA) / (w_total + ALPHA + BETA)

    # Flakiness: flips between consecutive outcomes, only meaningful when both
    # outcomes occur (same rule as RunStore.flakiness — always-fail is a
    # regression, not flakiness).
    statuses = [o["status"] for o in seq]
    flips = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
    both = 0 < statuses.count("failed") < n
    p_flaky = round(flips / (n - 1), 4) if (n > 1 and both) else 0.0

    factors: list[str] = []
    if n < MIN_RUNS:
        factors.append(f"only {n} runs of history — mostly prior")
    if flips and both:
        factors.append(f"{flips} pass/fail flips in last {n} runs")
    tail = statuses[-3:]
    if tail and all(s == "failed" for s in tail) and n >= 3:
        factors.append("last 3 runs all failed")
    drift = _duration_drift(seq)
    if drift is not None and drift > 0.3:
        factors.append(f"pass duration up {int(drift * 100)}% recently")

    return {
        "scenario_id": scenario_id,
        "environment": environment,
        "p_fail_next": round(p_fail, 4),
        "p_flaky": p_flaky,
        "basis": "frequency",
        "model_version": PREDICTOR_VERSION,
        "n_history": n,
        "top_factors": factors,
    }


def _duration_drift(seq: list[dict]) -> float | None:
    """Relative slowdown of recent passing runs vs earlier ones — slowdown
    often precedes failure. None when there isn't enough signal."""
    durs = [o.get("duration_ms", 0) for o in seq
            if o.get("status") == "passed" and o.get("duration_ms")]
    if len(durs) < 6:
        return None
    half = len(durs) // 2
    early = sum(durs[:half]) / half
    late = sum(durs[half:]) / (len(durs) - half)
    if early <= 0:
        return None
    return (late - early) / early
