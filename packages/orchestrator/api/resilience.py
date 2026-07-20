"""Resilience scoring — turn chaos injection into a pass/fail certificate.

The chaos dial (see api/main.py `set_simulator_chaos` / `start_chaos_storm`)
can make a simulator misbehave. On its own that only *proves faults happened*.
This module answers the question a payment integrator actually cares about:

    "When the switch went slow / dropped replies / hung up mid-frame, did MY
     client survive — did its retries, timeouts and reconnects keep the
     end-to-end success rate up, and did it recover when the storm passed?"

A **resilience run** drives live traffic through the topology (an ordinary
load run) while a chaos storm plays on a target simulator, sampling both sides
each second across three stages:

    baseline  — calm, establishes the client's normal success rate + p95
    storm     — chaos injected; the client is under fire
    recovery  — chaos lifted; does the client return to baseline?

The pure scorer here consumes those samples and emits component scores, hard
gates, an overall 0–100 score, a letter grade and a PASS/FAIL verdict — the
resilience analog of the certification report. Everything in this module is a
pure function over sample dicts, so it is fully unit-testable without a wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# stage labels a sample can carry
STAGE_BASELINE = "baseline"
STAGE_STORM = "storm"
STAGE_RECOVERY = "recovery"

# component weights (sum to 1.0)
WEIGHTS = {
    "availability": 0.35,
    "absorption": 0.30,
    "recovery": 0.25,
    "latency": 0.10,
}


@dataclass
class ResilienceThresholds:
    """Tunable gates + the pass bar for the overall score."""

    #: storm-stage client success rate must stay at least this fraction of the
    #: baseline success rate (0.90 = "lose at most 10% of your success rate").
    min_availability_ratio: float = 0.90
    #: after the storm lifts, recovery-stage success must reach this fraction of
    #: baseline within the recovery window.
    min_recovery_ratio: float = 0.98
    #: storm p95 latency may not exceed baseline p95 × this multiple.
    max_latency_multiple: float = 4.0
    #: overall score (0–100) at or above this is a PASS.
    pass_score: float = 70.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "ResilienceThresholds":
        d = d or {}
        base = cls()
        return cls(
            min_availability_ratio=float(d.get("min_availability_ratio", base.min_availability_ratio)),
            min_recovery_ratio=float(d.get("min_recovery_ratio", base.min_recovery_ratio)),
            max_latency_multiple=float(d.get("max_latency_multiple", base.max_latency_multiple)),
            pass_score=float(d.get("pass_score", base.pass_score)),
        )


@dataclass
class StageAgg:
    """Deltas accumulated for one stage from its first→last cumulative sample."""

    label: str
    client_received: int = 0
    client_errors: int = 0
    sim_faults: int = 0
    sim_received: int = 0
    p95_max: float = 0.0
    p95_samples: list[float] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def client_total(self) -> int:
        return self.client_received + self.client_errors

    @property
    def success_rate(self) -> float:
        return self.client_received / self.client_total if self.client_total else 0.0

    @property
    def error_rate(self) -> float:
        return self.client_errors / self.client_total if self.client_total else 0.0

    @property
    def fault_rate(self) -> float:
        """Fraction of simulator replies in which a fault was injected."""
        return self.sim_faults / self.sim_received if self.sim_received else 0.0

    @property
    def p95(self) -> float:
        if self.p95_samples:
            return round(sum(self.p95_samples) / len(self.p95_samples), 2)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "seconds": round(self.seconds, 1),
            "client_received": self.client_received,
            "client_errors": self.client_errors,
            "success_rate": round(self.success_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "sim_received": self.sim_received,
            "sim_faults": self.sim_faults,
            "fault_rate": round(self.fault_rate, 4),
            "p95_ms": self.p95,
            "p95_max_ms": round(self.p95_max, 2),
        }


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def aggregate_stages(samples: list[dict]) -> dict[str, StageAgg]:
    """Fold per-second cumulative samples into one delta per stage.

    Each sample is a dict with a ``stage`` label and cumulative counters
    (``client_received``, ``client_errors``, ``sim_faults``, ``sim_received``,
    ``p95_ms``, ``t``). Counters are cumulative for the whole run, so a stage's
    delta is (last − first) sample within that stage.
    """
    by_stage: dict[str, list[dict]] = {}
    for s in samples:
        by_stage.setdefault(s.get("stage", STAGE_BASELINE), []).append(s)

    out: dict[str, StageAgg] = {}
    for label, rows in by_stage.items():
        rows = sorted(rows, key=lambda r: r.get("t", 0.0))
        agg = StageAgg(label=label)
        first, last = rows[0], rows[-1]
        agg.client_received = max(0, int(last.get("client_received", 0)) - int(first.get("client_received", 0)))
        agg.client_errors = max(0, int(last.get("client_errors", 0)) - int(first.get("client_errors", 0)))
        agg.sim_faults = max(0, int(last.get("sim_faults", 0)) - int(first.get("sim_faults", 0)))
        agg.sim_received = max(0, int(last.get("sim_received", 0)) - int(first.get("sim_received", 0)))
        agg.seconds = float(last.get("t", 0.0)) - float(first.get("t", 0.0))
        agg.p95_samples = [float(r.get("p95_ms", 0.0) or 0.0) for r in rows if r.get("p95_ms")]
        agg.p95_max = max((float(r.get("p95_ms", 0.0) or 0.0) for r in rows), default=0.0)
        out[label] = agg
    return out


def _grade(score: float) -> str:
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 55: return "D"
    return "F"


def score_resilience(samples: list[dict], thresholds: dict | None = None) -> dict:
    """Score a resilience run from its per-second samples.

    Returns a report dict: per-stage aggregates, four component scores
    (availability / fault-absorption / recovery / latency), hard gates, an
    overall 0–100 score, a letter grade and a PASS/FAIL verdict.
    """
    th = ResilienceThresholds.from_dict(thresholds)
    stages = aggregate_stages(samples)
    base = stages.get(STAGE_BASELINE)
    storm = stages.get(STAGE_STORM)
    recov = stages.get(STAGE_RECOVERY)

    base_success = base.success_rate if base else 1.0
    base_p95 = base.p95 if base and base.p95 else 0.0

    components: dict[str, float] = {}
    gates: list[dict] = []
    notes: list[str] = []

    # -- 1. availability: storm success rate held vs baseline ----------------
    if storm and storm.client_total:
        ratio = (storm.success_rate / base_success) if base_success else 0.0
        components["availability"] = round(_clamp01(ratio) * 100, 1)
        gates.append({
            "id": "availability",
            "label": "Success rate held under chaos",
            "value": round(storm.success_rate, 4),
            "threshold": round(base_success * th.min_availability_ratio, 4),
            "passed": storm.success_rate >= base_success * th.min_availability_ratio,
        })
    else:
        components["availability"] = 0.0
        notes.append("no client traffic recorded during the storm stage")

    # -- 2. fault absorption: injected faults that didn't reach the client ----
    if storm and storm.fault_rate > 0:
        # of the fault rate the sim threw, how much did the client's error rate
        # stay below it → recovered via retry/reconnect.
        absorbed = 1.0 - (storm.error_rate / storm.fault_rate)
        components["absorption"] = round(_clamp01(absorbed) * 100, 1)
        gates.append({
            "id": "no_wedge",
            "label": "Client did not wedge under faults",
            "value": round(storm.success_rate, 4),
            "threshold": 0.0,
            # if the sim still answered *some* requests cleanly but the client
            # got nothing through, the client wedged.
            "passed": not (storm.fault_rate < 1.0 and storm.success_rate <= 0.0),
        })
    elif storm and storm.client_total:
        # storm stage ran but no faults were actually injected — absorption is
        # not measurable; don't punish or reward it.
        components["absorption"] = components["availability"]
        notes.append("no faults were injected during the storm stage")
    else:
        components["absorption"] = 0.0

    # -- 3. recovery: success returned to baseline after the storm lifted ----
    if recov and recov.client_total:
        rratio = (recov.success_rate / base_success) if base_success else 0.0
        components["recovery"] = round(_clamp01(rratio) * 100, 1)
        gates.append({
            "id": "recovery",
            "label": "Recovered to baseline after chaos",
            "value": round(recov.success_rate, 4),
            "threshold": round(base_success * th.min_recovery_ratio, 4),
            "passed": recov.success_rate >= base_success * th.min_recovery_ratio,
        })
    else:
        # no recovery stage requested → treat as neutral (mirror availability)
        components["recovery"] = components["availability"]
        notes.append("no recovery stage sampled")

    # -- 4. latency resilience: storm p95 vs a baseline-relative ceiling -----
    if storm and storm.p95 > 0 and base_p95 > 0:
        ceiling = base_p95 * th.max_latency_multiple
        # full marks at baseline p95, zero at the ceiling.
        span = max(1e-6, ceiling - base_p95)
        lat = 1.0 - (storm.p95 - base_p95) / span
        components["latency"] = round(_clamp01(lat) * 100, 1)
        gates.append({
            "id": "latency",
            "label": "p95 latency under ceiling during chaos",
            "value": storm.p95,
            "threshold": round(ceiling, 2),
            "passed": storm.p95 <= ceiling,
        })
    else:
        components["latency"] = 100.0 if not notes else components.get("availability", 0.0)

    # -- overall weighted score ----------------------------------------------
    score = round(sum(components[k] * w for k, w in WEIGHTS.items()), 1)
    blocking = [g["id"] for g in gates if not g["passed"]]
    verdict = "PASS" if (score >= th.pass_score and not blocking) else "FAIL"

    return {
        "score": score,
        "grade": _grade(score),
        "verdict": verdict,
        "components": components,
        "weights": WEIGHTS,
        "gates": gates,
        "blocking": blocking,
        "stages": {k: v.to_dict() for k, v in stages.items()},
        "baseline_success_rate": round(base_success, 4),
        "baseline_p95_ms": base_p95,
        "thresholds": {
            "min_availability_ratio": th.min_availability_ratio,
            "min_recovery_ratio": th.min_recovery_ratio,
            "max_latency_multiple": th.max_latency_multiple,
            "pass_score": th.pass_score,
        },
        "notes": notes,
    }
