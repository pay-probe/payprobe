"""${...} templating and the condition evaluator: pure, no database."""

import pytest
from agent_hub.exprs import ExpressionError, evaluate, lookup, render

CTX = {
    "inputs": {"network": "main-topology", "tps": 120, "tags": ["smoke", "nats"]},
    "review": {"json": {"verdict": "approve", "reasons": []}, "status": "done"},
    "planner": {"proposed": [{"tool": "create_scenario"}], "plan_id": "p1", "json": None},
}


def test_lookup_walks_dicts_and_lists_and_tolerates_missing():
    assert lookup(CTX, "inputs.network") == "main-topology"
    assert lookup(CTX, "review.json.verdict") == "approve"
    assert lookup(CTX, "inputs.tags.1") == "nats"
    assert lookup(CTX, "inputs.tags.9") is None
    assert lookup(CTX, "nope.at.all") is None
    assert lookup(CTX, "planner.json.anything") is None  # None mid-path


def test_render_passes_values_through_and_interpolates():
    assert render("${inputs.tps}", CTX) == 120  # whole-string ref keeps the type
    assert render("${planner.proposed}", CTX) == [{"tool": "create_scenario"}]
    assert render("Certify ${inputs.network} at ${inputs.tps} tps", CTX) == (
        "Certify main-topology at 120 tps"
    )
    assert render("plan: ${planner.proposed}", CTX) == 'plan: [{"tool": "create_scenario"}]'
    assert render("missing: [${nope}]", CTX) == "missing: []"
    assert render({"a": "${inputs.network}", "b": ["${inputs.tps}", 1]}, CTX) == {
        "a": "main-topology",
        "b": [120, 1],
    }
    assert render(42, CTX) == 42


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('${review.json.verdict} == "approve"', True),
        ('${review.json.verdict} != "approve"', False),
        ("${inputs.tps} > 100", True),
        ("${inputs.tps} >= 120 and ${inputs.tps} < 200", True),
        ('"smoke" in ${inputs.tags}', True),
        ('"prod" not in ${inputs.tags}', True),
        ("not ${planner.json}", True),  # None is falsy
        ("${planner.plan_id} == null", False),
        ("${missing.path} == null", True),  # missing resolves to None, not an error
        ("${review.status} == 'done' or false", True),
        ("(${inputs.tps} > 500) or (${review.json.verdict} == 'approve')", True),
        ("${inputs.tps} in [100, 120]", True),
        ("true", True),
        ("False", False),
    ],
)
def test_evaluate_supported_subset(expr, expected):
    assert evaluate(expr, CTX) is expected


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",
        "${inputs.tps}.bit_length()",
        "inputs['tps'] > 1",
        "len(${inputs.tags}) > 0",
        "x == 1",
        "${inputs.tps} + 1 > 100",
        "lambda: 1",
        "${inputs.tps} >",
    ],
)
def test_evaluate_refuses_everything_outside_the_subset(expr):
    with pytest.raises(ExpressionError):
        evaluate(expr, CTX)


def test_evaluate_ordering_a_non_number_is_an_error_not_a_guess():
    with pytest.raises(ExpressionError):
        evaluate("${review.json.verdict} > 3", CTX)
