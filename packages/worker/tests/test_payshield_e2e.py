"""End-to-end: run the bundled payShield example scenario through WorkerEngine.

Exercises the full run path (phase-1 health check + phase-3 e2e scenario) the
orchestrator uses, against a live in-process simulator — proving the example
scenario, the payShield client adapter, and the simulator interoperate and that
step-to-step response chaining (CVK -> CVV -> verify) works.
"""

import json
from pathlib import Path

from worker.adapters.hsm.payshield import PayShieldSimulator
from worker.engine.engine import WorkerEngine

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


async def test_payshield_smoke_scenario_runs_green():
    scenario = json.loads((EXAMPLES / "scenarios" / "payshield_hsm_smoke.json").read_text())
    env = json.loads((EXAMPLES / "environments" / "payshield-sim.json").read_text())

    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    # The bundled env is a selector (its HSM endpoint moved to the
    # `payshield_hsm` connection, attached by the orchestrator at run-build).
    # The worker-only test wires the equivalent inline adapter itself.
    env.setdefault("adapters", {})["hsm"] = {
        "adapter": "payshield",
        "host": "127.0.0.1",
        "port": port,
    }
    try:
        engine = WorkerEngine(env)
        summary = await engine.run_scenario_batch([scenario], run_id="hsm-e2e-test")
    finally:
        await sim.stop()

    assert summary["status"] == "passed"
    assert summary["phases"]["phase_1"]["status"] == "passed"
    assert summary["phases"]["phase_3"]["status"] == "passed"

    (sc,) = summary["scenarios"]
    assert sc["status"] == "passed"
    assert [s["status"] for s in sc["steps"]] == ["passed"] * 4
