"""The reason a load run shows for a failing transaction (``last_error``).

The runner stores a step's assertion results as plain dicts; the load
worker's reason builder read them as objects until 2026-11-10, so a failed
assertion was swallowed and the card showed ``<conn>.<action>: failed``.
"""

from __future__ import annotations

from worker.engine.runner import FAILED, PASSED, ScenarioResult, StepOutcome
from worker.load_worker import _failure_reason


def _result(step: StepOutcome) -> ScenarioResult:
    return ScenarioResult(scenario_id="sc", name="purchase", status=FAILED, steps=[step])


def test_failed_assertion_stored_as_dict_is_reported():
    step = StepOutcome(
        step_id="s1",
        target="pos_host",
        action="send_message",
        status=FAILED,
        duration_ms=12,
        response={"response_code": "000"},
        assertions=[
            {
                "field": "response_code",
                "expected": "00",
                "actual": "000",
                "operator": "==",
                "passed": False,
            }
        ],
    )
    assert _failure_reason(_result(step)) == (
        "pos_host.send_message: assertion response_code == '00' (got '000')"
    )


def test_adapter_error_wins_over_assertions():
    step = StepOutcome(
        step_id="s1",
        target="pos_host",
        action="send_message",
        status=FAILED,
        duration_ms=12,
        error="No response within 15s.",
        assertions=[
            {
                "field": "response_code",
                "expected": "00",
                "actual": None,
                "operator": "==",
                "passed": False,
            }
        ],
    )
    assert _failure_reason(_result(step)) == "pos_host.send_message: No response within 15s."


def test_no_error_and_no_failed_assertion_names_the_response_code():
    step = StepOutcome(
        step_id="s1",
        target="pos_host",
        action="send_message",
        status=FAILED,
        duration_ms=12,
        response={"response_code": "94"},
    )
    assert _failure_reason(_result(step)) == "pos_host.send_message: failed (response_code 94)"


def test_first_failed_step_is_the_one_reported():
    ok = StepOutcome(
        step_id="s0", target="pos_host", action="send_0800", status=PASSED, duration_ms=1
    )
    bad = StepOutcome(
        step_id="s1",
        target="pos_host",
        action="send_message",
        status=FAILED,
        duration_ms=1,
        error="peer closed the connection",
    )
    r = ScenarioResult(scenario_id="sc", name="x", status=FAILED, steps=[ok, bad])
    assert _failure_reason(r) == "pos_host.send_message: peer closed the connection"
    assert (
        _failure_reason(ScenarioResult(scenario_id="sc", name="x", status=FAILED))
        == "scenario failed"
    )
