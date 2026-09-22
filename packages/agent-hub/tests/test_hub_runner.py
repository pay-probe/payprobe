"""The heartbeat runner against a scripted LLM and an in-memory platform:
scoping, plan-mode proposals, budgets, cancel/pause, journal, envelope."""

from agent_hub.llm import FakeLLMBackend
from agent_hub.models import AgentSpec
from agent_hub.runner import run_heartbeat
from hub_testkit import FakeBackend, agent_spec

OK = {"paused": False, "cancel": False}


def _spec(**over) -> AgentSpec:
    return AgentSpec.model_validate(agent_spec(**over))


def test_read_then_answer_records_steps_and_usage():
    llm = FakeLLMBackend(
        [{"text": "checking", "tool_calls": [{"name": "list_runs"}]}], final="one failed run"
    )
    out = run_heartbeat(_spec(), "what failed?", FakeBackend(), llm, lambda: OK)
    assert out.status == "done" and out.result == "one failed run"
    kinds = [s["kind"] for s in out.steps]
    assert kinds == ["llm", "tool", "llm"]
    assert out.steps[1]["ok"] is True and out.steps[1]["tool"] == "list_runs"
    assert out.tokens["input"] == 200 and out.journal == [] and out.proposed == []
    # the untrusted note and the mode preamble are in the system prompt
    system = llm.calls[0][0]["content"]
    assert "kind=untrusted" in system and "You may only read" in system


def test_untrusted_results_are_enveloped():
    import json

    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}]}])
    run_heartbeat(_spec(), "", FakeBackend(), llm, lambda: OK)
    tool_msg = next(m for m in llm.calls[1] if m["role"] == "tool")
    res = json.loads(tool_msg["content"])
    assert res["result"]["kind"] == "untrusted" and res["result"]["source"] == "list_runs"
    assert "IGNORE PREVIOUS" in json.dumps(res["result"]["data"])  # data, not followed


def test_tool_outside_allowlist_is_refused_even_if_named():
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_connections"}]}])
    out = run_heartbeat(_spec(tools=["list_runs"]), "", FakeBackend(), llm, lambda: OK)
    assert out.status == "done"
    step = out.steps[1]
    assert step["ok"] is False and step["guardrail"] is True
    assert "not in this agent's allowlist" in step["error"]


def test_plan_mode_records_proposed_write_without_executing():
    be = FakeBackend()
    llm = FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {
                        "name": "upsert_connection",
                        "args": {"name": "issuer", "config": {"port": 9100}},
                    }
                ]
            }
        ],
        final="proposed one connection",
    )
    spec = _spec(
        mode="plan",
        tools=["list_runs", "upsert_connection"],
        write_scope={"projects": ["*"], "environments": []},
    )
    out = run_heartbeat(spec, "add issuer", be, llm, lambda: OK)
    assert out.status == "done"
    assert out.proposed == [
        {
            "step": 1,
            "tool": "upsert_connection",
            "args": {"name": "issuer", "config": {"port": 9100}},
        }
    ]
    assert "issuer" not in be.connections  # nothing executed
    assert out.steps[1]["proposed"] is True and out.journal == []


def test_full_mode_executes_and_journals_reversibly():
    be = FakeBackend()
    llm = FakeLLMBackend(
        [
            {
                "tool_calls": [
                    {
                        "name": "upsert_connection",
                        "args": {"name": "switch", "config": {"port": 9999}},
                    }
                ]
            }
        ]
    )
    spec = _spec(
        mode="full",
        tools=["upsert_connection"],
        write_scope={"projects": ["*"], "environments": []},
    )
    out = run_heartbeat(spec, "", be, llm, lambda: OK)
    assert out.status == "done" and out.steps[1]["ok"] is True
    assert be.connections["switch"]["port"] == 9999
    assert len(out.journal) == 1 and out.journal[0]["before"]["port"] == 9000
    # the journal is data: any replica can restore from it
    from payprobe_common import agent_toolkit as tk

    n = tk.restore_journal(tk.ToolContext(backend=be), out.journal)
    assert n == 1 and be.connections["switch"]["port"] == 9000


def test_write_scope_refuses_global_write_without_star():
    be = FakeBackend()
    llm = FakeLLMBackend(
        [{"tool_calls": [{"name": "upsert_connection", "args": {"name": "x", "config": {}}}]}]
    )
    spec = _spec(
        mode="full",
        tools=["upsert_connection"],
        write_scope={"projects": ["p1"], "environments": ["mock"]},
    )
    out = run_heartbeat(spec, "", be, llm, lambda: OK)
    assert out.steps[1]["guardrail"] is True
    assert "global registry writes need '*'" in out.steps[1]["error"]
    assert "x" not in be.connections


def test_max_steps_ends_in_budget_exceeded():
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}]}] * 10)
    out = run_heartbeat(_spec(limits={"max_steps": 3}), "", FakeBackend(), llm, lambda: OK)
    assert out.status == "budget_exceeded" and "max_steps (3)" in out.error
    assert sum(1 for s in out.steps if s["kind"] == "llm") == 3


def test_max_tokens_ends_in_budget_exceeded():
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}]}] * 10, tokens_per_turn=1000)
    out = run_heartbeat(_spec(limits={"max_tokens": 1500}), "", FakeBackend(), llm, lambda: OK)
    assert out.status == "budget_exceeded" and "max_tokens" in out.error


def test_cancel_and_pause_stop_before_the_next_call():
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}, {"name": "list_runs"}]}])
    seen = {"n": 0}

    def flags():
        seen["n"] += 1
        return {"paused": False, "cancel": seen["n"] >= 3}  # llm, tool#1 ok, tool#2 stops

    out = run_heartbeat(_spec(), "", FakeBackend(), llm, flags)
    assert out.status == "cancelled" and out.error == "cancel requested"
    assert [s["kind"] for s in out.steps] == ["llm", "tool"]

    out = run_heartbeat(
        _spec(), "", FakeBackend(), FakeLLMBackend(), lambda: {"paused": True, "cancel": False}
    )
    assert out.status == "paused" and out.steps == []


def test_wall_clock_is_enforced():
    clock = {"t": 0.0}
    llm = FakeLLMBackend([{"tool_calls": [{"name": "list_runs"}]}] * 5)

    def now():
        clock["t"] += 20.0
        return clock["t"]

    out = run_heartbeat(
        _spec(limits={"wall_clock_s": 30}), "", FakeBackend(), llm, lambda: OK, now=now
    )
    assert out.status == "timed_out"


def test_llm_failure_is_a_failed_outcome_not_an_exception():
    class Boom:
        model = "boom"

        @property
        def usage(self):
            return {"input": 0, "output": 0}

        def complete(self, convo, tools):
            raise RuntimeError("HTTP 500: provider down")

    out = run_heartbeat(_spec(), "", FakeBackend(), Boom(), lambda: OK)
    assert out.status == "failed" and "provider down" in out.error
