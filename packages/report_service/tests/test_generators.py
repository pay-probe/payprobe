"""Report generator tests (pure functions over run-detail dicts)."""
from xml.dom import minidom

from report_service import (
    certify, certification_html, signoff_html, junit_xml, html_report,
    index_steps, run_rollup,
)


def _detail(status="passed", err=None):
    return {
        "id": "run-1", "label": "demo", "status": "completed",
        "environment": "mock", "started_at": "2026-06-20T10:00:00",
        "completed_at": "2026-06-20T10:00:30",
        "summary": {"scenarios": [{
            "name": "auth happy path", "status": status,
            "steps": [
                {"step_id": "step_001", "target": "http", "action": "send_auth_request",
                 "status": status, "duration_ms": 42, "error": err,
                 "assertions": [{"field": "response_code", "operator": "eq",
                                 "expected": "00", "actual": "00", "passed": status == "passed"}]},
            ],
        }]},
    }


# -- junit -------------------------------------------------------------------

def test_junit_is_well_formed_and_counts():
    xml = junit_xml(_detail("failed", err="boom"))
    dom = minidom.parseString(xml)  # raises if malformed
    suites = dom.getElementsByTagName("testsuites")[0]
    assert suites.getAttribute("tests") == "1"
    assert suites.getAttribute("failures") == "1"
    assert "<failure" in xml and "boom" in xml


def test_junit_escapes_attributes():
    d = _detail("failed", err='quote " and <tag>')
    xml = junit_xml(d)
    minidom.parseString(xml)  # must still parse despite special chars


# -- certification -----------------------------------------------------------

def _pack():
    return {"id": "visa", "scheme": "VISA", "label": "Visa core",
            "cases": [
                {"id": "C1", "requirement": "auth approved",
                 "scenario": {"name": "auth happy path"}},
                {"id": "C2", "requirement": "refund", "scenario": {"name": "refund flow"}},
            ]}


def test_certify_scores_compliance():
    cert = certify(_detail("passed"), _pack())
    assert cert["total"] == 2
    assert cert["passed"] == 1            # only auth happy path ran+passed
    assert cert["compliance_pct"] == 50.0
    assert cert["certified"] is False
    # the missing case is marked not_run
    statuses = {c["id"]: c["status"] for c in cert["cases"]}
    assert statuses["C2"] == "not_run"


def test_certify_full_compliance_certifies():
    pack = {"id": "p", "scheme": "X", "label": "L",
            "cases": [{"id": "C1", "requirement": "r", "scenario": {"name": "auth happy path"}}]}
    cert = certify(_detail("passed"), pack)
    assert cert["certified"] is True and cert["compliance_pct"] == 100.0
    html = certification_html(cert)
    assert "CERTIFIED" in html and "100" in html


# -- html report + diff ------------------------------------------------------

def test_html_report_self_contained():
    html = html_report(_detail("passed"))
    assert html.startswith("<!doctype html>")
    assert "http.send_auth_request" in html
    assert "http://" not in html  # no external assets


def test_index_steps_for_diff():
    idx = index_steps(_detail("failed")["summary"])
    assert idx[("auth happy path", "step_001")] == "failed"


# -- sign-off document -------------------------------------------------------

def _snapshot(verdict="GO"):
    return {
        "id": "so-1", "run_id": "run-1", "pack": "visa-base1",
        "environment": "staging", "verdict": verdict,
        "created_at": "2026-06-28T10:00:00", "created_by": "david",
        "content_hash": "sha256:deadbeef",
        "gate_result": {
            "verdict": verdict,
            "gates": [
                {"id": "tier:P0", "ok": True, "detail": "12/12 P0 passed (100%)"},
                {"id": "regressions", "ok": verdict == "GO",
                 "detail": "0 regressions vs baseline"},
            ],
            "blocking": [] if verdict == "GO" else ["regressions"],
        },
        "provenance": {
            "environment": "staging",
            "pack": {"id": "visa-base1", "version": "2.1"},
            "system_under_test": {"build": "abcd123", "source": "git"},
            "triggered_by": "david", "baseline_run_id": "7af3",
            "started_at": "2026-06-28T09:59:00", "completed_at": "2026-06-28T10:00:00",
            "endpoints": [{"target": "issuer", "host": "10.0.1.50", "port": 7000}],
        },
        "approvals": [{"by": "alice", "decision": "approved", "note": "lgtm",
                       "at": "2026-06-28T10:05:00"}],
    }


def test_signoff_html_is_self_contained_and_shows_verdict():
    html = signoff_html(_snapshot("GO"))
    assert html.startswith("<!doctype html>")
    assert "http://" not in html.replace("10.0.1.50", "")  # no external assets
    assert ">GO<" in html
    assert "sha256:deadbeef" in html
    assert "issuer" in html and "alice" in html
    assert "abcd123" in html  # SUT build stamped


def test_signoff_html_renders_nogo():
    html = signoff_html(_snapshot("NO_GO"))
    assert "NO-GO" in html
    assert "1 blocking gate(s)" in html


def test_signoff_html_notes_playground_contamination():
    snap = _snapshot("GO")
    snap["provenance"]["playground_traffic"] = {
        "total": 4, "by_simulator": {"visa-sim": 4},
        "window": {"from": "t0", "to": "t1"}}
    html = signoff_html(snap)
    assert "Playground traffic" in html
    assert "ad-hoc playground message(s)" in html
    assert ">4</span>" in html          # the contamination count
    assert "visa-sim: 4" in html


def test_signoff_html_playground_clean_when_absent():
    html = signoff_html(_snapshot("GO"))
    assert "Playground traffic" in html
    assert "no playground traffic in the run window" in html


# -- enriched details ---------------------------------------------------------

def _rich_detail():
    d = _detail("passed")
    d["summary"]["phases"] = {
        "phase_1": {"status": "passed", "components": 3, "failed": [],
                    "recovered": ["issuer_sim"]},
        "phase_2": {"status": "passed", "passed": 1, "failed": 0, "blocked": 0},
    }
    d["summary"]["scenarios"].append({
        "name": "refund flow", "status": "failed",
        "steps": [
            {"step_id": "step_010", "target": "tcp_iso8583", "action": "send_message",
             "status": "failed", "duration_ms": 2500, "error": "DE39 expected 00 got 05",
             "environment_override": "staging",
             "request": {"mti": "0200", "de39": None},
             "response": {"mti": "0210", "de39": "05"},
             "raw_log": ">> 0200 ...\n<< 0210 ...",
             "assertions": [{"field": "de39", "operator": "eq",
                             "expected": "00", "actual": "05", "passed": False}]},
        ],
    })
    return d


def test_run_rollup_counts_and_triage():
    roll = run_rollup(_rich_detail())
    assert roll["scenarios"] == {"total": 2, "passed": 1, "failed": 1, "blocked": 0}
    assert roll["steps"]["total"] == 2 and roll["steps"]["failed"] == 1
    assert roll["assertions"] == {"total": 2, "passed": 1}
    assert roll["step_ms"] == 2542
    assert roll["wall_ms"] == 30_000  # from started/completed timestamps
    assert roll["pass_rate"] == 50.0
    assert roll["failures"][0]["step_id"] == "step_010"
    assert roll["slowest"][0]["duration_ms"] == 2500


def test_html_report_has_summary_triage_and_payloads():
    html = html_report(_rich_detail())
    assert "Failure triage" in html and "DE39 expected 00 got 05" in html
    assert "Slowest steps" in html
    assert "Phases" in html and "recovered on retry" in html
    assert "pass rate" in html
    assert "&quot;de39&quot;: &quot;05&quot;" in html      # response payload embedded
    assert "wire log" in html                               # raw_log embedded
    assert "@staging" in html                               # env override badge
    assert "http://" not in html                            # still self-contained


def test_junit_has_time_skipped_and_assertion_output():
    d = _rich_detail()
    xml = junit_xml(d)
    dom = minidom.parseString(xml)
    suites = dom.getElementsByTagName("testsuites")[0]
    assert suites.getAttribute("skipped") == "0"
    assert float(suites.getAttribute("time")) > 2.5
    suite = dom.getElementsByTagName("testsuite")[0]
    assert suite.getAttribute("timestamp") == "2026-06-20T10:00:00"
    assert "system-out" in xml and "de39" in xml


def test_certify_priority_breakdown_and_gaps():
    pack = {"id": "p", "scheme": "X", "label": "L", "cases": [
        {"id": "C1", "requirement": "auth", "priority": "P0",
         "scenario": {"name": "auth happy path"}},
        {"id": "C2", "requirement": "refund", "priority": "P1",
         "scenario": {"name": "refund flow"}},
        {"id": "C3", "requirement": "void", "priority": "P1",
         "scenario": {"name": "void flow"}},
    ]}
    cert = certify(_rich_detail(), pack)
    assert cert["by_priority"]["P0"] == {"passed": 1, "total": 1, "pct": 100.0}
    assert cert["by_priority"]["P1"]["passed"] == 0
    assert cert["failed_cases"] == ["C2"]
    assert cert["not_run_cases"] == ["C3"]
    html = certification_html(cert)
    assert "P1" in html and "Not run:" in html and "refund flow" in html


def test_signoff_html_includes_evidence_when_summary_frozen():
    snap = _snapshot("GO")
    snap["summary"] = _rich_detail()["summary"]
    html = signoff_html(snap)
    assert "Evidence" in html
    assert "refund flow" in html and "DE39 expected 00 got 05" in html
    assert "2/2 gate(s) passed" in html
