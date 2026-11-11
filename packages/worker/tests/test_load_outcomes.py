"""Load errors split by outcome: rejected / timeout / connection / other.

``errors`` alone cannot tell a host that answered "no" from a host that did
not answer. These tests pin the classification (structure first, then the
error strings the adapters really produce), that the four outcomes always sum
to ``errors``, that they survive the fleet merge, and that a sample from a
worker older than the split merges to an empty breakdown.
"""

from __future__ import annotations

import asyncio

import pytest

from worker.engine.load import LoadDriver, Shard, merge_samples
from worker.engine.load.outcomes import (
    CONNECTION,
    OTHER,
    OUTCOMES,
    REJECTED,
    TIMEOUT,
    classify_error_text,
    classify_exception,
    classify_step,
)
from worker.engine.runner import FAILED, ScenarioResult, StepOutcome
from worker.load_worker import _failure


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # TCP adapter
        ("No response within 30s.", TIMEOUT),
        (
            (
                "No response within 30s. (peer closed the connection 11 ms after the "
                "last send, 0 bytes read since)"
            ),
            TIMEOUT,
        ),
        ("Connection lost: [Errno 60] Operation timed out", CONNECTION),
        ("10.0.0.5:8583 is not connected", CONNECTION),
        ("10.0.0.5:8583 did not reconnect in time", CONNECTION),
        ("[Errno 61] Connect call failed ('127.0.0.1', 9)", CONNECTION),
        ("ConnectionRefusedError: [Errno 61] refused", CONNECTION),
        # HTTP runner
        ("request timed out after 5000 ms", TIMEOUT),
        ("ConnectError: [Errno 111] Connection refused", CONNECTION),
        # HSM / NATS / thrown
        ("TimeoutError", TIMEOUT),
        ("TimeoutError: nats: timeout", TIMEOUT),
        ("NoRespondersError: nats: no responders available for request", CONNECTION),
        ("hsm connection closed", CONNECTION),
        # soak connect phase
        ("connect: TimeoutError: ", CONNECTION),
        # nothing recognisable
        ("payload cannot be packed: DE 4 too long", OTHER),
        ("", OTHER),
        (None, OTHER),
    ],
)
def test_error_text_classification(error, expected):
    assert classify_error_text(error) == expected


def test_exception_classification():
    assert classify_exception(TimeoutError()) == TIMEOUT
    assert classify_exception(ConnectionResetError("reset")) == CONNECTION
    assert classify_exception(ValueError("bad payload")) == OTHER


def test_a_step_with_a_reply_is_rejected_whatever_its_error_says():
    # the TCP reject_reason path sets an error AND carries the reply
    assert classify_step({"response_code": "120"}, "declined: 120") == REJECTED
    assert classify_step({}, "No response within 30s.") == TIMEOUT
    assert classify_step(None, None) == OTHER


def _failed(**kw) -> ScenarioResult:
    step = StepOutcome(
        step_id="s1",
        target="svfe_pos",
        action="send_message",
        status=FAILED,
        duration_ms=12,
        **kw,
    )
    return ScenarioResult(scenario_id="sc", name="purchase", status=FAILED, steps=[step])


def test_scenario_failure_outcomes():
    declined = _failed(
        response={"response_code": "120"},
        assertions=[
            {
                "field": "response_code",
                "operator": "eq",
                "expected": "000",
                "actual": "120",
                "passed": False,
            }
        ],
    )
    assert _failure(declined) == (
        "svfe_pos.send_message: assertion response_code eq '000' (got '120')",
        REJECTED,
    )
    timed_out = _failed(response={}, error="No response within 30s.")
    assert _failure(timed_out) == ("svfe_pos.send_message: No response within 30s.", TIMEOUT)
    lost = _failed(response={}, error="Connection lost: peer reset")
    assert _failure(lost)[1] == CONNECTION
    assert _failure(ScenarioResult(scenario_id="sc", name="x", status=FAILED)) == (
        "scenario failed",
        OTHER,
    )


def _shard(**kw) -> Shard:
    base = {
        "run_id": "r1",
        "worker_index": 0,
        "worker_count": 1,
        "type": "steady",
        "duration_s": 0.3,
        "target_tps": 200,
        "max_in_flight": 50,
        "drain_s": 2.0,
    }
    base.update(kw)
    return Shard(**base)


async def test_driver_outcomes_sum_to_errors_and_reach_the_sample():
    results = [
        (True, 1.0),
        (
            False,
            1.0,
            "svfe_pos.send_message: assertion response_code eq '000' (got '120')",
            REJECTED,
        ),
        (False, 1.0, "svfe_pos.send_message: No response within 30s.", TIMEOUT),
        (False, 1.0, "svfe_pos.send_message: Connection lost: x"),  # no outcome → from text
        (False, 1.0),  # no reason at all → other
    ]
    i = 0

    async def run_once():
        nonlocal i
        r = results[i % len(results)]
        i += 1
        if i % 7 == 0:
            raise TimeoutError()
        return r

    driver = LoadDriver(_shard(), run_once=run_once, late_replies=lambda: 3)
    stats = await driver.run()
    assert stats.errors > 0
    assert set(stats.error_outcomes) == set(OUTCOMES)
    assert sum(stats.error_outcomes.values()) == stats.errors
    assert stats.error_outcomes[REJECTED] > 0
    assert stats.error_outcomes[TIMEOUT] > 0
    assert stats.error_outcomes[CONNECTION] > 0
    assert stats.error_outcomes[OTHER] > 0
    assert stats.late_replies == 3
    sample = driver.sample()
    assert sample["error_outcomes"] == stats.error_outcomes
    assert sample["late_replies"] == 3


async def test_a_broken_late_reply_counter_never_breaks_a_sample():
    def boom() -> int:
        raise RuntimeError("adapter gone")

    async def run_once():
        return True, 1.0

    driver = LoadDriver(_shard(duration_s=0.05), run_once=run_once, late_replies=boom)
    await driver.run()
    assert driver.sample()["late_replies"] == 0


def test_merge_sums_outcomes_and_tolerates_old_samples():
    a = {
        "sent": 10,
        "received": 6,
        "errors": 4,
        "error_outcomes": {REJECTED: 3, TIMEOUT: 1, CONNECTION: 0, OTHER: 0},
        "late_replies": 1,
    }
    b = {
        "sent": 5,
        "received": 3,
        "errors": 2,
        "error_outcomes": {REJECTED: 0, TIMEOUT: 2, CONNECTION: 0, OTHER: 0},
        "late_replies": 2,
    }
    merged = merge_samples([a, b])
    assert merged.error_outcomes == {REJECTED: 3, TIMEOUT: 3, CONNECTION: 0, OTHER: 0}
    assert sum(merged.error_outcomes.values()) == merged.errors
    assert merged.late_replies == 3
    s = merged.summary()
    assert s["error_outcomes"][TIMEOUT] == 3 and s["late_replies"] == 3

    old = merge_samples([{"sent": 3, "received": 1, "errors": 2}])
    assert old.error_outcomes == {}  # no breakdown, not a fake "0 timeouts"
    assert old.summary()["late_replies"] == 0


async def test_soak_heartbeat_failure_carries_reason_and_outcome():
    calls = 0

    class Client:
        async def heartbeat(self):
            nonlocal calls
            calls += 1
            return False, 1.0, "pos.echo: No response within 1s.", TIMEOUT

        async def aclose(self):
            return None

    async def open_client():
        return Client()

    driver = LoadDriver(
        _shard(type="soak", connections=1, heartbeat_interval_s=0.05, duration_s=0.2),
        open_client=open_client,
    )
    stats = await driver.run()
    assert calls > 0
    assert stats.error_outcomes[TIMEOUT] == stats.errors > 0
    assert stats.last_error == "pos.echo: No response within 1s."


def test_outcome_keys_are_stable():
    # the portal and the run-compare view key off these names
    assert OUTCOMES == ("rejected", "timeout", "connection", "other")


# -- the real path: scenario runner + engine + TCP adapter --------------------


async def _run_once_against(server, *, timeout_s: float = 0.3):
    from worker.engine import WorkerEngine
    from worker.load_worker import make_run_once

    port = await server.start()
    engine = WorkerEngine(
        {
            "adapters": {
                "svfe_pos": {
                    "adapter": "tcp",
                    "protocol": "iso8583",
                    "host": "127.0.0.1",
                    "port": port,
                    "sign_on": {"enabled": False},
                    "response_timeout_sec": timeout_s,
                }
            }
        }
    )
    scenario = {
        "id": "purchase",
        "steps": [
            {
                "id": "s1",
                "target": "svfe_pos",
                "action": "send_0200",
                "payload": {"amount": 100},
                "assertions": [{"field": "response_code", "operator": "eq", "expected": "00"}],
            }
        ],
    }
    try:
        return await make_run_once(engine, scenario)(), engine
    finally:
        await engine.teardown()
        await server.stop()


async def test_real_tcp_decline_is_rejected():
    from worker.tests.test_tcp_adapter import MockIsoServer

    (ok, _lat, reason, outcome), _ = await _run_once_against(MockIsoServer(response_code="05"))
    assert not ok
    assert outcome == REJECTED, reason


async def test_real_tcp_no_reply_is_a_timeout():
    from worker.tests.test_tcp_adapter import MockIsoServer

    (ok, _lat, reason, outcome), _ = await _run_once_against(MockIsoServer(drop=True))
    assert not ok
    assert outcome == TIMEOUT, reason
    assert "No response within" in reason


async def test_real_tcp_late_reply_reaches_the_registry_count():
    from worker.tests.test_tcp_adapter import MockIsoServer

    server = MockIsoServer(delay_s=0.4)
    from worker.engine import WorkerEngine
    from worker.load_worker import make_run_once

    port = await server.start()
    engine = WorkerEngine(
        {
            "adapters": {
                "svfe_pos": {
                    "adapter": "tcp",
                    "protocol": "iso8583",
                    "host": "127.0.0.1",
                    "port": port,
                    "sign_on": {"enabled": False},
                    "response_timeout_sec": 0.15,
                }
            }
        }
    )
    scenario = {
        "id": "purchase",
        "steps": [{"id": "s1", "target": "svfe_pos", "action": "send_0200", "payload": {}}],
    }
    try:
        ok, _lat, _reason, outcome = await make_run_once(engine, scenario)()
        assert not ok and outcome == TIMEOUT
        await asyncio.sleep(0.5)
        assert engine.registry.late_replies() == 1
    finally:
        await engine.teardown()
        await server.stop()


def test_a_called_sub_scenario_is_classified_by_its_child_error():
    from worker.engine.runner import _sub_failure

    child = StepOutcome(
        step_id="c1",
        target="svfe_pos",
        action="send_0200",
        status=FAILED,
        duration_ms=1,
        response={},
        error="No response within 30s.",
    )
    sub = ScenarioResult(scenario_id="sub", name="auth", status=FAILED, steps=[child])
    err = _sub_failure(sub)
    assert err == "sub-scenario failed: svfe_pos.send_0200: No response within 30s."
    call = StepOutcome(
        step_id="s1",
        target="call",
        action="auth",
        status=FAILED,
        duration_ms=1,
        response={"c1": {}},  # never empty for a call step
        error=err,
    )
    r = ScenarioResult(scenario_id="sc", name="x", status=FAILED, steps=[call])
    assert _failure(r)[1] == TIMEOUT
