"""ADR-0014: loop containers, error ports, failure policies, handled failures.

Run from the packages/ directory:
    cd packages && PYTHONPATH=. python -m pytest worker/tests/test_loop_failure_policy.py
"""

import pytest

from worker.engine import (
    FAILED,
    HANDLED,
    PASSED,
    STEP_RESULT,
    InMemorySink,
    WorkerEngine,
)

MOCK_ENV = {
    "mode": "mock",
    "connection_budget": {"total": 1000, "adapters": {"http": 400}},
    "adapters": {"http": {"mock": True, "latency_ms": 0}},
}

#: Fails for the loop item "b", passes for every other item.
CHECK_CODE = (
    "item = context['loop']['item']\n"
    "if item == 'b':\n"
    "    raise ValueError('bad item ' + item)\n"
    "return {'ok': item}"
)


def _check(**kw):
    return {
        "id": "check",
        "kind": "code",
        "config": {"language": "python", "code": CHECK_CODE},
        **kw,
    }


def _echo(sid, **kw):
    return {
        "id": sid,
        "target": "http",
        "action": "echo_test",
        "payload": {},
        "assertions": [],
        **kw,
    }


def _scenario(steps, edges, **kw):
    return {
        "id": "sc",
        "name": "loop_policy",
        "test_class": "e2e",
        "steps": steps,
        "edges": edges,
        **kw,
    }


def _loop(items=("a", "b", "c"), **cfg):
    return {"id": "lp", "kind": "loop", "config": {"mode": "forEach", "list": list(items), **cfg}}


BODY_EDGES = [
    {"source": "lp", "source_port": "loop", "target": "check"},
    {"source": "check", "source_port": "out", "target": "inner"},
    {"source": "lp", "source_port": "done", "target": "after"},
]


async def _run(scenario):
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    summary = await engine.run_scenario_batch([scenario], run_id="r14")
    sc = summary["scenarios"][0]
    by_id: dict[str, list[dict]] = {}
    for s in sc["steps"]:
        by_id.setdefault(s["step_id"], []).append(s)
    return sc, by_id, sink


@pytest.mark.asyncio
async def test_next_skips_the_rest_of_the_failed_iteration_and_passes():
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="next"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    assert [s["status"] for s in by_id["check"]] == [PASSED, HANDLED, PASSED]
    assert len(by_id["inner"]) == 2  # skipped for "b"
    assert len(by_id["after"]) == 1
    assert sc["status"] == PASSED
    assert sc["handled"] == {"check:next": 1}
    assert "handled:check:next" in sc["notes"]
    failed = by_id["check"][1]
    assert failed["failure_route"] == {"route": "next", "status": "error"}
    assert any("on_failure=next" in t["msg"] for t in failed["trace"])
    assert by_id["lp"][0]["response"] == {
        "iterations": 3,
        "ended": "done",
        "failed_iterations": 1,
    }


@pytest.mark.asyncio
async def test_break_leaves_the_loop_at_done():
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="break"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    assert len(by_id["check"]) == 2  # a, b — never c
    assert len(by_id["after"]) == 1
    assert by_id["lp"][0]["response"]["ended"] == "break"
    assert by_id["lp"][0]["response"]["iterations"] == 2
    assert sc["status"] == PASSED


@pytest.mark.asyncio
async def test_break_takes_the_loop_error_port_when_wired():
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="break"),
                _echo("inner", parent="lp"),
                _echo("after"),
                _echo("rescue"),
            ],
            [*BODY_EDGES, {"source": "lp", "source_port": "error", "target": "rescue"}],
        )
    )
    assert "rescue" in by_id and "after" not in by_id
    assert sc["status"] == PASSED


@pytest.mark.asyncio
async def test_loop_default_policy_and_step_override():
    # The loop says next; the step overrides with ignore, so "inner" still runs.
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(on_failure="next"),
                _check(parent="lp", on_failure="ignore"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    assert len(by_id["inner"]) == 3
    assert sc["handled"] == {"check:ignore": 1}
    # Without the override the loop default applies.
    sc2, by_id2, _ = await _run(
        _scenario(
            [
                _loop(on_failure="next"),
                _check(parent="lp"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    assert len(by_id2["inner"]) == 2
    assert sc2["status"] == PASSED


@pytest.mark.asyncio
async def test_handled_failures_fail_switch_keeps_the_route_but_fails_the_run():
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(on_failure="next", handled_failures="fail"),
                _check(parent="lp"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    assert sc["status"] == FAILED
    assert by_id["check"][1]["status"] == "error"  # not handled
    assert len(by_id["inner"]) == 2 and len(by_id["after"]) == 1  # route still taken
    assert sc["handled"] == {}


@pytest.mark.asyncio
async def test_error_port_routes_to_recovery_that_reads_the_failure():
    """Timeout-then-reversal shape: a failure is a standard path, the run passes."""
    sc, by_id, _ = await _run(
        _scenario(
            [
                {
                    "id": "auth",
                    "kind": "code",
                    "config": {"language": "python", "code": "raise TimeoutError('no answer')"},
                },
                {
                    "id": "reversal",
                    "kind": "init",
                    "config": {"data": '{"why": "${auth.error}", "was": "${auth.status}"}'},
                },
                _echo("happy"),
            ],
            [
                {"source": "auth", "source_port": "out", "target": "happy"},
                {"source": "auth", "source_port": "error", "target": "reversal"},
            ],
        )
    )
    assert sc["status"] == PASSED
    assert "happy" not in by_id
    assert by_id["auth"][0]["status"] == HANDLED
    assert by_id["auth"][0]["failure_route"]["route"] == "error_port"
    resp = by_id["reversal"][0]["response"]
    assert resp["was"] == "error" and "no answer" in resp["why"]


@pytest.mark.asyncio
async def test_explicit_stop_stops_even_when_stop_on_failure_is_off():
    sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="stop"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
            stop_on_failure=False,
        )
    )
    assert sc["status"] == FAILED
    assert len(by_id["check"]) == 2 and "after" not in by_id


@pytest.mark.asyncio
async def test_nothing_set_behaves_exactly_as_before():
    # stop_on_failure off, no policy, no error port: the body carries on, the
    # run fails, and nothing ADR-0014 shows up on the outcome.
    sc, by_id, _ = await _run(
        _scenario(
            [_loop(), _check(), _echo("inner"), _echo("after")],
            BODY_EDGES,
            stop_on_failure=False,
        )
    )
    assert sc["status"] == FAILED
    assert len(by_id["inner"]) == 3
    assert by_id["check"][1]["status"] == "error"
    assert by_id["check"][1]["failure_route"] is None
    assert sc["handled"] == {} and sc["notes"] == []
    # stop_on_failure on (the default): the first failure ends the scenario.
    sc2, by_id2, _ = await _run(
        _scenario(
            [_loop(), _check(), _echo("inner"), _echo("after")],
            BODY_EDGES,
        )
    )
    assert sc2["status"] == FAILED
    assert len(by_id2["check"]) == 2 and "after" not in by_id2


@pytest.mark.asyncio
async def test_inner_break_leaves_only_the_inner_loop():
    steps = [
        {"id": "outer", "kind": "loop", "config": {"mode": "times", "count": 2}},
        {
            "id": "lp",
            "kind": "loop",
            "parent": "outer",
            "config": {"mode": "forEach", "list": ["a", "b", "c"]},
        },
        _check(parent="lp", on_failure="break"),
        _echo("tail", parent="outer"),
        _echo("after"),
    ]
    edges = [
        {"source": "outer", "source_port": "loop", "target": "lp"},
        {"source": "lp", "source_port": "loop", "target": "check"},
        {"source": "lp", "source_port": "done", "target": "tail"},
        {"source": "outer", "source_port": "done", "target": "after"},
    ]
    sc, by_id, _ = await _run(_scenario(steps, edges))
    assert len(by_id["check"]) == 4  # (a, b) twice
    assert len(by_id["tail"]) == 2
    assert len(by_id["after"]) == 1
    assert sc["status"] == PASSED


@pytest.mark.asyncio
async def test_loop_variable_is_restored_after_a_break():
    _sc, by_id, _ = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="break"),
                {
                    "id": "after",
                    "kind": "code",
                    "config": {
                        "language": "python",
                        "code": "return {'has_loop': 'loop' in context}",
                    },
                },
            ],
            [
                {"source": "lp", "source_port": "loop", "target": "check"},
                {"source": "lp", "source_port": "done", "target": "after"},
            ],
        )
    )
    assert by_id["after"][0]["response"] == {"has_loop": False}


@pytest.mark.asyncio
async def test_event_status_matches_the_stored_status():
    _sc, by_id, sink = await _run(
        _scenario(
            [
                _loop(),
                _check(parent="lp", on_failure="next"),
                _echo("inner", parent="lp"),
                _echo("after"),
            ],
            BODY_EDGES,
        )
    )
    events = [e.data["status"] for e in sink.of_type(STEP_RESULT) if e.data["step"] == "check"]
    assert events == [s["status"] for s in by_id["check"]] == [PASSED, HANDLED, PASSED]


@pytest.mark.asyncio
async def test_call_route_applies_to_the_call_step_not_its_children():
    sub = {
        "id": "sub",
        "name": "sub",
        "test_class": "e2e",
        "steps": [
            {
                "id": "boom",
                "kind": "code",
                "config": {"language": "python", "code": "raise ValueError('x')"},
            }
        ],
    }
    parent = _scenario(
        [
            {"id": "c", "kind": "call", "config": {"scenario_id": "sub"}, "on_failure": "ignore"},
            _echo("after"),
        ],
        [{"source": "c", "source_port": "out", "target": "after"}],
        subflows={"sub": sub},
    )
    sc, by_id, _ = await _run(parent)
    assert by_id["c"][0]["status"] == HANDLED
    assert "after" in by_id
    assert sc["status"] == PASSED


@pytest.mark.asyncio
async def test_linear_scenario_honours_ignore():
    sc, by_id, _ = await _run(
        {
            "id": "lin",
            "name": "lin",
            "test_class": "e2e",
            "steps": [
                {
                    "id": "boom",
                    "kind": "code",
                    "on_failure": "ignore",
                    "config": {"language": "python", "code": "raise ValueError('x')"},
                },
                _echo("after"),
            ],
        }
    )
    assert by_id["boom"][0]["status"] == HANDLED
    assert "after" in by_id
    assert sc["status"] == PASSED


@pytest.mark.asyncio
async def test_a_contained_step_is_never_the_entry():
    # "stray" is inside the box, unwired, and listed first.
    _sc, by_id, _ = await _run(
        _scenario(
            [_echo("stray", parent="lp"), _loop(items=("a",)), _check(parent="lp"), _echo("after")],
            [
                {"source": "lp", "source_port": "loop", "target": "check"},
                {"source": "lp", "source_port": "done", "target": "after"},
            ],
        )
    )
    assert "stray" not in by_id
    assert by_id["lp"][0]["response"]["iterations"] == 1


@pytest.mark.asyncio
async def test_recovery_reads_live_values_not_the_redacted_copy():
    """A step whose adapter raised: the published outcome is redacted, but the
    reversal on its error port must send the real value it read from it."""
    scenario = _scenario(
        [
            _echo("auth", payload={"pin_block": "S3CRET"}),
            {
                "id": "reversal",
                "kind": "code",
                "config": {
                    "language": "python",
                    "code": (
                        "req = context['auth']['request']['pin_block']\n"
                        "return {'reversed': req[::-1], 'was': context['auth']['status']}"
                    ),
                },
            },
        ],
        [{"source": "auth", "source_port": "error", "target": "reversal"}],
        __secrets__=["S3CRET"],
    )
    engine = WorkerEngine(MOCK_ENV, InMemorySink())

    async def boom(target, action, payload, assertions):
        raise TimeoutError("no answer in 30000 ms")

    engine._execute_step = boom  # the adapter raises on dispatch
    summary = await engine.run_scenario_batch([scenario], run_id="r14s")
    by_id = {s["step_id"]: s for s in summary["scenarios"][0]["steps"]}
    assert by_id["auth"]["status"] == HANDLED
    assert by_id["auth"]["request"] == {"pin_block": "***"}  # stored copy redacted
    # the live context carried the real value to the recovery step: reversed,
    # it survives redaction, so "TERC3S" proves it was not "***"
    assert by_id["reversal"]["status"] == PASSED
    assert by_id["reversal"]["response"] == {"reversed": "TERC3S", "was": "error"}
