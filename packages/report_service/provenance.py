"""Provenance — prove *what* was tested against *what*, tamper-evidently.

A sign-off report is the fact that gives the green light to production, so it
must record the full context of the run and be tamper-evident. :func:`provenance`
builds that stamp from a run ``detail`` plus a ``ctx`` of things the run detail
can't know on its own (the system-under-test build, who triggered it, the
resolved endpoint targets, the baseline run). :func:`content_hash` then produces
a stable SHA-256 over the frozen result + gate verdict + provenance, so any later
edit to the artifact is detectable.

Pure and dependency-free (``hashlib``/``json`` only). See ADR-0003.

Security note: endpoint hosts and any secret-bearing fields must be masked by the
caller before this stamp is persisted — provenance records *targets*, never
credentials. The hash is computed over the masked, canonical form.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _scenario_versions(detail: dict) -> dict[str, Any]:
    """{scenario_name: version} for every scenario in the run, when present."""
    out: dict[str, Any] = {}
    for sc in (detail.get("summary") or {}).get("scenarios", []) or []:
        name = sc.get("name") or sc.get("scenario_id")
        if name is None:
            continue
        out[str(name)] = sc.get("version")
    return out


def provenance(detail: dict, ctx: dict | None = None) -> dict:
    """Build the provenance stamp for a run.

    ``ctx`` supplies what the run detail can't: ``system_under_test`` (build +
    source), ``triggered_by``, ``endpoints`` (runtime-resolved host/port per
    target — already masked by the caller), ``baseline_run_id``, the
    ``pack`` {id, version}, and ``playground_traffic`` (ADR-0007: ad-hoc
    Playground fires that hit a live simulator inside this run's window, which
    inflate its stats/traces — recorded so a sign-off can note contamination
    rather than hide it). Anything absent is recorded as ``None``/empty rather
    than omitted, so the shape (and therefore the hash) is stable.
    """
    ctx = ctx or {}
    return {
        "run_id": detail.get("id"),
        "scenario_versions": _scenario_versions(detail),
        "pack": ctx.get("pack"),
        "environment": detail.get("environment"),
        "endpoints": ctx.get("endpoints") or [],
        "system_under_test": ctx.get("system_under_test"),
        "triggered_by": ctx.get("triggered_by"),
        "started_at": detail.get("started_at"),
        "completed_at": detail.get("completed_at"),
        "baseline_run_id": ctx.get("baseline_run_id"),
        "playground_traffic": ctx.get("playground_traffic")
        or {"total": 0, "by_simulator": {}, "window": None},
    }


def _canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, stable across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(*, summary: dict, gate_result: dict, prov: dict) -> str:
    """Stable ``sha256:…`` over the frozen evidence.

    Combines the run summary (what happened), the gate result (the verdict and
    why), and the provenance (the context). Any later mutation of any of the
    three changes the hash, making the sign-off artifact tamper-evident.
    """
    payload = _canonical({"summary": summary, "gate_result": gate_result,
                          "provenance": prov})
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
