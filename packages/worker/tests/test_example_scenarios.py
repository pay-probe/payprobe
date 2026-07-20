"""Every bundled example scenario must run end-to-end with a clear result.

This guards the core promise (Definition of Done): no shipped scenario errors
out, silently skips, or swallows an exception. Each scenario in
``examples/scenarios/*.json`` is executed through the real engine in mock mode
and must produce a definite per-step status — never ``error``.

Run from the packages/ directory:
    cd packages && python -m pytest worker/tests/test_example_scenarios.py -v
"""

import json
import pathlib

import pytest

from worker.engine import WorkerEngine, InMemorySink, PASSED, FAILED, BLOCKED

# packages/worker/tests/this_file -> repo root is parents[3]
EXAMPLES = pathlib.Path(__file__).resolve().parents[3] / "examples" / "scenarios"
SCENARIO_FILES = sorted(EXAMPLES.glob("*.json"))

MOCK_ENV = {"mode": "mock", "adapters": {}}

CLEAR_STATUSES = {PASSED, FAILED, BLOCKED}


def test_examples_directory_is_present_and_nonempty():
    assert SCENARIO_FILES, f"no example scenarios found in {EXAMPLES}"


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.name)
async def test_example_scenario_runs_end_to_end(path):
    scenario = json.loads(path.read_text())
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id=f"ex-{path.stem}")

    scenarios = summary["scenarios"]
    assert len(scenarios) == 1
    result = scenarios[0]

    # ran to a definite verdict
    assert (
        result["status"] in CLEAR_STATUSES
    ), f"{path.name}: scenario status {result['status']!r} is not a clear pass/fail"
    assert result["steps"], f"{path.name}: produced no step results"

    # no step errored out / swallowed an exception
    errored = [s for s in result["steps"] if s["status"] not in CLEAR_STATUSES]
    assert not errored, f"{path.name}: steps did not produce a clear result: " + ", ".join(
        f"{s['step_id']}={s['status']} ({s.get('error')})" for s in errored
    )
