"""Diagnose-my-run: failure taxonomy + cause/suggestion."""
from report_service import diagnose, classify_error


def test_classify_error_taxonomy():
    assert classify_error("ConnectionRefusedError: [Errno 111] refused") == "unreachable"
    assert classify_error("read timed out after 5000ms") == "timeout"
    assert classify_error("HTTP 401 Unauthorized") == "auth"
    assert classify_error("assertion response_code eq '00' (got '05')") == "assertion"
    assert classify_error("TLS handshake failed") == "tls"
    assert classify_error("no connection bound for target switch") == "binding"
    assert classify_error("unresolved reference: 'gen_cvk.response.key'") == "reference"
    assert classify_error(
        "UnsupportedProtocol: Request URL is missing an 'http://' or "
        "'https://' protocol."
    ) == "url"
    assert classify_error("http connection has no usable base_url") == "url"
    assert classify_error(None) == "unknown"


# -- load runs ---------------------------------------------------------------

def _load(**kw):
    base = {"sent": 0, "received": 0, "errors": 0, "pending": 0,
            "tps": 0, "target_tps": 0, "workers": 4, "workers_reporting": 4,
            "latency_ms": {"p95": 0}, "last_error": None}
    base.update(kw)
    return base


def test_load_all_failing_unreachable():
    d = diagnose(_load(sent=1000, errors=1000,
                       last_error="http.send_auth_request: Connection refused"))
    top = d["findings"][0]
    assert top["code"] == "all_failing_unreachable"
    assert top["severity"] == "error"
    assert "refused" in top["evidence"].lower()
    assert not d["ok"]


def test_load_no_workers():
    d = diagnose(_load(workers=4, workers_reporting=0, sent=0))
    assert d["findings"][0]["code"] == "no_workers"
    assert "worker" in d["headline"].lower()


def test_load_below_target():
    d = diagnose(_load(sent=500, received=480, errors=0, pending=300,
                       tps=200, target_tps=1000))
    codes = [f["code"] for f in d["findings"]]
    assert "below_target" in codes


def test_load_healthy():
    d = diagnose(_load(sent=1000, received=1000, errors=0, tps=950, target_tps=1000))
    assert d["ok"] is True
    assert d["findings"][0]["code"] == "healthy"


def test_tcp_no_response_is_a_timeout():
    assert classify_error("svfe_pos.send_message: No response within 30s.") == "timeout"


def test_load_small_share_of_timeouts_is_not_called_healthy():
    # 1.9 % errors used to read "Run looks healthy"; the outcome split names
    # the timeouts and the declines separately, whatever the last error was.
    d = diagnose(_load(
        sent=139995, received=137385, errors=2610, tps=500, target_tps=500,
        last_error="svfe_pos.send_message: assertion response_code eq '000' (got '120')",
        error_outcomes={"rejected": 2580, "timeout": 30, "connection": 0, "other": 0},
        late_replies=28))
    codes = [f["code"] for f in d["findings"]]
    assert "healthy" not in codes
    assert codes[0] == "outcome_timeout"
    assert "outcome_rejected" in codes and "outcome_connection" not in codes
    t = d["findings"][0]
    assert "28 of those replies arrived later" in t["cause"]
    assert t["evidence"] == "timeout 30, late replies 28"


def test_load_timeouts_without_late_replies_point_at_drops():
    d = diagnose(_load(sent=100, received=90, errors=10,
                       error_outcomes={"rejected": 0, "timeout": 10,
                                       "connection": 0, "other": 0}))
    t = next(f for f in d["findings"] if f["code"] == "outcome_timeout")
    assert "no late reply" in t["cause"]


def test_load_run_recorded_before_the_split_gets_no_outcome_findings():
    d = diagnose(_load(sent=1000, received=990, errors=10))
    assert not any(f["code"].startswith("outcome_") for f in d["findings"])


def test_load_notice_surfaced():
    d = diagnose(_load(sent=0, notice="start external workers"))
    assert d["findings"][0]["code"] == "notice"


# -- functional runs ---------------------------------------------------------

def _run(steps):
    return {"id": "r", "summary": {"scenarios": [{"name": "auth", "steps": steps}]}}


def test_functional_groups_failures_by_target():
    d = diagnose(_run([
        {"step_id": "s1", "target": "http", "action": "send_auth_request",
         "status": "error", "error": "Connection refused"},
        {"step_id": "s2", "target": "http", "action": "send_auth_request",
         "status": "error", "error": "Connection refused"},
    ]))
    top = d["findings"][0]
    assert top["code"].startswith("unreachable:http.send_auth_request")
    assert "2×" in top["title"]


def test_functional_assertion_failure():
    d = diagnose(_run([
        {"step_id": "s1", "target": "http", "action": "send_auth_request",
         "status": "failed", "assertions": [
             {"field": "response_code", "operator": "eq", "expected": "00",
              "actual": "05", "passed": False}]},
    ]))
    assert d["findings"][0]["code"].startswith("assertion:")


def test_functional_passed_is_ok():
    d = diagnose(_run([{"step_id": "s1", "target": "t", "action": "a",
                        "status": "passed"}]))
    assert d["ok"] is True
