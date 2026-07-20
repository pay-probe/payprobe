"""Run the worker engine against a mock environment from the CLI.

    cd packages && python -m worker            # uses bundled mock env + examples
    cd packages && python -m worker <env.json> <scenario.json> [...]

Pure mock execution — no real systems, no Redis, no Postgres required.
"""

import asyncio
import json
import sys
from pathlib import Path

try:
    import uvloop  # noqa

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:  # uvloop optional for local/dev
    pass

from worker.engine import WorkerEngine, InMemorySink, FAILED

REPO = Path(__file__).resolve().parents[2]


def _load(paths: list[str]):
    if paths:
        env = json.loads(Path(paths[0]).read_text())
        scenarios = [json.loads(Path(p).read_text()) for p in paths[1:]]
    else:
        env = json.loads((REPO / "examples/environments/mock.json").read_text())
        scenarios = [
            json.loads(p.read_text()) for p in sorted((REPO / "examples/scenarios").glob("*.json"))
        ]
    return env, scenarios


async def _main(argv: list[str]) -> int:
    env, scenarios = _load(argv)
    sink = InMemorySink()
    engine = WorkerEngine(env, sink)
    summary = await engine.run_scenario_batch(scenarios, run_id="cli-run")

    print(f"\nRun status: {summary['status'].upper()}")
    for phase, info in summary["phases"].items():
        print(f"  {phase}: {info}")
    print("\nScenarios:")
    for sc in summary["scenarios"]:
        mark = {"passed": "PASS", "failed": "FAIL", "blocked": "BLOCKED"}.get(
            sc["status"], sc["status"].upper()
        )
        print(f"  [{mark:7}] {sc['name']}")
    return 0 if summary["status"] != FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
