"""Provenance + content-hash tests."""
from report_service import provenance, content_hash


def _detail():
    return {
        "id": "run-1", "environment": "staging",
        "started_at": "2026-06-28T10:00:00", "completed_at": "2026-06-28T10:01:00",
        "summary": {"scenarios": [
            {"scenario_id": "auth", "name": "auth", "version": "v4", "status": "passed"},
            {"scenario_id": "refund", "name": "refund", "status": "passed"},
        ]},
    }


def _ctx():
    return {
        "pack": {"id": "visa-base1", "version": "2.1"},
        "endpoints": [{"target": "issuer", "host": "10.0.1.50", "port": 7000}],
        "system_under_test": {"build": "abcd123", "source": "git"},
        "triggered_by": "david@example.com",
        "baseline_run_id": "7af3beef",
    }


def test_provenance_captures_context_and_versions():
    prov = provenance(_detail(), _ctx())
    assert prov["run_id"] == "run-1"
    assert prov["environment"] == "staging"
    assert prov["scenario_versions"] == {"auth": "v4", "refund": None}
    assert prov["pack"] == {"id": "visa-base1", "version": "2.1"}
    assert prov["baseline_run_id"] == "7af3beef"
    assert prov["endpoints"][0]["host"] == "10.0.1.50"


def test_provenance_stable_shape_without_ctx():
    prov = provenance(_detail())
    # absent fields are recorded as None / [] rather than omitted
    assert prov["pack"] is None
    assert prov["system_under_test"] is None
    assert prov["endpoints"] == []
    # ADR-0007: playground contamination defaults to an empty (stable) shape
    assert prov["playground_traffic"] == {
        "total": 0, "by_simulator": {}, "window": None}
    assert set(prov) == {
        "run_id", "scenario_versions", "pack", "environment", "endpoints",
        "system_under_test", "triggered_by", "started_at", "completed_at",
        "baseline_run_id", "playground_traffic",
    }


def test_provenance_records_playground_contamination():
    prov = provenance(_detail(), {"playground_traffic": {
        "total": 3, "by_simulator": {"visa-sim": 3},
        "window": {"from": "t0", "to": "t1"}}})
    assert prov["playground_traffic"]["total"] == 3
    assert prov["playground_traffic"]["by_simulator"] == {"visa-sim": 3}


def test_content_hash_is_stable_and_prefixed():
    detail = _detail()
    prov = provenance(detail, _ctx())
    gate = {"verdict": "GO", "gates": [], "blocking": []}
    h1 = content_hash(summary=detail["summary"], gate_result=gate, prov=prov)
    h2 = content_hash(summary=detail["summary"], gate_result=gate, prov=prov)
    assert h1 == h2                       # deterministic
    assert h1.startswith("sha256:")


def test_content_hash_is_order_independent():
    """Key order in the inputs must not change the hash (canonical form)."""
    detail = _detail()
    prov = provenance(detail, _ctx())
    gate_a = {"verdict": "GO", "gates": [], "blocking": []}
    gate_b = {"blocking": [], "gates": [], "verdict": "GO"}
    assert content_hash(summary=detail["summary"], gate_result=gate_a, prov=prov) == \
        content_hash(summary=detail["summary"], gate_result=gate_b, prov=prov)


def test_content_hash_changes_on_tamper():
    detail = _detail()
    prov = provenance(detail, _ctx())
    gate = {"verdict": "GO", "gates": [], "blocking": []}
    base = content_hash(summary=detail["summary"], gate_result=gate, prov=prov)

    tampered_gate = {"verdict": "NO_GO", "gates": [], "blocking": ["tier:P0"]}
    assert content_hash(summary=detail["summary"], gate_result=tampered_gate, prov=prov) != base

    tampered_summary = {"scenarios": []}
    assert content_hash(summary=tampered_summary, gate_result=gate, prov=prov) != base
