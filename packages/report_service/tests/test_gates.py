"""Go/No-Go gate tests (pure functions over run-detail dicts)."""
from report_service import GatePolicy, evaluate_gates, regressions, score_cases


def _detail(*, scenarios=None, environment="staging"):
    return {
        "id": "run-1", "label": "demo", "status": "completed",
        "environment": environment,
        "summary": {"scenarios": scenarios or []},
    }


def _scenario(name, status, *, priority=None, steps=None):
    sc = {"scenario_id": name, "name": name, "status": status}
    if priority is not None:
        sc["priority"] = priority
    sc["steps"] = steps if steps is not None else [
        {"step_id": "s1", "target": "http", "action": "send",
         "status": status, "duration_ms": 1, "assertions": []},
    ]
    return sc


def _pack(cases):
    return {"id": "p", "scheme": "X", "label": "L", "cases": cases}


# -- policy parsing ----------------------------------------------------------

def test_policy_defaults_and_from_dict():
    assert GatePolicy.from_dict(None) == GatePolicy()
    pol = GatePolicy.from_dict({"tiers": {"P0": 100, "P1": 95},
                                "max_regressions": 2,
                                "allowed_environments": ["staging"]})
    assert pol.tiers == {"P0": 100.0, "P1": 95.0}
    assert pol.max_regressions == 2
    assert pol.allowed_environments == ("staging",)


# -- score_cases carries priority -------------------------------------------

def test_score_cases_carries_priority():
    detail = _detail(scenarios=[_scenario("auth", "passed")])
    cases = score_cases(detail, _pack([
        {"id": "C1", "scenario": {"name": "auth"}, "priority": "P0"},
    ]))
    assert cases[0]["priority"] == "P0" and cases[0]["passed"] is True


# -- happy path: full GO -----------------------------------------------------

def test_all_pass_full_pack_is_go():
    detail = _detail(scenarios=[
        _scenario("auth", "passed", priority="P0"),
        _scenario("refund", "passed", priority="P1"),
    ])
    pack = _pack([
        {"id": "C1", "scenario": {"name": "auth"}, "priority": "P0"},
        {"id": "C2", "scenario": {"name": "refund"}, "priority": "P1"},
    ])
    res = evaluate_gates(detail, policy={"tiers": {"P0": 100, "P1": 95},
                                         "allowed_environments": ["staging"]},
                         pack=pack)
    assert res["verdict"] == "GO"
    assert res["blocking"] == []


# -- a failed P0 fails its tier (and overall) --------------------------------

def test_failed_p0_blocks_go():
    detail = _detail(scenarios=[_scenario("auth", "failed", priority="P0")])
    pack = _pack([{"id": "C1", "scenario": {"name": "auth"}, "priority": "P0"}])
    res = evaluate_gates(detail, policy={"tiers": {"P0": 100}}, pack=pack)
    assert res["verdict"] == "NO_GO"
    assert "tier:P0" in res["blocking"]


# -- a not_run case fails coverage and no_silent_skips -----------------------

def test_missing_case_fails_coverage():
    # pack expects two cases, run only produced one
    detail = _detail(scenarios=[_scenario("auth", "passed", priority="P0")])
    pack = _pack([
        {"id": "C1", "scenario": {"name": "auth"}, "priority": "P0"},
        {"id": "C2", "scenario": {"name": "refund"}, "priority": "P0"},
    ])
    res = evaluate_gates(detail, policy={"tiers": {"P0": 100}}, pack=pack)
    assert res["verdict"] == "NO_GO"
    assert "coverage" in res["blocking"]


def test_blocked_step_fails_no_silent_skips():
    detail = _detail(scenarios=[_scenario(
        "auth", "passed",
        steps=[{"step_id": "s1", "status": "blocked", "assertions": []}],
    )])
    res = evaluate_gates(detail, policy={"tiers": {"all": 0}})
    assert "no_silent_skips" in res["blocking"]
    assert res["verdict"] == "NO_GO"


# -- regressions vs baseline -------------------------------------------------

def test_regression_vs_baseline_blocks():
    base = _detail(scenarios=[_scenario(
        "auth", "passed",
        steps=[{"step_id": "s1", "status": "passed", "assertions": []}],
    )])
    now = _detail(scenarios=[_scenario(
        "auth", "failed",
        steps=[{"step_id": "s1", "status": "failed", "assertions": []}],
    )])
    regressed, fixed = regressions(now, base)
    assert ("auth", "s1") in regressed and not fixed

    res = evaluate_gates(now, policy={"tiers": {"all": 0},
                                      "forbid_not_run": False},
                         baseline=base)
    assert "regressions" in res["blocking"]


def test_no_baseline_means_no_regression_gate():
    detail = _detail(scenarios=[_scenario("auth", "passed")])
    res = evaluate_gates(detail, policy={"tiers": {"all": 100}})
    assert all(g["id"] != "regressions" for g in res["gates"])
    assert res["verdict"] == "GO"


# -- environment gate --------------------------------------------------------

def test_simulator_environment_blocks_go():
    detail = _detail(scenarios=[_scenario("auth", "passed")],
                     environment="mock")
    res = evaluate_gates(detail, policy={"tiers": {"all": 100},
                                         "allowed_environments": ["staging", "prod-like"]})
    assert "environment" in res["blocking"]


# -- vacuous tier is satisfied, not failed -----------------------------------

def test_tier_with_no_cases_is_ok():
    detail = _detail(scenarios=[_scenario("auth", "passed", priority="P1")])
    pack = _pack([{"id": "C1", "scenario": {"name": "auth"}, "priority": "P1"}])
    res = evaluate_gates(detail, policy={"tiers": {"P0": 100, "P1": 100}}, pack=pack)
    p0 = next(g for g in res["gates"] if g["id"] == "tier:P0")
    assert p0["ok"] is True and "no P0" in p0["detail"]
    assert res["verdict"] == "GO"
