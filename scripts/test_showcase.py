"""Guard the showcase demo (scripts/showcase.py).

The showcase is a headline asset — the one-command "PayProbe in five minutes".
It talks to the live REST APIs by hand-written path/body, so a renamed field or
endpoint in a service would break it *silently* (nothing imports it). These
tests lock two things without any network:

1. every document it generates parses + validates against the REAL models;
2. build()/certify() emit the exact API calls, methods and body shapes the
   services expose (the field names verified by hand when it was written).

Run via ``make test`` (the Makefile adds ../scripts to the suite) or directly:
``PYTHONPATH=packages python -m pytest scripts/test_showcase.py``.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
# the real models live in the scenario-service package
sys.path.insert(0, str(_ROOT / "packages" / "scenario-service"))
sys.path.insert(0, str(_ROOT / "packages"))

_spec = importlib.util.spec_from_file_location(
    "showcase", Path(__file__).resolve().parent / "showcase.py")
showcase = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(showcase)


class FakeClient:
    """Records every (method, path, body) and answers from a canned table so
    build()/certify() run offline. ``routes`` maps (method, path) → response;
    a ``GET`` with no route returns []."""

    def __init__(self, routes: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.routes = routes or {}

    def __call__(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if (method, path) in self.routes:
            return self.routes[(method, path)]
        if method == "GET":
            return []
        # writes echo a plausible object (id derived from the path tail)
        return {"id": path.rsplit("/", 1)[-1], "name": (body or {}).get("name"),
                "run_id": "run-x", "version": 1}

    def paths(self, method: str) -> list[str]:
        return [p for m, p, _ in self.calls if m == method]

    def body(self, method: str, path: str) -> dict | None:
        for m, p, b in self.calls:
            if m == method and p == path:
                return b
        return None


# -- 1. documents validate against the real models --------------------------------

def test_participant_flows_parse():
    from models.participant_flow import ParticipantFlowDraft
    ParticipantFlowDraft(**showcase._issuer_flow())
    ParticipantFlowDraft(**showcase._switch_flow())


def test_driver_scenario_is_valid_and_targets_the_switch():
    from models.scenario import ScenarioDraft, validate_scenario
    d = ScenarioDraft(**showcase._driver_scenario())
    assert validate_scenario(d).valid
    # the reachability fix: the step selects an explicit switch connection
    assert d.steps[0].config["connection"] == "showcase_switch_out"


def test_network_is_valid_and_plans_callees_first():
    from models.network_flow import (
        NetworkFlowDraft, validate_network_flow, plan_network_flow)
    d = NetworkFlowDraft(**showcase._network_doc("scn-driver", "showcase-payshield"))
    assert validate_network_flow(d).valid
    plan = plan_network_flow(d)
    order = [p["node_id"] for p in plan.participants]
    assert order.index("issuers") < order.index("switch")     # callee first
    assert plan.simulators[0]["simulator_id"] == "showcase-payshield"
    assert plan.initiators[0]["scenario_id"] == "scn-driver"
    # the driver autostarts in the LIVE selector env, not mock, so it dials
    # the real switch (a mock run has no response_code to assert on)
    assert plan.initiators[0]["environment"] == "showcase"
    assert not plan.warnings


def test_switch_out_connection_targets_the_switch_port():
    # the driver dials the switch's inbound port, not a type default
    out = showcase.CONNS["showcase_switch_out"]
    inn = showcase.CONNS["showcase_switch_in"]
    assert out["mode"] == "outbound" and out["port"] == inn["port"]


# -- 2. build() emits the verified API calls ---------------------------------------

def test_build_creates_every_artifact_and_wires_resolved_ids():
    sc = FakeClient(routes={
        ("POST", "/scenarios"): {"id": "scn-driver", "name": "Showcase driver"},
    })
    sc.routes[("GET", f"/network-flows/{showcase.NET_ID}/plan")] = {
        "participants": [{"node_id": "issuers", "flow_id": showcase.ISSUER_FLOW},
                         {"node_id": "switch", "flow_id": showcase.SWITCH_FLOW}],
        "simulators": [{"simulator_id": "showcase-payshield"}],
        "initiators": [{"scenario_id": "scn-driver"}], "warnings": []}
    run = FakeClient(routes={
        ("POST", "/simulator-configs"): {"id": "showcase-payshield"},
    })
    showcase.build(sc, run)

    # connections (PUT by chosen id)
    for name in showcase.CONNS:
        assert ("PUT", f"/connections/{name}", showcase.CONNS[name]) in sc.calls
    # participant flows + group + network by chosen id
    assert f"/participant-flows/{showcase.ISSUER_FLOW}" in sc.paths("PUT")
    assert f"/participant-flows/{showcase.SWITCH_FLOW}" in sc.paths("PUT")
    assert f"/participant-groups/{showcase.GROUP_ID}" in sc.paths("PUT")
    assert f"/network-flows/{showcase.NET_ID}" in sc.paths("PUT")
    # simulator + scenario resolved by name → server-assigned id created
    assert "/simulator-configs" in run.paths("POST")
    assert "/scenarios" in sc.paths("POST")

    # the saved network references the RESOLVED driver + sim ids (not constants)
    net = sc.body("PUT", f"/network-flows/{showcase.NET_ID}")
    cfg = {n["id"]: n["config"] for n in net["nodes"]}
    assert cfg["driver"]["scenario_id"] == "scn-driver"
    assert cfg["hsm"]["simulator_id"] == "showcase-payshield"


# -- 3. certify() emits the verified load + resilience calls -----------------------

def test_certify_runs_load_then_resilience_and_reads_the_report(monkeypatch):
    monkeypatch.setattr(showcase.time, "sleep", lambda *_: None)
    sc = FakeClient(routes={
        ("GET", "/scenarios"): [{"id": "scn-driver", "name": "Showcase driver"}],
    })
    report = {"grade": "B", "score": 82.0, "verdict": "PASS",
              "gates": [{"id": "availability", "label": "held", "value": 0.7,
                         "threshold": 0.6, "passed": True}]}
    run = FakeClient(routes={
        ("GET", "/simulator-configs"): [
            {"id": "showcase-payshield", "label": "Showcase payShield"}],
        ("POST", "/load-runs"): {"run_id": "lr-1", "status": "running"},
        ("POST", "/resilience/runs"): {"id": "res-1", "status": "running"},
        ("GET", "/resilience/runs/res-1"): {"status": "done", "report": report},
    })

    showcase.certify(sc, run)

    load = run.body("POST", "/load-runs")
    assert load["scenario_ids"] == ["scn-driver"] and load["type"] == "steady"
    res = run.body("POST", "/resilience/runs")
    assert res["target_sim_id"] == "showcase-payshield"
    assert res["load_run_id"] == "lr-1"                       # /load-runs → run_id
    assert res["storm"]["phases"][0]["chaos"] == {"drop_pct": 100}
    assert ("GET", "/resilience/runs/res-1", None) in run.calls


def test_certify_aborts_cleanly_without_the_payshield_sim():
    sc = FakeClient()
    run = FakeClient()   # GET /simulator-configs → []
    with pytest.raises(SystemExit):
        showcase.certify(sc, run)


def test_switch_flow_relays_issuer_response_code():
    # the reply must map the call's decoded response_code, NOT a literal
    # "${call.response.de.39}" (which is not a resolvable path and lands on the
    # wire verbatim, corrupting DE39/DE41).
    reply = next(n for n in showcase._switch_flow()["nodes"] if n["id"] == "reply")
    assert reply["payload"]["set"]["39"] == "${call.response.response_code}"


def test_showcase_environment_is_a_live_selector():
    env = showcase.spec.showcase_environment()
    assert env["name"] == "showcase"
    assert env.get("mode") != "mock"            # live: not the mock adapter
    assert env["adapters"] == {}                # selector: each step uses its connection
