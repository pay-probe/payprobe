"""Deterministic post-checks on a heartbeat's answer (ADR-0010 phase 5).

A model's verdict is advice; where the platform holds the evidence, the
evidence wins. A post-check runs after the tool loop has finished, under the
same on-behalf-of backend the heartbeat used, and may correct one field of the
answer while keeping the model's own claim beside it, so a reader sees both
what the agent said and what the platform knows.

One check today, ``regression``: an answer that names a ``run_id`` and carries
a ``regression`` field is compared with the orchestrator's run history
(``GET /runs/{id}/regression``, reached as the ``get_run_regression`` toolkit
primitive). The field is overwritten with the deterministic verdict and a
``regression_evidence`` block is added. Real case, 2026-09-23: failure-triage
judged the very first run of a scenario a regression because "similar failures"
existed; the history said ``first_run``.

The check is pure with respect to the store: :func:`apply` returns the new
answer text and a step record the API persists on the heartbeat. It never
raises; a backend failure becomes a step with ``ok: false`` and the answer is
left as the model wrote it.
"""

from __future__ import annotations

import json
from typing import Any

from .alerts import extract_json

#: kind of the step record a post-check appends to the heartbeat
STEP_KIND = "postcheck"

#: evidence fields copied per failed scenario (kept short: this lands on the
#: run report and in the alert payload)
_SCENARIO_FIELDS = (
    "name", "scenario_id", "status", "conclusion", "prior_runs", "prior_passed",
    "last_passed_run_id", "failure_streak",
)


def regression(result: str | None, backend: Any) -> tuple[str | None, dict | None]:
    """Check a triage-style answer's ``regression`` claim against run history.

    Returns ``(result, None)`` when the answer is not a JSON object with both
    ``run_id`` and ``regression`` (nothing to check). Otherwise returns the
    re-serialised answer (the JSON object, indented) and the step record.
    """
    data = extract_json(result)
    if not isinstance(data, dict) or "regression" not in data:
        return result, None
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return result, None
    step: dict = {"kind": STEP_KIND, "tool": "get_run_regression", "run_id": run_id}
    claimed = data.get("regression")
    try:
        ev = backend.get_run_regression(run_id)
    except Exception as exc:  # noqa: BLE001 — evidence unavailable: keep the model's claim
        return result, {**step, "ok": False, "changed": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(ev, dict) or "verdict" not in ev:
        return result, {**step, "ok": False, "changed": False, "error": f"run '{run_id}' not found"}

    verified = bool(ev.get("regression"))
    data["regression"] = verified
    data["regression_evidence"] = {
        "verdict": ev.get("verdict"),
        "claimed": claimed,
        "regressed": list(ev.get("regressed") or []),
        "never_passed": list(ev.get("never_passed") or []),
        "scenarios": [
            {k: s.get(k) for k in _SCENARIO_FIELDS}
            for s in ev.get("scenarios") or []
            if isinstance(s, dict) and s.get("status") == "failed"
        ],
    }
    return json.dumps(data, indent=2, default=str), {
        **step,
        "ok": True,
        "claimed": claimed,
        "verified": verified,
        "verdict": ev.get("verdict"),
        "changed": claimed is not verified,
        "error": None,
    }


def apply(result: str | None, backend: Any) -> tuple[str | None, list[dict]]:
    """Run every post-check; returns the (possibly corrected) answer and the
    step records to append, in order."""
    steps: list[dict] = []
    result, step = regression(result, backend)
    if step is not None:
        steps.append(step)
    return result, steps
