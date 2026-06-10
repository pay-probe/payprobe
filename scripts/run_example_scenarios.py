#!/usr/bin/env python3
"""
Run example scenarios against a live PayProbe environment.
Used in CI to verify the mock integration end-to-end.

Usage:
    python scripts/run_example_scenarios.py --env mock --suite examples/scenarios/
"""
import argparse
import json
import sys
import time
from pathlib import Path
try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)

BASE_URL = "http://localhost/api"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="mock")
    parser.add_argument("--suite", default="examples/scenarios/")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=60)

    # Load scenario files
    scenario_dir = Path(args.suite)
    scenario_files = list(scenario_dir.glob("*.json"))
    print(f"Found {len(scenario_files)} scenarios")

    # Trigger run
    payload = {
        "environment": args.env,
        "scenarios": [json.loads(f.read_text()) for f in scenario_files],
    }
    resp = client.post("/runs", json=payload)
    if resp.status_code != 201:
        print(f"Failed to create run: {resp.status_code} {resp.text}")
        sys.exit(1)

    run_id = resp.json()["id"]
    print(f"Run started: {run_id}")

    # Poll for completion
    for _ in range(60):
        time.sleep(2)
        resp = client.get(f"/runs/{run_id}")
        run = resp.json()
        status = run.get("status")
        print(f"  Status: {status}")
        if status in ("completed", "failed", "cancelled"):
            break

    # Print summary
    phases = run.get("phases", {})
    print("\nResults:")
    for phase, data in phases.items():
        icon = "✓" if data.get("status") == "passed" else "✗"
        print(f"  {icon} {phase}: {data}")

    if run.get("status") != "completed":
        print("\nRun did not complete successfully")
        sys.exit(1)

    regressions = run.get("regressions", [])
    if regressions:
        print(f"\n{len(regressions)} regression(s) detected:")
        for r in regressions:
            print(f"  - {r}")
        sys.exit(1)

    print("\nAll scenarios passed.")


if __name__ == "__main__":
    main()
