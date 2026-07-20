#!/usr/bin/env python3
"""
Run example scenarios against a live PayProbe environment.
Used in CI to verify the mock integration end-to-end.

The orchestrator's auth gate is fail-closed: when AUTH_JWT_SECRET is configured
(it is, in the docker-compose stack) every request needs a valid HS256 JWT. Pass
one with --token or the PP_TOKEN env var. In CI we mint a short-lived token with
the same shared secret the orchestrator is started with.

Usage:
    python scripts/run_example_scenarios.py \
        --env mock --suite examples/scenarios/ \
        --base-url http://localhost:8100 --token "$PP_TOKEN"
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)

BASE_URL = "http://localhost:8100"
# Run-level statuses that mean the run is still in flight (anything else is terminal).
IN_FLIGHT = {"pending", "running"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="mock", help="bundled environment name")
    parser.add_argument("--suite", default="examples/scenarios/")
    parser.add_argument("--base-url", default=os.environ.get("PP_BASE_URL", BASE_URL))
    parser.add_argument(
        "--token",
        default=os.environ.get("PP_TOKEN", ""),
        help="Bearer token (JWT) for the orchestrator auth gate",
    )
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    client = httpx.Client(base_url=args.base_url, headers=headers, timeout=60)

    # Load scenario files
    scenario_dir = Path(args.suite)
    scenario_files = sorted(scenario_dir.glob("*.json"))
    print(f"Found {len(scenario_files)} scenarios")
    if not scenario_files:
        print(f"No scenarios found in {scenario_dir}")
        sys.exit(1)

    # Trigger run. `environment_name` selects a bundled env (e.g. "mock");
    # scenarios are submitted inline.
    payload = {
        "environment_name": args.env,
        "scenarios": [json.loads(f.read_text()) for f in scenario_files],
    }
    resp = client.post("/runs", json=payload)
    if resp.status_code != 200:
        print(f"Failed to create run: {resp.status_code} {resp.text}")
        sys.exit(1)

    run_id = resp.json()["run_id"]
    print(f"Run started: {run_id}")

    # Poll for completion.
    run: dict = {}
    for _ in range(60):
        time.sleep(2)
        resp = client.get(f"/runs/{run_id}")
        if resp.status_code != 200:
            print(f"Failed to fetch run: {resp.status_code} {resp.text}")
            sys.exit(1)
        run = resp.json()
        status = run.get("status")
        print(f"  Status: {status}")
        if status not in IN_FLIGHT:
            break
    else:
        print("\nRun did not finish within the timeout")
        sys.exit(1)

    # Report. The run detail carries the engine summary: per-phase status and
    # per-scenario verdicts.
    summary = run.get("summary") or {}

    phases = summary.get("phases", {})
    if phases:
        print("\nPhases:")
        for phase, data in phases.items():
            icon = "✓" if data.get("status") == "passed" else "✗"
            print(f"  {icon} {phase}: {data.get('status')}")

    print("\nScenarios:")
    failed = []
    for sc in summary.get("scenarios", []):
        st = sc.get("status")
        icon = "✓" if st == "passed" else "✗"
        print(f"  {icon} {sc.get('name')}: {st}")
        if st != "passed":
            failed.append(sc.get("name"))

    overall = run.get("status")
    if overall != "passed":
        print(f"\nRun finished with status '{overall}'.")
        if failed:
            print(f"Failing scenarios: {', '.join(str(f) for f in failed)}")
        sys.exit(1)

    print("\nAll scenarios passed.")


if __name__ == "__main__":
    main()
