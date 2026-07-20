#!/usr/bin/env python3
"""Build and start PayProbe's showcase network — the 5-minute demo.

Stands up a small but complete simulated acquiring network entirely through the
**public REST APIs** (so this script doubles as a worked integration example):

    driver scenario ──0200──▶  switch (participant flow)
                                   │  Call → "showcase-issuers" group
                                   ▼
                        issuer A / B / C  (3 instances of one issuer flow)
                        + a payShield HSM simulator alongside

It creates the connections, the issuer + switch participant flows, the issuer
group, a payShield saved-simulator, a driver scenario, and a **network** wiring
them together — then starts the network and prints its health. Idempotent:
re-running updates the same named artifacts.

Usage:
    python scripts/showcase.py                       # build + start
    python scripts/showcase.py --build-only          # create artifacts, don't start
    python scripts/showcase.py --teardown            # stop the run, delete artifacts
    python scripts/showcase.py \
        --scenario-url http://localhost:8000 \
        --run-url http://localhost:8100 --token "$PP_TOKEN"

Auth: both services fail closed when a credential is configured. Pass --token
(a bearer the services accept) or set PP_TOKEN; omit in a dev stack
(PAYPROBE_ENV=dev).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
import json as _json
from pathlib import Path

# the demo documents live once in the orchestrator package (shared with the
# portal's one-click install + the guard test); put packages/ on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))
from orchestrator.api import showcase_spec as spec  # noqa: E402

# names kept for readability + back-compat with the guard test's re-exports
NET_ID = spec.NET_ID
ISSUER_FLOW = spec.ISSUER_FLOW
SWITCH_FLOW = spec.SWITCH_FLOW
GROUP_ID = spec.GROUP_ID
CONNS = spec.CONNS
_issuer_flow = spec.issuer_flow
_switch_flow = spec.switch_flow
_showcase_environment = spec.showcase_environment
_driver_scenario = spec.driver_scenario
_network_doc = spec.network_doc

SCENARIO_URL = os.environ.get("SCENARIO_API_URL", "http://localhost:8000")
RUN_URL = os.environ.get("RUN_API_URL", "http://localhost:8100")

class Client:
    def __init__(self, base: str, token: str | None) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def __call__(self, method: str, path: str, body: dict | None = None):
        data = _json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode()
                return _json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise SystemExit(f"{method} {path} → HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"cannot reach {self.base} ({exc}) — is the service up? "
                "start the stack with infra/docker or run the services locally.")


def build(sc: Client, run: Client) -> None:
    print("· connections")
    for name, cfg in CONNS.items():
        sc("PUT", f"/connections/{name}", cfg)

    print("· participant flows (issuer, switch)")
    # PUT /participant-flows/{id} creates-or-replaces at a stable id (idempotent).
    sc("PUT", f"/participant-flows/{ISSUER_FLOW}", _issuer_flow())
    sc("PUT", f"/participant-flows/{SWITCH_FLOW}", _switch_flow())

    print("· issuer group (fleet)")
    _upsert(sc, "/participant-groups", GROUP_ID, spec.issuer_group())

    print("· showcase environment (live selector)")
    sc("PUT", f"/environments/{spec.ENV_NAME}", _showcase_environment())

    print("· payShield HSM simulator (saved)")
    sim_id = _upsert_simulator(run, spec.SIM_LABEL, spec.simulator_config())

    print("· driver scenario")
    driver_id = _upsert_scenario(sc, spec.DRIVER_NAME, _driver_scenario())

    print("· network")
    _upsert(sc, "/network-flows", NET_ID, _network_doc(driver_id, sim_id))

    plan = sc("GET", f"/network-flows/{NET_ID}/plan")
    order = " → ".join(p["node_id"] for p in plan.get("participants", []))
    print(f"  plan: sims={[s['simulator_id'] for s in plan.get('simulators', [])]}"
          f"  start-order: {order}  initiators="
          f"{[i['scenario_id'] for i in plan.get('initiators', [])]}")
    if plan.get("warnings"):
        print("  warnings:", plan["warnings"])


def start(run: Client) -> None:
    print("· starting the network …")
    started = run("POST", f"/network-flows/{NET_ID}/start")
    rid = started["id"]
    h = started.get("health", {})
    print(f"  run {rid}  health {h.get('live')}/{h.get('total')} "
          f"ready={h.get('ready')}")
    for ep in started.get("participants", []):
        print(f"    ↳ {ep.get('flow_id')} @ {ep.get('endpoint', ':' + str(ep.get('port')))}")
    if started.get("initiator_runs"):
        print(f"  driver run(s): {started['initiator_runs']}")
    print("\nOpen the portal → Simulated Network → Networks to watch it live, "
          "or Network Trace (resume capture first) to see a transaction stitched\n"
          "across switch → issuer → back.")


def certify(sc: Client, run: Client) -> None:
    """The thesis payoff: drive load through the network, storm the payShield
    simulator, and score a resilience certificate. Needs the network running
    (start it first) and its payShield sim live + chaos-capable."""
    sim = next((s for s in (run("GET", "/simulator-configs") or [])
                if s.get("label") == "Showcase payShield"), None)
    if not sim:
        raise SystemExit("payShield simulator not found — run the build first.")
    sim_id = sim["id"]

    driver = next((s for s in (sc("GET", "/scenarios") or [])
                   if s.get("name") == "Showcase driver"), None)
    if not driver:
        raise SystemExit("driver scenario not found — run the build first.")

    print("· starting a load run against the switch (steady, 45s) …")
    load = run("POST", "/load-runs", {
        "scenario_ids": [driver["id"]],
        "environment": {"adapters": {}},   # driver step selects its connection
        "type": "steady", "target_tps": 25, "duration_s": 45, "workers": 1,
        "label": "load:showcase"})
    load_id = load.get("id") or load.get("run_id")
    print(f"  load run {load_id}")
    time.sleep(3)  # let the load run register with the coordinator

    print("· resilience run: 10s calm → 15s payShield outage → 15s recovery …")
    res = run("POST", "/resilience/runs", {
        "target_sim_id": sim_id,
        "load_run_id": load_id,
        "baseline_s": 10, "recovery_s": 15,
        "storm": {"phases": [
            {"duration_s": 15, "chaos": {"drop_pct": 100}, "label": "outage"},
        ]},
        "label": "showcase resilience"})
    rid = res["id"]

    print("  scoring", end="", flush=True)
    report = None
    for _ in range(60):
        time.sleep(3)
        view = run("GET", f"/resilience/runs/{rid}")
        if view.get("status") != "running":
            report = view.get("report")
            break
        print(".", end="", flush=True)
    print()

    if not report:
        print("  resilience run did not finish in time — check "
              f"GET /resilience/runs/{rid}")
        return
    print(f"\n  RESILIENCE CERTIFICATE — grade {report.get('grade', '?')}  "
          f"score {report.get('score', '?')}/100  "
          f"{report.get('verdict', '?')}")
    for g in report.get("gates", []):
        mark = "PASS" if g.get("passed") else "FAIL"
        print(f"    [{mark}] {g.get('label', g.get('id'))}: "
              f"{g.get('value')} (threshold {g.get('threshold')})")
    print(f"\n  Full certificate: portal → Resilience, or "
          f"GET /resilience/runs/{rid}")


def teardown(sc: Client, run: Client) -> None:
    print("· stopping any running showcase network")
    for t in run("GET", "/topology-runs") or []:
        if t.get("topology_id") == NET_ID:
            run("DELETE", f"/topology-runs/{t['id']}")
            print(f"  stopped run {t['id']}")
    print("· deleting artifacts")
    _delete(sc, f"/network-flows/{NET_ID}")
    for s in sc("GET", "/scenarios") or []:
        if s.get("name") == "Showcase driver":
            _delete(sc, f"/scenarios/{s['id']}")
    for s in run("GET", "/simulator-configs") or []:
        if s.get("label") == "Showcase payShield":
            _delete(run, f"/simulator-configs/{s['id']}")
    _delete(sc, f"/participant-groups/{GROUP_ID}")
    _delete(sc, f"/participant-flows/{SWITCH_FLOW}")
    _delete(sc, f"/participant-flows/{ISSUER_FLOW}")
    for name in CONNS:
        _delete(sc, f"/connections/{name}")
    print("done — showcase removed.")


# -- small idempotent helpers --------------------------------------------------

def _exists(c: Client, path: str) -> bool:
    try:
        c("GET", path)
        return True
    except SystemExit:
        return False


def _upsert(c: Client, collection: str, key: str, body: dict) -> None:
    # collections that support PUT /{collection}/{id}
    c("PUT", f"{collection}/{key}", body)


def _upsert_scenario(c: Client, name: str, draft: dict) -> str:
    """Scenario ids are server-assigned (unlike flows/networks/groups), so we
    find the demo scenario by name and replace it, else create. Returns the id."""
    existing = next((s for s in (c("GET", "/scenarios") or [])
                     if s.get("name") == name), None)
    if existing:
        sid = existing["id"]
        c("PUT", f"/scenarios/{sid}", {"scenario": draft, "comment": "showcase"})
        return sid
    return c("POST", "/scenarios", {"scenario": draft, "comment": "showcase"})["id"]


def _upsert_simulator(run: Client, label: str, body: dict) -> str:
    """Saved-simulator ids are the slug of their label (server-side); find by
    label and update, else create. Returns the saved id."""
    existing = next((s for s in (run("GET", "/simulator-configs") or [])
                     if s.get("label") == label), None)
    if existing:
        sid = existing["id"]
        run("PUT", f"/simulator-configs/{sid}", {"config": body["config"]})
        return sid
    return run("POST", "/simulator-configs", body)["id"]


def _delete(c: Client, path: str) -> None:
    try:
        c("DELETE", path)
    except SystemExit:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario-url", default=SCENARIO_URL)
    ap.add_argument("--run-url", default=RUN_URL)
    ap.add_argument("--token", default=os.environ.get("PP_TOKEN"))
    ap.add_argument("--build-only", action="store_true",
                    help="create artifacts but don't start the network")
    ap.add_argument("--certify", action="store_true",
                    help="after starting, run a load + chaos storm and score a "
                         "resilience certificate")
    ap.add_argument("--teardown", action="store_true",
                    help="stop the run and delete every showcase artifact")
    args = ap.parse_args()

    sc = Client(args.scenario_url, args.token)
    run = Client(args.run_url, args.token)

    if args.teardown:
        teardown(sc, run)
        return 0

    print("PayProbe showcase — building the demo acquiring network\n")
    build(sc, run)
    if args.build_only:
        print("\nbuilt (not started). Start it from the portal, or re-run "
              "without --build-only.")
        return 0
    start(run)
    if args.certify:
        print()
        certify(sc, run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
