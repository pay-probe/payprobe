#!/usr/bin/env python3
"""NATS resilience showcase — the messaging-fabric thesis in one command (ADR-0006).

Stands up a NATS request/reply issuer and drives it under load while a chaos
storm knocks the responder out, then scores a resilience certificate — entirely
through the **public REST APIs**, so this doubles as a worked NATS integration
example:

    driver scenario ──request pay.auth──▶  NATS broker  ──▶  NATS issuer responder
              ▲                                                    (queue group
              └──────────────── APPROVE / DECLINE ◀───────────────  "issuers")

It creates a NATS outbound connection (the driver), a saved **NATS responder
simulator** (subscribes to ``pay.auth`` in a queue group, approves under a
limit), a driver scenario that issues request/reply, and — optionally — a NATS
server registry entry so the Cluster-health / Streams pages know the broker.
Then it starts the responder and, with ``--certify``, runs a load + chaos storm
and prints a pass/fail resilience certificate.

Prerequisites: the NATS cluster must be up (``infra/docker/docker-compose.yml``
brings up nats-1/2/3) and reachable from the orchestrator, and the orchestrator
image must have ``nats-py`` installed. Point ``--nats-host`` at the broker
(default ``nats-1``; use ``localhost`` when running the services on the host).

Usage:
    python scripts/nats_showcase.py                      # build + start responder
    python scripts/nats_showcase.py --certify            # + load + storm + cert
    python scripts/nats_showcase.py --build-only         # create artifacts only
    python scripts/nats_showcase.py --teardown           # remove everything
    python scripts/nats_showcase.py --nats-host localhost --token "$PP_TOKEN"

Real-node-kill variant (beyond the simulator chaos this script injects): while a
``--certify`` load run is in flight, ``docker stop nats-2`` and watch the
queue-group / JetStream-replica failover absorb it — the same resilience scorer
grades the recovery.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import time
import urllib.error
import urllib.request

SCENARIO_URL = os.environ.get("SCENARIO_API_URL", "http://localhost:8000")
RUN_URL = os.environ.get("RUN_API_URL", "http://localhost:8100")

DRIVER_CONN = "nats-driver"
SIM_LABEL = "NATS issuer"
DRIVER_NAME = "NATS showcase driver"
CLUSTER_NAME = "payprobe-cluster"
SUBJECT = "pay.auth"
QUEUE_GROUP = "issuers"


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


# -- artifact specs ------------------------------------------------------------

def driver_connection(nats_host: str, nats_port: int) -> dict:
    """Outbound NATS connection the driver scenario requests on."""
    return {
        "adapter": "nats", "protocol": "nats", "mode": "outbound",
        "host": nats_host, "port": nats_port,
        "subject": SUBJECT, "codec": "json", "request_timeout_sec": 2.0,
        "description": "NATS showcase driver (request/reply on pay.auth)",
    }


def simulator_config(nats_host: str, nats_port: int) -> dict:
    """Saved NATS responder simulator: subscribes to pay.auth in a queue group,
    approves under a limit, declines over it. Chaos-capable (storm target)."""
    return {
        "label": SIM_LABEL,
        "config": {
            "protocol": "nats",
            "host": nats_host, "port": nats_port,
            "subjects": [SUBJECT], "queue_group": QUEUE_GROUP, "codec": "json",
            "rules": [
                {"when": {"body": {"amount": {"gte": 100000}}},
                 "respond": {"json": {"decision": "DECLINE", "reason": "limit"}}},
            ],
            "default": {"json": {"decision": "APPROVE"}},
        },
    }


def driver_scenario() -> dict:
    """One step: request/reply against the NATS driver connection, asserting the
    issuer approves."""
    return {
        "name": DRIVER_NAME,
        "steps": [{
            "name": "authorize", "target": DRIVER_CONN, "action": "request",
            "payload": {"pan": "4111111111111111", "amount": 4200,
                        "correlation_id": "${seq.stan}"},
            "assertions": [{"field": "data.decision", "operator": "eq",
                            "expected": "APPROVE"}],
        }],
    }


def cluster_registry(nats_host: str, nats_port: int) -> dict:
    """A NATS server registry entry so the Cluster-health / Streams pages know
    which broker to probe/manage."""
    if nats_host in ("nats-1", "nats-2", "nats-3"):
        servers = ["nats://nats-1:4222", "nats://nats-2:4222", "nats://nats-3:4222"]
        monitoring = ["http://nats-1:8222", "http://nats-2:8222", "http://nats-3:8222"]
    else:
        servers = [f"nats://{nats_host}:{nats_port}"]
        monitoring = [f"http://{nats_host}:8222"]
    return {"name": "PayProbe cluster", "servers": servers,
            "monitoring": monitoring, "description": "NATS showcase cluster"}


# -- build / start / certify ---------------------------------------------------

def build(sc: Client, run: Client, nats_host: str, nats_port: int) -> str:
    print("· NATS driver connection")
    sc("PUT", f"/connections/{DRIVER_CONN}", driver_connection(nats_host, nats_port))

    print("· NATS server registry entry")
    sc("PUT", f"/nats-servers/{CLUSTER_NAME}", cluster_registry(nats_host, nats_port))

    print("· NATS issuer responder (saved simulator, queue group)")
    sim_id = _upsert_simulator(run, SIM_LABEL, simulator_config(nats_host, nats_port))

    print("· driver scenario (request/reply)")
    _upsert_scenario(sc, DRIVER_NAME, driver_scenario())
    print(f"  built. subject={SUBJECT!r} queue_group={QUEUE_GROUP!r} sim={sim_id}")
    return sim_id


def start(run: Client, sim_id: str) -> None:
    print("· starting the NATS issuer responder …")
    run("POST", f"/simulator-configs/{sim_id}/start")
    print("  responder subscribing on the broker. Point a client at the driver\n"
          "  connection, or run with --certify to drive load + a chaos storm.")


def certify(sc: Client, run: Client, sim_id: str) -> int:
    driver = next((s for s in (sc("GET", "/scenarios") or [])
                   if s.get("name") == DRIVER_NAME), None)
    if not driver:
        raise SystemExit("driver scenario not found — run the build first.")

    print("· load run: request/reply through the queue group (steady, 45s) …")
    load = run("POST", "/load-runs", {
        "scenario_ids": [driver["id"]],
        "environment": {"adapters": {}},   # the step selects its NATS connection
        "type": "steady", "target_tps": 25, "duration_s": 45, "workers": 1,
        "label": "load:nats-showcase"})
    load_id = load.get("id") or load.get("run_id")
    print(f"  load run {load_id}")
    time.sleep(3)

    print("· resilience run: 10s calm → 15s responder outage → 15s recovery …")
    res = run("POST", "/resilience/runs", {
        "target_sim_id": sim_id,
        "load_run_id": load_id,
        "baseline_s": 10, "recovery_s": 15,
        "storm": {"phases": [
            {"duration_s": 15, "chaos": {"drop_pct": 100}, "label": "outage"},
        ]},
        "label": "NATS showcase resilience"})
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
        raise SystemExit("resilience run did not finish in time.")
    grade = report.get("grade") or report.get("verdict") or "?"
    print(f"\n  resilience certificate: grade={grade}  "
          f"pass={report.get('passed')}")
    for k in ("availability", "absorption", "recovery", "latency"):
        if k in report:
            print(f"    {k:12} {report[k]}")
    return 0


def teardown(sc: Client, run: Client) -> None:
    print("· tearing down the NATS showcase …")
    for s in run("GET", "/simulator-configs") or []:
        if s.get("label") == SIM_LABEL:
            try:
                run("POST", f"/simulators/{s['id']}/stop")
            except SystemExit:
                pass
            _delete(run, f"/simulator-configs/{s['id']}")
    for s in sc("GET", "/scenarios") or []:
        if s.get("name") == DRIVER_NAME:
            _delete(sc, f"/scenarios/{s['id']}")
    _delete(sc, f"/connections/{DRIVER_CONN}")
    _delete(sc, f"/nats-servers/{CLUSTER_NAME}")
    print("done — NATS showcase removed.")


# -- idempotent helpers --------------------------------------------------------

def _upsert_scenario(c: Client, name: str, draft: dict) -> str:
    existing = next((s for s in (c("GET", "/scenarios") or [])
                     if s.get("name") == name), None)
    if existing:
        sid = existing["id"]
        c("PUT", f"/scenarios/{sid}", {"scenario": draft, "comment": "nats-showcase"})
        return sid
    return c("POST", "/scenarios", {"scenario": draft, "comment": "nats-showcase"})["id"]


def _upsert_simulator(run: Client, label: str, body: dict) -> str:
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
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario-url", default=SCENARIO_URL)
    ap.add_argument("--run-url", default=RUN_URL)
    ap.add_argument("--token", default=os.environ.get("PP_TOKEN"))
    ap.add_argument("--nats-host", default=os.environ.get("NATS_HOST", "nats-1"),
                    help="broker host reachable from the orchestrator (default nats-1; "
                         "use localhost when services run on the host)")
    ap.add_argument("--nats-port", type=int, default=int(os.environ.get("NATS_PORT", 4222)))
    ap.add_argument("--build-only", action="store_true",
                    help="create artifacts but don't start the responder")
    ap.add_argument("--certify", action="store_true",
                    help="after starting, run a load + chaos storm and score a "
                         "resilience certificate")
    ap.add_argument("--teardown", action="store_true",
                    help="stop the responder and delete every showcase artifact")
    args = ap.parse_args()

    sc = Client(args.scenario_url, args.token)
    run = Client(args.run_url, args.token)

    if args.teardown:
        teardown(sc, run)
        return 0

    sim_id = build(sc, run, args.nats_host, args.nats_port)
    if args.build_only:
        print("\nbuild-only: artifacts created, responder not started.")
        return 0
    start(run, sim_id)
    if args.certify:
        return certify(sc, run, sim_id)
    print("\nTip: re-run with --certify to drive load + a chaos storm and score a "
          "resilience certificate,\nor open the portal → NATS to watch cluster "
          "health and manage JetStream streams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
