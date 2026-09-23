"""A tool failure is data for the model, never a dead heartbeat.

Regression for 2026-09-23: failure-triage was woken with a run label in its
input, called get_scenario with "initiator · Showcase network" as the id, the
REST backend built a URL with a space, urllib raised http.client.InvalidURL
(not a ValueError, so dispatch did not catch it), the exception escaped the
runner and the heartbeat died with no steps and no tokens recorded."""

import http.client
import time

from agent_hub.llm import FakeLLMBackend
from hub_testkit import FakeBackend, wire_fakes

from payprobe_common import agent_toolkit as tk
from payprobe_common.rest_backend import RestBackend

BAD_ID = "initiator · Showcase network"


def test_rest_backend_percent_encodes_every_path_id():
    seen: list[tuple[str, str]] = []

    def fake_request(method, url, body=None, **kw):
        seen.append((method, url))

    be = RestBackend(fake_request, "http://s", "http://r", "http://i")
    be.get_scenario(BAD_ID)
    be.get_connection("switch out")
    be.get_table("a/b")
    be.get_network(BAD_ID)
    be.plan_network(BAD_ID)
    be.get_environment("prod env")
    be.get_format("iso 8583")
    be.list_scenarios(project_id="p 1")
    be.list_formats(protocol="iso 8583")
    for _, url in seen:
        assert " " not in url and "·" not in url, url
    assert seen[0][1] == "http://s/scenarios/initiator%20%C2%B7%20Showcase%20network"
    assert seen[1][1] == "http://s/connections/switch%20out"
    assert seen[2][1] == "http://s/tables/a%2Fb"  # a slash cannot escape the segment
    assert seen[4][1].endswith("/plan")
    assert seen[7][1] == "http://s/scenarios?project_id=p%201"


def test_dispatch_turns_any_exception_into_an_error_envelope():
    class Boom(FakeBackend):
        def list_runs(self):
            raise http.client.InvalidURL("URL can't contain control characters")

    out = tk.dispatch(tk.ToolContext(backend=Boom()), "list_runs", {})
    assert out["ok"] is False and out["guardrail"] is False
    assert "InvalidURL" in out["error"]


class InvalidUrlBackend(FakeBackend):
    def get_scenario(self, sid):
        self.calls.append(("get_scenario", sid))
        raise http.client.InvalidURL(f"URL can't contain control characters. '{sid}'")


def _wait_done(client, hb_id, timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        hb = client.get(f"/heartbeats/{hb_id}").json()
        if hb["status"] != "running":
            return hb
        time.sleep(0.02)
    raise AssertionError("heartbeat did not finish")


def test_heartbeat_survives_a_tool_that_raises_and_records_the_step(client):
    be = InvalidUrlBackend()
    llm = FakeLLMBackend(
        [{"tool_calls": [{"name": "get_scenario", "args": {"scenario_id": BAD_ID}}]}],
        final='{"run_id": "r", "category": "unknown", "root_cause": "scenario not found"}',
    )
    wire_fakes(client.app, be, lambda spec: llm)
    r = client.post("/agents/failure-triage/wake", json={"input": '{"run_id": "r"}'})
    assert r.status_code == 202, r.text
    hb = _wait_done(client, r.json()["id"])
    assert hb["status"] == "done", hb["error"]  # the wake finished, the model saw the error
    step = next(s for s in hb["steps"] if s["kind"] == "tool")
    assert step["ok"] is False and "InvalidURL" in step["error"]
    assert hb["tokens_in"] > 0 and len(hb["steps"]) == 3  # llm, tool, llm: nothing lost
    assert hb["subject"] == "run:r"
    # the model was told, in the tool message, what went wrong
    tool_msg = next(m for m in llm.calls[-1] if m.get("role") == "tool")
    assert "InvalidURL" in tool_msg["content"]
