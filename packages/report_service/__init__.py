"""PayProbe Report Service — run/certification report generators.

Pure, dependency-free format logic (JUnit, HTML, certification, run diffs) over
a run-detail dict. Imported by the orchestrator today; can be lifted into a
standalone FastAPI service later without touching the format code.
"""
from .generators import (
    certify,
    score_cases,
    certification_html,
    signoff_html,
    junit_xml,
    html_report,
    index_steps,
    run_rollup,
    HTML_CSS,
)
from .diagnose import diagnose, diagnose_run, diagnose_load, classify_error
from .gates import GatePolicy, evaluate_gates, regressions
from .provenance import provenance, content_hash

__all__ = [
    "certify",
    "score_cases",
    "certification_html",
    "signoff_html",
    "junit_xml",
    "html_report",
    "index_steps",
    "run_rollup",
    "HTML_CSS",
    "diagnose",
    "diagnose_run",
    "diagnose_load",
    "classify_error",
    "GatePolicy",
    "evaluate_gates",
    "regressions",
    "provenance",
    "content_hash",
]
