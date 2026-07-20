"""Phase-2 insight explanations — the LLM rewrites evidence packs as prose.

ADR-0005 §2.2 boundary, enforced here: the standalone assistant is the ONLY
LLM egress, and the model sees NOTHING the deterministic evidence pack does
not contain. This module fetches categorized failures from the insight
service (``rest.i()``), strips each to its evidence fields, and asks the LLM
to explain — grounded, no invention, no tools. If the LLM is not configured
the caller gets the standard needs-config reply; the deterministic
``explanation`` strings remain available either way.
"""
from __future__ import annotations

import json
from typing import Any

from . import rest

SYSTEM = (
    "You explain payment-test failures to an engineer. You are given "
    "EVIDENCE derived from recorded run history: a failure category, taxonomy "
    "help text, the failure's position in the scenario's history, prior "
    "identical failures and whether the scenario recovered afterwards, and "
    "correlated platform signals. Rules: use ONLY the evidence — never invent "
    "hosts, codes, causes, or numbers that are not in it; if evidence is "
    "thin, say so; be concrete and brief (2-4 sentences per failure); end "
    "each with the single most useful next action. These are advisory "
    "observations — never claim a verdict about release readiness."
)


def evidence_view(failure: dict) -> dict:
    """The exact subset of a categorized failure the LLM is allowed to see."""
    ev = failure.get("evidence") or {}
    return {
        "scenario": failure.get("scenario_name"),
        "step": failure.get("step_id"),
        "target": failure.get("target"),
        "category": failure.get("label"),
        "novel_shape": failure.get("novel"),
        "taxonomy_help": ev.get("help"),
        "history": (ev.get("history") or {}).get("text"),
        "similar_failures": len(ev.get("similar_failures") or []),
        "recovered_after_similar": ev.get("recovered_after_similar"),
        "signals": ev.get("signals") or {},
    }


def fetch_failures(run_id: str) -> list[dict]:
    """Categorized failures for one run, from the insight service."""
    import urllib.parse
    body = rest.request(
        "GET", rest.i(f"/insights/failures/{urllib.parse.quote(run_id, safe='')}"))
    return (body or {}).get("failures") or []


def completion(system: str, user: str, llm: dict) -> str:
    """One-shot, tool-free completion against the configured provider."""
    provider = (llm.get("provider") or "openai").lower()
    if provider == "anthropic":
        data = rest.post_json(
            llm["base_url"],
            {"x-api-key": llm["api_key"], "anthropic-version": "2023-06-01"},
            {"model": llm["model"], "max_tokens": 1200, "system": system,
             "messages": [{"role": "user", "content": user}]})
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts
                       if p.get("type") == "text").strip()
    data = rest.post_json(
        llm["base_url"], {"Authorization": f"Bearer {llm['api_key']}"},
        {"model": llm["model"], "temperature": 0.2,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]})
    choices = data.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    return (msg.get("content") or "").strip()


def explain_run(run_id: str, llm: dict) -> dict[str, Any]:
    """Prose explanations for every categorized failure of one run."""
    failures = fetch_failures(run_id)
    if not failures:
        return {"run_id": run_id, "explanations": [],
                "note": "no categorized failures for this run"}
    views = [evidence_view(f) for f in failures]
    user = (
        "Explain each failure below. Answer as a JSON array of strings, one "
        "explanation per failure, same order.\n\nEVIDENCE:\n"
        + json.dumps(views, indent=2)
    )
    text = completion(SYSTEM, user, llm)
    explanations = _parse_explanations(text, len(views))
    return {
        "run_id": run_id,
        "advisory_only": True,
        "explanations": [
            {"scenario_id": f.get("scenario_id"), "step_id": f.get("step_id"),
             "text": e}
            for f, e in zip(failures, explanations)
        ],
    }


def _parse_explanations(text: str, expected: int) -> list[str]:
    """The model was asked for a JSON array; tolerate fences and prose-only
    replies (then every failure shares the one blob rather than losing it)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list) and all(isinstance(x, str) for x in arr):
            arr = [x.strip() for x in arr]
            if len(arr) < expected:
                arr += [""] * (expected - len(arr))
            return arr[:expected]
    except ValueError:
        pass
    return [text.strip()] * expected
