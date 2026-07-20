"""LLM label suggestions for the active-learning queue — human approves.

Same ADR-0005 boundary as :mod:`explain`: the standalone assistant is the
ONLY LLM egress, and the model sees nothing beyond what the insight service
already exposes (normalized failure-shape texts + the existing taxonomy).
The output is a SUGGESTION per shape — nothing is written anywhere until a
human clicks teach in the portal, so ground truth stays human-made.
"""
from __future__ import annotations

import json
from typing import Any

from . import rest
from .explain import completion

SYSTEM = (
    "You label payment-test failure shapes for an engineer. You are given "
    "failure texts (already normalized: digits/hosts/ids masked to <n>) and "
    "the EXISTING label taxonomy. For each shape propose ONE label: reuse an "
    "existing label when it truly fits, otherwise coin a new short kebab-case "
    "one (e.g. unresolved-reference, assertion-mismatch). Never invent causes "
    "not visible in the text. Answer as a JSON array, same order, of objects "
    '{"label": "...", "reason": "one short sentence"}.'
)


def fetch_queue(limit: int) -> list[dict]:
    body = rest.request("GET", rest.i(f"/insights/label-queue?limit={limit}"))
    return (body or {}).get("queue") or []


def fetch_taxonomy() -> list[str]:
    """Every label the platform already knows: dataset labels + categories."""
    labels: set[str] = set()
    try:
        for d in (rest.request("GET", rest.i("/datasets")) or {}).get(
                "datasets") or []:
            labels.update((d.get("labels") or {}).keys())
    except RuntimeError:
        pass
    try:
        for c in (rest.request("GET", rest.i("/insights/categories")) or {}).get(
                "categories") or []:
            if c.get("label"):
                labels.add(c["label"])
    except RuntimeError:
        pass
    return sorted(labels)


def suggest_labels(limit: int, llm: dict) -> dict[str, Any]:
    """One suggestion per label-queue shape; advisory, human-approved."""
    queue = fetch_queue(limit)
    if not queue:
        return {"suggestions": [], "advisory_only": True,
                "note": "label queue is empty"}
    taxonomy = fetch_taxonomy()
    user = (
        "EXISTING LABELS:\n" + json.dumps(taxonomy)
        + "\n\nFAILURE SHAPES (suggest one label each, same order):\n"
        + json.dumps([{"text": e.get("sample_text", ""),
                       "model_says": (e.get("current") or {}).get("label"),
                       "instances": e.get("count")} for e in queue], indent=2)
    )
    text = completion(SYSTEM, user, llm)
    parsed = _parse_suggestions(text, len(queue))
    return {
        "advisory_only": True,
        "suggestions": [
            {"signature": e.get("signature"), "sample_text": e.get("sample_text"),
             "label": s.get("label", ""), "reason": s.get("reason", ""),
             "is_new": s.get("label", "") not in taxonomy}
            for e, s in zip(queue, parsed)
        ],
    }


def _parse_suggestions(text: str, expected: int) -> list[dict]:
    """The model was asked for a JSON array of {label, reason}; tolerate
    fences, bare-string arrays, and short arrays (missing → empty)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    out: list[dict] = []
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            for x in arr:
                if isinstance(x, dict) and isinstance(x.get("label"), str):
                    out.append({"label": x["label"].strip(),
                                "reason": str(x.get("reason", "")).strip()})
                elif isinstance(x, str):
                    out.append({"label": x.strip(), "reason": ""})
    except ValueError:
        pass
    while len(out) < expected:
        out.append({"label": "", "reason": ""})
    return out[:expected]
