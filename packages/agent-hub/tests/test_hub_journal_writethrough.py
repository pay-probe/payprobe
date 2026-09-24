"""Write-through journalling (the gap the 2026-09-24 security review left
open): every journal record reaches the heartbeat row the moment it is made,
before the write it protects, so a process that dies mid-wake leaves the
``before`` snapshots behind and the row stays revertable. A record that
cannot be persisted refuses the write; a late runner never resurrects a row
the watchdog already failed.
"""

import threading
import time

from agent_hub.llm import FakeLLMBackend
from hub_testkit import FakeBackend, agent_spec, wire_fakes

FULL = {
    "role": "Exec",
    "mode": "full",
    "tools": ["upsert_connection", "delete_connection"],
    "write_scope": {"projects": ["*"], "environments": []},
}


def _publish(client, name, **over):
    r = client.post("/agents", json={"name": name, "spec": agent_spec(**over)})
    assert r.status_code == 201, r.text
    assert client.post(f"/agents/{name}/versions/1/publish").status_code == 200


class _BlockAfterFirstWrite(FakeLLMBackend):
    """Two writes, but the second LLM turn waits on ``gate``: the wake is
    caught between its first write and its end, like a process about to die."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__(
            [
                {
                    "tool_calls": [
                        {
                            "name": "upsert_connection",
                            "args": {"name": "switch", "config": {"port": 1}},
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "name": "upsert_connection",
                            "args": {"name": "switch", "config": {"port": 2}},
                        }
                    ]
                },
            ],
            final="applied",
        )
        self.gate = gate
        self.turn = 0

    def complete(self, convo, tools):
        self.turn += 1
        if self.turn == 2:
            self.gate.wait(timeout=10)
        return super().complete(convo, tools)


def _wait(pred, t=5.0):
    t0 = time.time()
    while time.time() - t0 < t:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition never met")


def test_journal_record_is_on_the_row_before_the_wake_ends_and_survives_an_orphan(client):
    _publish(client, "exec", **FULL)
    be = FakeBackend()
    gate = threading.Event()
    wire_fakes(client.app, be, lambda spec: _BlockAfterFirstWrite(gate))
    r = client.post("/agents/exec/wake", json={"input": "go"})
    assert r.status_code == 202, r.text
    hb_id = r.json()["id"]
    # the first write happened and its record is already durable, mid-wake
    _wait(lambda: be.connections["switch"]["port"] == 1)
    row = _wait(lambda: (h := client.get(f"/heartbeats/{hb_id}").json()) and h["journal"] and h)
    assert row["status"] == "running"
    assert [j["key"] for j in row["journal"]] == ["switch"]
    assert row["journal"][0]["before"]["port"] == 9000
    # the process "dies": the watchdog fails the row (no grace), the journal stays
    rows = client.portal.call(client.app.state.store.reconcile_running, -10_000)
    assert [x["id"] for x in rows] == [hb_id]
    row = client.get(f"/heartbeats/{hb_id}").json()
    assert row["status"] == "failed" and "orphaned" in row["error"] and len(row["journal"]) == 1
    # a human reverts from what the write-through left behind
    assert client.post(f"/heartbeats/{hb_id}/revert").json()["reverted"] == 1
    assert be.connections["switch"]["port"] == 9000
    # the runner wakes up late and finishes: it must not resurrect the row
    gate.set()
    time.sleep(0.5)
    row = client.get(f"/heartbeats/{hb_id}").json()
    assert row["status"] == "failed" and row["reverted_at"]


def test_write_is_refused_when_its_journal_record_cannot_be_persisted(client, monkeypatch):
    _publish(client, "exec", **FULL)
    be = FakeBackend()
    wire_fakes(
        client.app,
        be,
        lambda spec: FakeLLMBackend(
            [
                {
                    "tool_calls": [
                        {
                            "name": "upsert_connection",
                            "args": {"name": "switch", "config": {"port": 7}},
                        }
                    ]
                }
            ],
            final="tried",
        ),
    )

    async def boom(hb_id, record):
        raise RuntimeError("journal store unavailable")

    monkeypatch.setattr(client.app.state.store, "append_journal", boom)
    r = client.post("/agents/exec/wake", json={"input": "go"})
    assert r.status_code == 202, r.text
    hb = _wait(
        lambda: (h := client.get(f"/heartbeats/{r.json()['id']}").json())
        and h["status"] != "running"
        and h
    )
    tool = next(s for s in hb["steps"] if s["kind"] == "tool")
    assert not tool["ok"] and "journal store unavailable" in tool["error"]
    assert be.connections["switch"]["port"] == 9000  # the write never happened
    assert hb["journal"] == []
