"""End-to-end engine tests against mock adapters.

Run from the packages/ directory:
    cd packages && python -m pytest worker/tests -v
"""

import asyncio

import pytest

from worker.engine import (
    WorkerEngine,
    InMemorySink,
    PASSED,
    FAILED,
    BLOCKED,
    STEP_RESULT,
    PHASE_UPDATE,
    RUN_COMPLETED,
    resolve_value,
    UnresolvedReferenceError,
)

MOCK_ENV = {
    "mode": "mock",
    "connection_budget": {"total": 1000, "adapters": {"http": 400}},
    "adapters": {
        "terminal_sim": {"mock": True, "latency_ms": 0},
        "http": {"mock": True, "latency_ms": 0},
        "db_probe_core": {"mock": True, "latency_ms": 0},
    },
}


def _chip_scenario(test_class="e2e"):
    return {
        "id": "sc-chip",
        "name": "chip_purchase_approved",
        "test_class": test_class,
        "stop_on_failure": True,
        "steps": [
            {
                "id": "step_001",
                "target": "terminal_sim",
                "action": "insert_card",
                "payload": {"card_profile": "visa"},
                "assertions": [{"field": "status", "operator": "eq", "expected": "inserted"}],
            },
            {
                "id": "step_003",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"amount": 10000, "currency": "GEL"},
                "assertions": [
                    {"field": "response_code", "operator": "eq", "expected": "00"},
                    {"field": "auth_code", "operator": "present"},
                ],
            },
            {
                "id": "step_004",
                "target": "db_probe_core",
                "action": "query_transaction",
                "payload": {"rrn": "${step_003.response.rrn}"},
                "assertions": [{"field": "status", "operator": "eq", "expected": "APPROVED"}],
            },
        ],
    }


@pytest.mark.asyncio
async def test_step_trace_fields_populated():
    """Each executed step carries wire detail + a structured engine trace."""
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    summary = await engine.run_scenario_batch([_chip_scenario()], run_id="run-trace")

    step = summary["scenarios"][0]["steps"][0]
    # timing offset for the waterfall
    assert step["started_at_ms"] > 0
    # adapter wire log threaded through
    assert "REQ" in step["raw_log"] and "RES" in step["raw_log"]
    # structured engine log lines: dispatch + response + assertion outcome
    levels = [ln["level"] for ln in step["trace"]]
    assert "info" in levels and "wire" in levels
    assert any(ln["level"] in ("pass", "fail") for ln in step["trace"])


@pytest.mark.asyncio
async def test_environment_override_surfaces_clean_target_on_outcome():
    """When the orchestrator repoints a step at a ``{base}@{env}`` adapter and
    tags ``config._environment_override``, the report shows the clean base
    target plus the override name (and the trace notes it)."""
    env = {
        "mode": "mock",
        "adapters": {
            "http": {"mock": True, "latency_ms": 0},
            "http@staging": {"mock": True, "latency_ms": 0},
        },
    }
    scenario = {
        "id": "sc-ovr",
        "name": "override_flow",
        "test_class": "integration",
        "steps": [
            {
                "id": "s1",
                "target": "http@staging",  # derived adapter actually dispatched
                "action": "send_auth_request",
                "payload": {"amount": 100, "currency": "GEL"},
                "assertions": [],
                "config": {
                    "_environment_override": "staging",
                    "_catalog_target": "http",
                },
            },
        ],
    }
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="run-ovr")
    step = summary["scenarios"][0]["steps"][0]
    assert step["environment_override"] == "staging"
    assert step["target"] == "http"  # cleaned for display, not "http@staging"
    assert step["status"] == PASSED
    assert any(
        ln["level"] == "info" and "environment override" in ln["msg"] for ln in step["trace"]
    )


@pytest.mark.asyncio
async def test_trace_redacts_secrets():
    """Secrets must not leak into raw_log / trace of the stored summary."""
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    scenario = {
        "id": "sc-secret",
        "name": "pin_flow",
        "test_class": "integration",
        "__secrets__": ["1234"],
        "steps": [
            {
                "id": "s1",
                "target": "terminal_sim",
                "action": "enter_pin",
                "payload": {"pin": "1234"},
                "assertions": [],
            },
        ],
    }
    summary = await engine.run_scenario_batch([scenario], run_id="run-secret")
    step = summary["scenarios"][0]["steps"][0]
    assert "1234" not in step["raw_log"]
    assert "1234" not in "".join(ln["msg"] for ln in step["trace"])


@pytest.mark.asyncio
async def test_full_mock_run_passes():
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    summary = await engine.run_scenario_batch([_chip_scenario()], run_id="run-1")

    assert summary["status"] == PASSED
    assert summary["phases"]["phase_1"]["status"] == PASSED
    assert summary["phases"]["phase_3"]["status"] == PASSED
    sc = summary["scenarios"][0]
    assert sc["status"] == PASSED
    assert len(sc["steps"]) == 3
    # events were emitted on the stream
    assert len(sink.of_type(STEP_RESULT)) == 3
    assert len(sink.of_type(PHASE_UPDATE)) == 3
    assert len(sink.of_type(RUN_COMPLETED)) == 1


@pytest.mark.asyncio
async def test_variable_resolution_between_steps():
    """step_004 must receive the rrn produced by step_003's mock response."""
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    summary = await engine.run_scenario_batch([_chip_scenario()], run_id="run-2")

    step4 = next(s for s in summary["scenarios"][0]["steps"] if s["step_id"] == "step_004")
    # mock send_auth_request returns rrn "000000000001"; it must have flowed in
    assert step4["request"]["rrn"] == "000000000001"
    assert step4["status"] == PASSED


@pytest.mark.asyncio
async def test_pool_card_resolves_from_scenario_cards_in_normal_run():
    """${pool.card} draws from the scenario's inline cards (registry-resolved by
    the orchestrator) even in an ordinary, non-load run."""
    sink = InMemorySink()
    engine = WorkerEngine(MOCK_ENV, sink)
    scenario = {
        "id": "sc-pool",
        "name": "pooled_auth",
        "test_class": "integration",
        "cards": ["4111111111111111", "4111111111111129"],
        "terminals": ["TERM0001"],
        "steps": [
            {
                "id": "s1",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"pan": "${pool.card}", "terminal": "${pool.terminal}", "amount": 100},
                "assertions": [],
            },
        ],
    }
    summary = await engine.run_scenario_batch([scenario], run_id="run-pool")
    step = summary["scenarios"][0]["steps"][0]
    assert step["request"]["pan"] in scenario["cards"]
    assert step["request"]["terminal"] == "TERM0001"


@pytest.mark.asyncio
async def test_phase1_health_failure_blocks_dependent_scenario():
    env = {
        "mode": "mock",
        "adapters": {
            "terminal_sim": {"mock": True, "latency_ms": 0},
            "http": {"mock": True, "latency_ms": 0, "force_unhealthy": True},
            "db_probe_core": {"mock": True, "latency_ms": 0},
        },
    }
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([_chip_scenario()], run_id="run-3")

    assert "http" in summary["phases"]["phase_1"]["failed"]
    # scenario touches http -> BLOCKED, not failed
    assert summary["scenarios"][0]["status"] == BLOCKED
    assert summary["status"] in (PASSED, FAILED)  # blocked-only run is not a regression


@pytest.mark.asyncio
async def test_failing_assertion_marks_scenario_failed():
    env = {
        "mode": "mock",
        "adapters": {
            "http": {
                "mock": True,
                "latency_ms": 0,
                "responses": {"send_auth_request": {"response_code": "05"}},
            },
        },
    }
    scenario = {
        "id": "sc-decline",
        "name": "declined",
        "test_class": "e2e",
        "steps": [
            {
                "id": "s1",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"amount": 1},
                "assertions": [{"field": "response_code", "operator": "eq", "expected": "00"}],
            },
        ],
    }
    engine = WorkerEngine(env, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="run-4")

    assert summary["scenarios"][0]["status"] == FAILED
    assert summary["status"] == FAILED


@pytest.mark.asyncio
async def test_iter_run_yields_events_live():
    """The async-generator API streams events and ends after run.completed."""
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    events = [e async for e in engine.iter_run([_chip_scenario()], run_id="run-iter")]

    types = [e.type for e in events]
    assert types[0] == "run.started"
    assert types[-1] == RUN_COMPLETED
    assert "step.result" in types
    # generator stopped exactly at the terminal event
    assert types.count(RUN_COMPLETED) == 1


# -- graph execution: branching & loops --------------------------------------


def _steps_by_id(summary, scenario_index=0):
    out = {}
    for s in summary["scenarios"][scenario_index]["steps"]:
        out.setdefault(s["step_id"], []).append(s)
    return out


@pytest.mark.asyncio
async def test_graph_linear_via_edges_matches_list_order():
    """Explicit edges chaining actions run the same as a linear list."""
    scenario = {
        "id": "sc-g1",
        "name": "graph_linear",
        "test_class": "e2e",
        "steps": [
            {
                "id": "a",
                "target": "terminal_sim",
                "action": "insert_card",
                "payload": {"card_profile": "visa"},
                "assertions": [],
            },
            {
                "id": "b",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"amount": 100},
                "assertions": [],
            },
        ],
        "edges": [{"source": "a", "source_port": "out", "target": "b"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rg1")
    ids = [s["step_id"] for s in summary["scenarios"][0]["steps"]]
    assert ids == ["a", "b"]
    assert summary["scenarios"][0]["status"] == PASSED


@pytest.mark.asyncio
async def test_graph_if_takes_true_branch():
    """An if node routes to its true port when the condition holds."""
    scenario = {
        "id": "sc-if",
        "name": "graph_if",
        "test_class": "e2e",
        "steps": [
            {
                "id": "auth",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"amount": 100},
                "assertions": [],
            },
            {
                "id": "gate",
                "kind": "if",
                "config": {
                    "left": "${auth.response.response_code}",
                    "operator": "eq",
                    "right": "00",
                },
            },
            {
                "id": "ok",
                "target": "db_probe_core",
                "action": "query_transaction",
                "payload": {"rrn": "${auth.response.rrn}"},
                "assertions": [],
            },
            {
                "id": "bad",
                "target": "db_probe_core",
                "action": "query_balance",
                "payload": {"account_id": "x"},
                "assertions": [],
            },
        ],
        "edges": [
            {"source": "auth", "source_port": "out", "target": "gate"},
            {"source": "gate", "source_port": "true", "target": "ok"},
            {"source": "gate", "source_port": "false", "target": "bad"},
        ],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rif")
    by_id = _steps_by_id(summary)
    assert "ok" in by_id and "bad" not in by_id
    assert by_id["gate"][0]["response"]["branch"] == "true"


@pytest.mark.asyncio
async def test_graph_loop_times_repeats_body():
    """A times loop runs its body node the configured number of times."""
    scenario = {
        "id": "sc-loop",
        "name": "graph_loop",
        "test_class": "e2e",
        "steps": [
            {"id": "lp", "kind": "loop", "config": {"mode": "times", "count": 3}},
            {
                "id": "ping",
                "target": "http",
                "action": "echo_test",
                "payload": {},
                "assertions": [],
            },
            {
                "id": "after",
                "target": "http",
                "action": "echo_test",
                "payload": {},
                "assertions": [],
            },
        ],
        "edges": [
            {"source": "lp", "source_port": "loop", "target": "ping"},
            {"source": "lp", "source_port": "done", "target": "after"},
        ],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rloop")
    by_id = _steps_by_id(summary)
    assert len(by_id["ping"]) == 3
    assert len(by_id["after"]) == 1


@pytest.mark.asyncio
async def test_graph_foreach_exposes_loop_item():
    """A forEach loop iterates a list and exposes ${loop.item} to the body."""
    scenario = {
        "id": "sc-fe",
        "name": "graph_foreach",
        "test_class": "e2e",
        "stop_on_failure": False,
        "steps": [
            {"id": "lp", "kind": "loop", "config": {"mode": "forEach", "list": ["a", "b"]}},
            {
                "id": "body",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"card_profile": "${loop.item}"},
                "assertions": [],
            },
        ],
        "edges": [{"source": "lp", "source_port": "loop", "target": "body"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rfe")
    by_id = _steps_by_id(summary)
    profiles = [s["request"]["card_profile"] for s in by_id["body"]]
    assert profiles == ["a", "b"]


# -- code nodes (custom Python / TypeScript) ---------------------------------


@pytest.mark.asyncio
async def test_code_node_output_flows_downstream():
    """A code node returns an object that a later step references via ${id.response.*}."""
    scenario = {
        "id": "sc-code",
        "name": "graph_code",
        "test_class": "e2e",
        "steps": [
            {
                "id": "calc",
                "kind": "code",
                "config": {"language": "python", "code": 'return {"amount": 7 * 6}'},
            },
            {
                "id": "auth",
                "target": "http",
                "action": "send_auth_request",
                "payload": {"amount": "${calc.response.amount}"},
                "assertions": [],
            },
        ],
        "edges": [{"source": "calc", "source_port": "out", "target": "auth"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rcode")
    by_id = _steps_by_id(summary)
    assert by_id["calc"][0]["status"] == PASSED
    assert by_id["calc"][0]["response"] == {"amount": 42}
    assert by_id["auth"][0]["request"]["amount"] == 42


@pytest.mark.asyncio
async def test_crypto_node_pin_block_roundtrip():
    """A crypto node computes a PIN block; a second decodes it back."""
    key = "0123456789ABCDEFFEDCBA9876543210"
    scenario = {
        "id": "sc-crypto",
        "name": "crypto",
        "test_class": "e2e",
        "steps": [
            {
                "id": "enc",
                "kind": "crypto",
                "config": {
                    "operation": "pin_block_encode",
                    "pin": "1234",
                    "pan": "4111111111111111",
                    "key": key,
                },
            },
            {
                "id": "dec",
                "kind": "crypto",
                "config": {
                    "operation": "pin_block_decode",
                    "pin_block": "${enc.response.pin_block}",
                    "pan": "4111111111111111",
                    "key": key,
                },
            },
        ],
        "edges": [{"source": "enc", "source_port": "out", "target": "dec"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rcrypto")
    by_id = _steps_by_id(summary)
    assert by_id["enc"][0]["status"] == PASSED
    assert by_id["dec"][0]["response"]["pin"] == "1234"


@pytest.mark.asyncio
async def test_debug_hook_pauses_and_resumes():
    """The debug hook can block execution at a node and release it (breakpoint)."""
    gate = asyncio.Event()
    paused_at = []

    async def hook(node_id, ctx):
        if node_id == "a":
            paused_at.append(node_id)
            await gate.wait()

    scenario = {
        "id": "sc-dbg",
        "name": "dbg",
        "test_class": "e2e",
        "steps": [
            {"id": "init", "kind": "init", "config": {"data": "{}"}},
            {
                "id": "a",
                "target": "http",
                "action": "echo_test",
                "payload": {},
                "assertions": [],
            },
        ],
        "edges": [{"source": "init", "source_port": "out", "target": "a"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    task = asyncio.create_task(engine.run_scenario_batch([scenario], "rdbg", debug_hook=hook))
    await asyncio.sleep(0.05)
    assert paused_at == ["a"]  # paused at the breakpoint
    assert not task.done()  # blocked, run not finished
    gate.set()  # resume
    summary = await task
    assert summary["scenarios"][0]["status"] in (PASSED, FAILED)


@pytest.mark.asyncio
async def test_crypto_emv_arqc_chain_from_mdk():
    """MDK → ICC master key → session key → ARQC, wired across crypto nodes."""
    mdk = "0123456789ABCDEFFEDCBA9876543210"
    scenario = {
        "id": "sc-emv",
        "name": "emv",
        "test_class": "e2e",
        "steps": [
            {
                "id": "udk",
                "kind": "crypto",
                "config": {
                    "operation": "emv_icc_mk",
                    "key": mdk,
                    "pan": "4111111111111111",
                    "psn": "00",
                },
            },
            {
                "id": "sk",
                "kind": "crypto",
                "config": {
                    "operation": "emv_session_key",
                    "key": "${udk.response.udk}",
                    "atc": "001C",
                },
            },
            {
                "id": "ac",
                "kind": "crypto",
                "config": {
                    "operation": "arqc",
                    "key": "${sk.response.session_key}",
                    "data": "0000000010000000000000084000000840000008261800",
                },
            },
        ],
        "edges": [
            {"source": "udk", "source_port": "out", "target": "sk"},
            {"source": "sk", "source_port": "out", "target": "ac"},
        ],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="remv")
    by_id = _steps_by_id(summary)
    assert by_id["udk"][0]["status"] == PASSED
    assert len(by_id["sk"][0]["response"]["session_key"]) == 32
    assert len(by_id["ac"][0]["response"]["arqc"]) == 16


@pytest.mark.asyncio
async def test_crypto_emv_arqc_to_arpc_verify_flow():
    """End-to-end issuer flow: ARQC → ARPC (method 1) → ARPC verify (match)."""
    mdk = "0123456789ABCDEFFEDCBA9876543210"
    data = "0000000010000000000000084000000840000008261800"
    scenario = {
        "id": "sc-arpc",
        "name": "arpc",
        "test_class": "e2e",
        "steps": [
            {
                "id": "udk",
                "kind": "crypto",
                "config": {
                    "operation": "emv_icc_mk",
                    "key": mdk,
                    "pan": "4111111111111111",
                    "psn": "00",
                },
            },
            {
                "id": "sk",
                "kind": "crypto",
                "config": {
                    "operation": "emv_session_key",
                    "key": "${udk.response.udk}",
                    "atc": "001C",
                },
            },
            {
                "id": "ac",
                "kind": "crypto",
                "config": {"operation": "arqc", "key": "${sk.response.session_key}", "data": data},
            },
            # issuer generates ARPC from the ARQC + auth response code
            {
                "id": "rc",
                "kind": "crypto",
                "config": {
                    "operation": "arpc",
                    "key": "${sk.response.session_key}",
                    "arqc": "${ac.response.arqc}",
                    "arc": "3030",
                    "arpc_method": "1",
                },
            },
            # card-side verification of that ARPC
            {
                "id": "vfy",
                "kind": "crypto",
                "config": {
                    "operation": "arpc",
                    "key": "${sk.response.session_key}",
                    "arqc": "${ac.response.arqc}",
                    "arc": "3030",
                    "arpc_method": "1",
                    "expected": "${rc.response.arpc}",
                },
            },
        ],
        "edges": [
            {"source": "udk", "source_port": "out", "target": "sk"},
            {"source": "sk", "source_port": "out", "target": "ac"},
            {"source": "ac", "source_port": "out", "target": "rc"},
            {"source": "rc", "source_port": "out", "target": "vfy"},
        ],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rarpc")
    by_id = _steps_by_id(summary)
    assert len(by_id["rc"][0]["response"]["arpc"]) == 16  # method 1 → 8 bytes
    assert by_id["vfy"][0]["response"]["match"] is True


@pytest.mark.asyncio
async def test_edgeless_non_action_nodes_run_without_target_keyerror():
    """A scenario with NO edges containing non-action nodes must still run.

    Regression: the linear (edge-less) path used to call ``_run_step`` for every
    node, which reads ``step['target']`` — crashing any crypto/code node with
    ``KeyError: 'target'`` and erroring the step. Both kinds should now execute.
    """
    scenario = {
        "id": "sc-linear",
        "name": "linear",
        "test_class": "e2e",
        "steps": [
            {
                "id": "calc",
                "kind": "code",
                "config": {"language": "python", "code": "return {'n': 21 * 2}"},
            },
            {
                "id": "enc",
                "kind": "crypto",
                "config": {
                    "operation": "pin_block_encode",
                    "pin": "1234",
                    "pan": "4111111111111111",
                    "key": "0123456789ABCDEFFEDCBA9876543210",
                },
            },
        ],
        # deliberately no "edges" -> exercises the linear path
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rlin")
    by_id = _steps_by_id(summary)
    assert by_id["calc"][0]["status"] == PASSED
    assert by_id["calc"][0]["response"] == {"n": 42}
    assert by_id["enc"][0]["status"] == PASSED
    assert by_id["enc"][0]["response"].get("pin_block")


@pytest.mark.asyncio
async def test_init_node_feeds_downstream():
    """An init node emits a JSON object (with ${vars} resolved) for later steps."""
    scenario = {
        "id": "sc-init",
        "name": "init_flow",
        "test_class": "e2e",
        "variables": {"cur": "USD"},
        "steps": [
            {
                "id": "init",
                "kind": "init",
                "config": {"data": '{"amount": 10000, "currency": "${vars.cur}"}'},
            },
            {
                "id": "auth",
                "target": "http",
                "action": "send_auth_request",
                "payload": {
                    "amount": "${init.response.amount}",
                    "ccy": "${init.response.currency}",
                },
                "assertions": [],
            },
        ],
        "edges": [{"source": "init", "source_port": "out", "target": "auth"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rinit")
    by_id = _steps_by_id(summary)
    assert by_id["init"][0]["response"] == {"amount": 10000, "currency": "USD"}
    assert by_id["auth"][0]["request"]["amount"] == 10000
    assert by_id["auth"][0]["request"]["ccy"] == "USD"


@pytest.mark.asyncio
async def test_call_node_runs_subflow_and_exposes_output():
    """A call node runs an attached sub-scenario; its per-step outputs are
    exposed as ${call.response.<sub_step>.<field>} to the caller."""
    sub = {
        "id": "sub-setup",
        "name": "setup",
        "test_class": "e2e",
        "steps": [
            {"id": "seed", "kind": "init", "config": {"data": '{"token": "ABC", "amount": 5000}'}},
            {"id": "done", "kind": "merge"},
        ],
        "edges": [{"source": "seed", "source_port": "out", "target": "done"}],
    }
    parent = {
        "id": "sc-parent",
        "name": "parent",
        "test_class": "e2e",
        "subflows": {"sub-setup": sub},
        "steps": [
            {"id": "setup", "kind": "call", "config": {"scenario_id": "sub-setup"}},
            {
                "id": "use",
                "kind": "init",
                "config": {"data": '{"echo": "${setup.response.seed.token}"}'},
            },
        ],
        "edges": [{"source": "setup", "source_port": "out", "target": "use"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([parent], run_id="rcall")
    by_id = _steps_by_id(summary)
    assert by_id["setup"][0]["status"] == PASSED
    assert by_id["setup"][0]["response"]["seed"] == {"token": "ABC", "amount": 5000}
    # the caller resolves a value out of the sub-flow's output
    assert by_id["use"][0]["response"] == {"echo": "ABC"}
    # sub-flow internal steps are not surfaced on the parent timeline
    assert "seed" not in by_id


@pytest.mark.asyncio
async def test_call_node_missing_subflow_errors():
    parent = {
        "id": "p",
        "name": "p",
        "test_class": "e2e",
        "steps": [
            {"id": "c", "kind": "call", "config": {"scenario_id": "nope"}},
            {"id": "end", "kind": "merge"},
        ],
        "edges": [{"source": "c", "source_port": "out", "target": "end"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([parent], run_id="rmiss")
    by_id = _steps_by_id(summary)
    assert by_id["c"][0]["status"] == "error"
    assert "not available" in by_id["c"][0]["error"]


@pytest.mark.asyncio
async def test_call_node_depth_guard_stops_recursion():
    """A self-referencing sub-scenario terminates via the depth guard."""
    recursive = {
        "id": "r",
        "name": "r",
        "test_class": "e2e",
        "stop_on_failure": False,
        "steps": [
            {"id": "self", "kind": "call", "config": {"scenario_id": "r"}},
            {"id": "tail", "kind": "merge"},
        ],
        "edges": [{"source": "self", "source_port": "out", "target": "tail"}],
    }
    parent = {
        "id": "p",
        "name": "p",
        "test_class": "e2e",
        "stop_on_failure": False,
        "subflows": {"r": recursive},
        "steps": [
            {"id": "c", "kind": "call", "config": {"scenario_id": "r"}},
            {"id": "end", "kind": "merge"},
        ],
        "edges": [{"source": "c", "source_port": "out", "target": "end"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([parent], run_id="rdepth")
    by_id = _steps_by_id(summary)
    # it returns (no hang) and the deep recursion is reported as a failure
    assert by_id["c"][0]["status"] in (FAILED, "error")


@pytest.mark.asyncio
async def test_code_node_inputs_chain_from_upstream():
    """A code node's `inputs` map resolves ${refs} and is exposed as `inputs`."""
    scenario = {
        "id": "sc-chain",
        "name": "code_chain",
        "test_class": "e2e",
        "steps": [
            {
                "id": "src",
                "kind": "code",
                "config": {"language": "python", "code": "return {'v': 21}"},
            },
            {
                "id": "dbl",
                "kind": "code",
                "config": {
                    "language": "python",
                    "code": "return {'doubled': int(inputs['x']) * 2}",
                    "inputs": [{"name": "x", "value": "${src.response.v}"}],
                },
            },
        ],
        "edges": [{"source": "src", "source_port": "out", "target": "dbl"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rchain")
    by_id = _steps_by_id(summary)
    assert by_id["dbl"][0]["response"] == {"doubled": 42}


@pytest.mark.asyncio
async def test_code_node_error_fails_scenario():
    scenario = {
        "id": "sc-code-err",
        "name": "code_err",
        "test_class": "e2e",
        "steps": [
            {
                "id": "boom",
                "kind": "code",
                "config": {"language": "python", "code": "raise ValueError('nope')"},
            },
        ],
        "edges": [],  # single node; graph path not needed but error must surface
        "stop_on_failure": True,
    }
    # give it an edge to force the graph path
    scenario["steps"].append(
        {
            "id": "after",
            "target": "http",
            "action": "echo_test",
            "payload": {},
            "assertions": [],
        }
    )
    scenario["edges"] = [{"source": "boom", "source_port": "out", "target": "after"}]
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rcodeerr")
    sc = summary["scenarios"][0]
    assert sc["status"] == FAILED
    by_id = _steps_by_id(summary)
    assert "after" not in by_id  # stop_on_failure halted the flow


# -- http nodes --------------------------------------------------------------


@pytest.fixture()
def http_server(monkeypatch):
    """A tiny localhost JSON server; proxies disabled so httpx hits it directly."""
    import json as _json
    import threading
    import http.server
    import socketserver

    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                _json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
            )

    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.mark.asyncio
async def test_http_node_runs_and_feeds_downstream(http_server):
    scenario = {
        "id": "sc-http",
        "name": "graph_http",
        "test_class": "e2e",
        "steps": [
            {
                "id": "ctx",
                "kind": "code",
                "config": {"language": "python", "code": "return {\"id\": 'RR42'}"},
            },
            {
                "id": "call",
                "kind": "http",
                "config": {
                    "method": "GET",
                    "url": http_server + "/txn/${ctx.response.id}",
                    "authentication": {"type": "bearer", "token": "SECRET"},
                    "options": {"responseFormat": "json"},
                },
            },
        ],
        "edges": [{"source": "ctx", "source_port": "out", "target": "call"}],
    }
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rhttp")
    by_id = _steps_by_id(summary)
    assert by_id["call"][0]["status"] == PASSED
    body = by_id["call"][0]["response"]["body"]
    assert body["path"] == "/txn/RR42"
    assert body["auth"] == "Bearer SECRET"


@pytest.mark.asyncio
async def test_http_node_4xx_fails(http_server):
    scenario = {
        "id": "sc-http-404",
        "name": "http404",
        "test_class": "e2e",
        "stop_on_failure": False,
        "steps": [
            {
                "id": "call",
                "kind": "http",
                "config": {"method": "POST", "url": http_server + "/missing"},
            },
        ],
        "edges": [{"source": "call", "source_port": "out", "target": "call2"}],
    }
    # add a trailing node so the graph path is used
    scenario["steps"].append(
        {"id": "call2", "kind": "http", "config": {"method": "GET", "url": http_server + "/ok"}}
    )
    engine = WorkerEngine(MOCK_ENV, InMemorySink())
    summary = await engine.run_scenario_batch([scenario], run_id="rhttp404")
    by_id = _steps_by_id(summary)
    # POST to BaseHTTPRequestHandler without a handler -> 501; >=400 => failed
    assert by_id["call"][0]["status"] == FAILED
    assert summary["scenarios"][0]["status"] == FAILED


# -- unit: variable resolver -------------------------------------------------


def test_resolve_whole_string_preserves_type():
    ctx = {"step_003": {"response": {"amount": 10000, "rrn": "abc"}}}
    assert resolve_value("${step_003.response.amount}", ctx) == 10000
    assert resolve_value("rrn=${step_003.response.rrn}", ctx) == "rrn=abc"


def test_resolve_unknown_reference_raises():
    with pytest.raises(UnresolvedReferenceError):
        resolve_value("${nope.response.x}", {})
