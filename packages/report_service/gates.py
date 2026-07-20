"""Go/No-Go gates — turn a finished run into a defensible verdict.

Where :mod:`report_service.diagnose` answers *why* a run failed (improvement
mode), this module answers *may we ship it* (sign-off mode). It evaluates a run
detail against an explicit, configurable :class:`GatePolicy` and returns a
``GateResult`` dict with a single ``GO``/``NO_GO`` verdict plus the per-gate
reasons that produced it.

Pure and dependency-free: dicts in, a dict out. The orchestrator wraps it in the
certify endpoint; the portal renders the verdict. See ADR-0003.

Gates, in evaluation order:

* **tiers**            — per-priority minimum pass-% (e.g. P0 must be 100%).
* **coverage**         — every case in the pack actually ran (pack mode only).
* **no_silent_skips**  — no ``not_run`` / ``blocked`` / ``pending`` outcomes; a
  green run that quietly skipped cases is *not* a GO.
* **regressions**      — no more than ``max_regressions`` steps regressed vs the
  baseline (the last GO run), when a baseline is supplied.
* **environment**      — the run hit an approved environment (a sim-only run is
  not a prod-readiness signal).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .generators import index_steps, score_cases

# Outcomes that mean "no evidence was produced for this case/step".
SKIP_STATUSES = frozenset({"not_run", "blocked", "pending"})

# Tier key that matches every case regardless of its declared priority — useful
# when scoring without a pack (priorities aren't known).
TIER_ALL = "all"


@dataclass(frozen=True)
class GatePolicy:
    """The rules that turn a run into GO / NO-GO.

    ``tiers`` maps a priority label to the minimum pass-percentage required for
    cases of that priority (e.g. ``{"P0": 100, "P1": 95}``). The special key
    ``"all"`` matches every case and is the sensible default when no pack (and
    therefore no per-case priority) is available.
    """

    tiers: dict[str, float] = field(default_factory=lambda: {TIER_ALL: 100.0})
    require_coverage: bool = True
    forbid_not_run: bool = True
    max_regressions: int = 0
    allowed_environments: tuple[str, ...] | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "GatePolicy":
        """Build a policy from a (partial) config dict, applying defaults."""
        if not data:
            return cls()
        base = cls()
        env = data.get("allowed_environments")
        return replace(
            base,
            tiers={str(k): float(v) for k, v in (data.get("tiers") or base.tiers).items()},
            require_coverage=bool(data.get("require_coverage", base.require_coverage)),
            forbid_not_run=bool(data.get("forbid_not_run", base.forbid_not_run)),
            max_regressions=int(data.get("max_regressions", base.max_regressions)),
            allowed_environments=tuple(env) if env is not None else base.allowed_environments,
        )


def _gate(gid: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"id": gid, "ok": ok, "detail": detail}


def _cases(detail: dict, pack: dict | None) -> list[dict]:
    """Case list to score: pack cases if given, else one per scenario."""
    if pack:
        return score_cases(detail, pack)
    out: list[dict] = []
    for sc in (detail.get("summary") or {}).get("scenarios", []) or []:
        status = sc.get("status", "not_run")
        out.append({
            "id": sc.get("scenario_id") or sc.get("name"),
            "scenario": sc.get("name"), "status": status,
            "passed": status == "passed", "priority": sc.get("priority"),
        })
    return out


def _all_statuses(detail: dict) -> list[str]:
    """Every scenario- and step-level status in the run."""
    out: list[str] = []
    for sc in (detail.get("summary") or {}).get("scenarios", []) or []:
        if sc.get("status"):
            out.append(sc["status"])
        for st in sc.get("steps", []) or []:
            if st.get("status"):
                out.append(st["status"])
    return out


def regressions(detail: dict, baseline: dict) -> tuple[list, list]:
    """(regressed, fixed) steps between a run and its baseline.

    A step *regressed* if it passed in the baseline and no longer passes now; it
    is *fixed* if the reverse. Steps absent from the current run are ignored
    (they can't be a regression in *this* run).
    """
    cur = index_steps(detail.get("summary"))
    base = index_steps(baseline.get("summary"))
    regressed, fixed = [], []
    for key, bstat in base.items():
        cstat = cur.get(key)
        if cstat is None:
            continue
        if bstat == "passed" and cstat != "passed":
            regressed.append(key)
        elif bstat != "passed" and cstat == "passed":
            fixed.append(key)
    return regressed, fixed


def evaluate_gates(
    detail: dict,
    *,
    policy: GatePolicy | dict | None = None,
    pack: dict | None = None,
    baseline: dict | None = None,
) -> dict:
    """Evaluate a run against a policy -> GateResult dict.

    Returns ``{verdict, gates, blocking}`` where ``verdict`` is ``"GO"`` only
    when every applicable gate passed and ``blocking`` lists the ids of the
    gates that failed.
    """
    pol = policy if isinstance(policy, GatePolicy) else GatePolicy.from_dict(policy)
    cases = _cases(detail, pack)
    gates: list[dict] = []

    # -- tiers: per-priority pass-% threshold --------------------------------
    for label, threshold in pol.tiers.items():
        relevant = (
            cases if label == TIER_ALL
            else [c for c in cases if c.get("priority") == label]
        )
        if not relevant:
            gates.append(_gate(f"tier:{label}", True, f"no {label} cases defined"))
            continue
        passed = sum(1 for c in relevant if c["passed"])
        pct = passed / len(relevant) * 100
        gates.append(_gate(
            f"tier:{label}", pct >= threshold,
            f"{passed}/{len(relevant)} {label} passed ({pct:.0f}%), "
            f"need {threshold:.0f}%",
        ))

    # -- coverage: every pack case ran (pack mode only) ----------------------
    if pol.require_coverage and pack is not None:
        missing = [c["id"] for c in cases if c["status"] == "not_run"]
        gates.append(_gate(
            "coverage", not missing,
            "all pack cases ran" if not missing
            else f"{len(missing)} case(s) never ran: {', '.join(map(str, missing))}",
        ))

    # -- no silent skips -----------------------------------------------------
    if pol.forbid_not_run:
        skips = [s for s in _all_statuses(detail) if s in SKIP_STATUSES]
        gates.append(_gate(
            "no_silent_skips", not skips,
            "no skipped outcomes" if not skips
            else f"{len(skips)} skipped/blocked outcome(s) — absence of evidence",
        ))

    # -- regressions vs baseline (last GO run) -------------------------------
    if baseline is not None:
        regressed, _ = regressions(detail, baseline)
        gates.append(_gate(
            "regressions", len(regressed) <= pol.max_regressions,
            f"{len(regressed)} regression(s) vs baseline, "
            f"allow {pol.max_regressions}",
        ))

    # -- environment ---------------------------------------------------------
    if pol.allowed_environments is not None:
        env = detail.get("environment")
        gates.append(_gate(
            "environment", env in pol.allowed_environments,
            f"ran against {env or '—'}; "
            f"allowed: {', '.join(pol.allowed_environments) or '—'}",
        ))

    blocking = [g["id"] for g in gates if not g["ok"]]
    return {
        "verdict": "GO" if not blocking else "NO_GO",
        "gates": gates,
        "blocking": blocking,
    }
