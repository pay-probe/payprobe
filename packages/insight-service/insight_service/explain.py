"""Failure-reason explanation — deterministic evidence packs.

For a failed step, assemble everything the data can honestly say about *why*:
the category (+ the shared taxonomy's help text), the failure's position in
the scenario's history, prior failures with the same normalized signature
(and whether the scenario later recovered), and any correlated platform
signals the caller passes in (active chaos storm, saturation, …).

The pack is pure data; :func:`render_text` turns it into prose. A future
Phase-2 LLM rewrite (via the standalone assistant) consumes the SAME pack —
the LLM never sees anything the pack doesn't contain (ADR-0005 §2.2).
"""
from __future__ import annotations

from .categorize import category_help
from .normalize import signature


def evidence_pack(
    failure: dict,
    history: list[dict],
    similar: list[dict],
    signals: dict | None = None,
) -> dict:
    """``failure``: a categorized failure row (store shape + category fields).
    ``history``: time-ordered outcome rows for the same scenario/environment.
    ``similar``: prior failures sharing this failure's signature.
    ``signals``: optional correlated platform facts, passed through verbatim.
    """
    statuses = [h["status"] for h in history
                if h.get("status") in ("passed", "failed")]
    position = _history_position(statuses)
    recovered = _recovered_after_similar(similar, history)

    return {
        "category": failure.get("category", "unknown"),
        "label": failure.get("label") or failure.get("category", "unknown"),
        "heuristic": failure.get("heuristic", "unknown"),
        "help": category_help(failure.get("heuristic", "unknown")),
        "history": position,
        "similar_failures": [
            {"run_id": s["run_id"], "scenario": s.get("scenario_name", ""),
             "when": s.get("created_at", "")} for s in similar
        ],
        "recovered_after_similar": recovered,
        "signals": signals or {},
        "signature": failure.get("signature")
                     or signature(failure.get("norm") or failure.get("error")),
    }


def _history_position(statuses: list[str]) -> dict:
    n = len(statuses)
    if n == 0:
        return {"kind": "no_history",
                "text": "first recorded run of this scenario"}
    fails = statuses.count("failed")
    if statuses and all(s == "failed" for s in statuses):
        return {"kind": "always_failing", "runs": n,
                "text": f"fails every recorded run ({n}) — a standing "
                        "regression, not a blip"}
    # streak of passes immediately before the latest failure
    streak = 0
    for s in reversed(statuses[:-1] if statuses[-1] == "failed" else statuses):
        if s == "passed":
            streak += 1
        else:
            break
    if statuses[-1] == "failed" and streak > 0:
        return {"kind": "fresh_break", "passes_before": streak, "runs": n,
                "text": f"first failure after {streak} consecutive passes — "
                        "look at what changed since the last green run"}
    return {"kind": "intermittent", "runs": n, "failed": fails,
            "text": f"{fails} of the last {n} runs failed — intermittent; "
                    "see flakiness"}


def _recovered_after_similar(similar: list[dict], history: list[dict]) -> bool:
    """True if, after a prior identical-signature failure, a later run of the
    scenario passed — evidence the failure shape is fixable, not permanent."""
    if not similar:
        return False
    passes = {h.get("run_id") for h in history if h.get("status") == "passed"}
    if not passes:
        return False
    by_time = {h.get("run_id"): h.get("created_at", "") for h in history}
    for s in similar:
        after = s.get("created_at", "")
        if any(by_time.get(rid, "") > after for rid in passes):
            return True
    return False


def render_text(pack: dict) -> str:
    """The evidence pack as plain prose (portal 'why did this fail?' panel)."""
    lines = [
        f"Category: {pack['label']} ({pack['heuristic']}).",
        pack["help"]["what"],
        pack["history"]["text"].capitalize() + ".",
    ]
    sim = pack["similar_failures"]
    if sim:
        lines.append(
            f"{len(sim)} earlier run(s) failed with this exact shape"
            + (", and the scenario later passed — the failure is fixable; "
               "compare configs with the recovering run"
               if pack["recovered_after_similar"] else "."))
    for key, val in (pack.get("signals") or {}).items():
        if val:
            lines.append(f"Correlated signal: {key} = {val}.")
    lines.append("Suggested fix: " + pack["help"]["fix"])
    return " ".join(lines)
