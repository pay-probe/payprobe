"""Diagnose-my-run — turn a finished run into a likely cause + suggested fix.

This is the codified version of the manual triage we keep doing ("0 TPS? every
transaction is failing because the target refused the connection"). It reads a
run's outcome — a functional run detail (scenarios → steps) or a load-run
summary (sent/received/errors/last_error/workers) — classifies the failure
against a small taxonomy, and returns ranked findings, each with a plain-English
cause, a concrete suggestion, and the evidence it keyed off.

Pure and dependency-free: input dicts in, findings out. The orchestrator wraps
it in an endpoint; the portal renders the findings.
"""
from __future__ import annotations

import re
from typing import Any

# severity ordering for ranking
_SEV = {"error": 0, "warn": 1, "info": 2}


def _finding(code: str, severity: str, title: str, cause: str,
             suggestion: str, evidence: str = "") -> dict[str, Any]:
    return {
        "code": code, "severity": severity, "title": title,
        "cause": cause, "suggestion": suggestion, "evidence": evidence,
    }


# -- error-string taxonomy ---------------------------------------------------

def classify_error(msg: str | None) -> str:
    """Map a raw error/assertion message to a taxonomy category."""
    if not msg:
        return "unknown"
    m = msg.lower()
    if re.search(r"refused|econnrefused|connection error|cannot connect|"
                 r"unreachable|name or service not known|no route to host|"
                 r"failed to connect|connection reset", m):
        return "unreachable"
    if re.search(r"timed out|timeout|deadline exceeded|read timed out", m):
        return "timeout"
    if re.search(r"\b401\b|\b403\b|unauthor|forbidden|invalid token|"
                 r"permission denied|authentication", m):
        return "auth"
    if re.search(r"assertion|expected .* got|response_code|did not match", m):
        return "assertion"
    if re.search(r"tls|ssl|certificate|handshake", m):
        return "tls"
    if re.search(r"no connection|not bound|unknown target|no adapter|"
                 r"not configured", m):
        return "binding"
    if re.search(r"unresolved reference|unknown variable|undefined variable", m):
        return "reference"
    if re.search(r"unsupportedprotocol|missing an 'http|no usable base_url|"
                 r"invalid url|unsupported protocol", m):
        return "url"
    return "error"


_CATEGORY_HELP = {
    "unreachable": (
        "The target system refused or could not accept the connection.",
        "Check the target is running and the Connection's host/port are correct "
        "and reachable from the worker (firewall/DNS). Test the Connection from "
        "the Connections page.",
    ),
    "timeout": (
        "The target accepted the connection but didn't respond in time.",
        "Raise the step/connection timeout, or check the target is healthy and "
        "not overloaded; under load this often means the target is the bottleneck.",
    ),
    "auth": (
        "The target rejected the request as unauthorized.",
        "Check the Connection's credentials/keys and that any required sign-on "
        "succeeded.",
    ),
    "assertion": (
        "The target responded, but the response didn't match the scenario's "
        "assertions.",
        "Verify the expected values (e.g. response_code) match what this target "
        "actually returns, or fix the upstream data so it approves.",
    ),
    "tls": (
        "A TLS/SSL handshake problem prevented the request.",
        "Check the Connection's TLS settings and certificates/trust chain.",
    ),
    "binding": (
        "The scenario's target isn't bound to a real endpoint in this "
        "environment.",
        "Attach a Connection for the target, or pick an environment that defines "
        "it (mock for a dry run).",
    ),
    "url": (
        "The request URL is malformed — the connection's base_url is empty or "
        "missing its http(s):// scheme, so the adapter had nothing valid to "
        "dial.",
        "Set the connection's base_url to a full URL including https:// (or "
        "fill the variable it interpolates, e.g. a *_base_url global "
        "variable), then re-run.",
    ),
    "reference": (
        "A step interpolates a value that doesn't exist at run time — e.g. "
        "${step.response.field} where that step failed, was skipped, or its "
        "response has no such field.",
        "Open the run report's Trace tab and find the *referenced* step (the "
        "name before the first dot): check it ran, succeeded, and that its "
        "response payload actually contains the field being referenced. A "
        "scenario-authoring problem, not a network one.",
    ),
    "error": ("Transactions errored.", "See the error detail and the run trace."),
    "unknown": ("Transactions failed without a captured reason.",
                "Open the run trace for per-step detail."),
}


def _looks_like_load(d: dict) -> bool:
    s = d.get("summary") if isinstance(d.get("summary"), dict) else d
    return isinstance(s, dict) and ("latency_ms" in s or "tps" in s) and "scenarios" not in s


# -- load-run diagnosis ------------------------------------------------------

def diagnose_load(summary: dict) -> list[dict]:
    s = summary.get("summary") if isinstance(summary.get("summary"), dict) else summary
    findings: list[dict] = []

    if s.get("notice"):
        findings.append(_finding(
            "notice", "warn", "Action needed", s["notice"],
            "Follow the notice above (typically: start external workers).",
            evidence=s["notice"]))

    workers = s.get("workers") or 0
    reporting = s.get("workers_reporting") or 0
    sent = int(s.get("sent", 0))
    received = int(s.get("received", 0))
    errors = int(s.get("errors", 0))

    if workers and reporting == 0 and not s.get("notice"):
        findings.append(_finding(
            "no_workers", "error", "No workers reported",
            "No worker joined the run, so no load was generated.",
            "Start the external worker fleet (`LOAD_RUN_ID=<id> python -m "
            "worker.load_worker`) or check the orchestrator log for the "
            "in-process fallback message.",
            evidence=f"workers {reporting}/{workers}"))
        return _rank(findings)

    if sent > 0 and received == 0 and errors > 0:
        cat = classify_error(s.get("last_error"))
        cause, suggestion = _CATEGORY_HELP[cat]
        findings.append(_finding(
            f"all_failing_{cat}", "error",
            "Every transaction is failing",
            f"0 succeeded, {errors} errored. {cause}",
            suggestion,
            evidence=s.get("last_error") or f"{errors} errors, 0 received"))
    elif errors and received:
        rate = errors / (errors + received)
        if rate >= 0.2:
            cat = classify_error(s.get("last_error"))
            cause, suggestion = _CATEGORY_HELP[cat]
            findings.append(_finding(
                f"partial_failures_{cat}", "warn",
                f"Elevated error rate ({rate*100:.0f}%)",
                f"{errors} of {errors+received} transactions failed. {cause}",
                suggestion, evidence=s.get("last_error") or ""))

    pending = int(s.get("pending", 0))
    target = float(s.get("target_tps", 0) or 0)
    tps = float(s.get("tps", 0) or 0)
    if target and received and tps < 0.6 * target and pending > 0:
        findings.append(_finding(
            "below_target", "warn", "Throughput below target",
            f"Achieved ~{tps:.0f} TPS against a target of {target:.0f} with "
            f"{pending} in flight — the target or the network is the bottleneck.",
            "Check target latency/capacity, raise worker concurrency, or lower "
            "the target rate.",
            evidence=f"tps {tps:.0f} / target {target:.0f}, pending {pending}"))

    if not findings:
        findings.append(_finding(
            "healthy", "info", "Run looks healthy",
            f"{received} transactions completed with {errors} errors.",
            "No action needed.",
            evidence=f"received {received}, errors {errors}"))
    return _rank(findings)


# -- functional-run diagnosis ------------------------------------------------

def diagnose_run(detail: dict) -> list[dict]:
    summary = detail.get("summary") or {}
    scenarios = summary.get("scenarios", []) or []
    # bucket failing steps by (category, target.action)
    buckets: dict[tuple[str, str], dict] = {}
    total_fail = 0
    for sc in scenarios:
        for st in sc.get("steps", []) or []:
            if st.get("status") not in ("failed", "error"):
                continue
            total_fail += 1
            reason = st.get("error")
            if not reason:
                bad = next((a for a in st.get("assertions") or []
                            if not a.get("passed")), None)
                if bad:
                    reason = (f"assertion {bad.get('field')} {bad.get('operator')} "
                              f"{bad.get('expected')!r} (got {bad.get('actual')!r})")
            cat = classify_error(reason)
            tgt = f"{st.get('target','')}.{st.get('action','')}"
            key = (cat, tgt)
            b = buckets.setdefault(key, {"count": 0, "example": reason or st.get("status")})
            b["count"] += 1

    if not buckets:
        return [_finding("healthy", "info", "Run passed",
                         "All steps passed.", "No action needed.")]

    findings: list[dict] = []
    for (cat, tgt), b in sorted(buckets.items(), key=lambda kv: -kv[1]["count"]):
        cause, suggestion = _CATEGORY_HELP[cat]
        findings.append(_finding(
            f"{cat}:{tgt}", "error",
            f"{tgt} failing ({b['count']}×)",
            cause, suggestion, evidence=str(b["example"])))
    return _rank(findings)


# -- dispatch ----------------------------------------------------------------

def diagnose(detail: dict) -> dict:
    """Diagnose a run (functional or load). Returns {findings, headline}."""
    findings = diagnose_load(detail) if _looks_like_load(detail) else diagnose_run(detail)
    top = findings[0] if findings else None
    headline = top["title"] if top else "No findings"
    return {"findings": findings, "headline": headline,
            "ok": all(f["severity"] == "info" for f in findings)}


def _rank(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: _SEV.get(f["severity"], 3))
