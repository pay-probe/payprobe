"""GroupAdapter: a participant group routes dispatches across member connections."""

from worker.adapters.registry import AdapterRegistry
from worker.adapters.tcp.responder import TcpResponder


def _responder():
    return TcpResponder(
        {"protocol": "iso8583", "default": {"echo": ["11", "37"], "set": {"39": "00"}}}
    )


def _member(name, port):
    return {
        "connection": name,
        "config": {
            "adapter": "tcp",
            "protocol": "iso8583",
            "host": "127.0.0.1",
            "port": port,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        },
    }


async def test_group_round_robins_across_members():
    r1, r2 = _responder(), _responder()
    p1, p2 = await r1.start(), await r2.start()
    reg = AdapterRegistry(
        {
            "adapters": {
                "issuers": {
                    "adapter": "group",
                    "members": [_member("m1", p1), _member("m2", p2)],
                    "selection": {"policy": "round_robin"},
                }
            }
        }
    )
    adapter = await reg.get("issuers")
    try:
        for _ in range(4):
            res = await adapter.execute("send_0200", {"amount": 1000})
            assert res.response_payload["response_code"] == "00"
        assert len(r1.received) == 2 and len(r2.received) == 2
    finally:
        await reg.disconnect_all()
        await r1.stop()
        await r2.stop()


async def test_responder_stop_closes_active_connections():
    """Stopping a responder drops live client sockets (not just the listener),
    so a client notices and can reconnect to the next instance. Regression: a
    restarted simulator received nothing because the old connection lingered."""
    from worker.adapters.tcp.adapter import TcpAdapter

    r = _responder()
    p = await r.start()
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": p,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await a.connect()
    first = await a.execute("send_0200", {"amount": 1})
    assert first.success and len(r._conns) == 1  # connection tracked

    await r.stop()  # must close the live socket too
    after = await a.execute("send_0200", {"amount": 1})
    assert not after.success  # connection really dropped
    await a.disconnect()


async def test_group_recovers_after_member_restart():
    """round_robin across 2 members: when one goes down its cached child is
    evicted, and after it 'restarts' the group reconnects and routes to it
    again (the regression: a restarted member never received traffic)."""
    from worker.adapters.base.base_adapter import BaseAdapter, StepResult
    from worker.adapters.group import GroupAdapter

    state = {"m1": {"down": False, "ok": 0}, "m2": {"down": False, "ok": 0}}

    class FakeChild(BaseAdapter):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.s = state[cfg["id"]]

        async def connect(self):
            if self.s["down"]:
                raise ConnectionError("refused")  # can't connect while down

        async def execute(self, action, payload):
            if self.s["down"]:
                # transport failure surfaces as success=False (like TcpAdapter)
                return StepResult(
                    success=False,
                    request_payload=payload,
                    response_payload=None,
                    duration_ms=0,
                    error="down",
                )
            self.s["ok"] += 1
            return StepResult(
                success=True, request_payload=payload, response_payload={"ok": True}, duration_ms=1
            )

        async def disconnect(self):
            pass

        async def health_check(self):
            return not self.s["down"]

    members = [
        {"connection": "m1", "config": {"id": "m1"}},
        {"connection": "m2", "config": {"id": "m2"}},
    ]
    group = GroupAdapter(
        {}, members, {"policy": "round_robin"}, make_child=lambda cfg: FakeChild(cfg)
    )

    # both members serve
    for _ in range(2):
        await group.execute("send_0200", {})
    assert state["m1"]["ok"] == 1 and state["m2"]["ok"] == 1

    # m2 goes down — its turns fail (success=False, or connect refused on rebuild)
    state["m2"]["down"] = True
    for _ in range(4):
        try:
            await group.execute("send_0200", {})
        except Exception:
            pass
    assert state["m1"]["ok"] >= 3  # survivor kept serving
    assert state["m2"]["ok"] == 1  # down member took nothing more

    # m2 restarts — the next round_robin turn rebuilds the evicted child and
    # routes to it again
    state["m2"]["down"] = False
    for _ in range(2):
        await group.execute("send_0200", {})
    assert state["m2"]["ok"] >= 2  # traffic returned to the restarted member


async def test_group_sticky_routes_same_key_to_one_member():
    r1, r2, r3 = _responder(), _responder(), _responder()
    p1, p2, p3 = await r1.start(), await r2.start(), await r3.start()
    reg = AdapterRegistry(
        {
            "adapters": {
                "issuers": {
                    "adapter": "group",
                    "members": [_member("m1", p1), _member("m2", p2), _member("m3", p3)],
                    "selection": {"policy": "sticky", "key": "pan"},
                }
            }
        }
    )
    adapter = await reg.get("issuers")
    try:
        for _ in range(5):
            await adapter.execute("send_0200", {"amount": 1000, "pan": "4111111111111111"})
        counts = sorted([len(r1.received), len(r2.received), len(r3.received)])
        # one member took all 5, the others none — same PAN sticks to one member
        assert counts == [0, 0, 5]
    finally:
        await reg.disconnect_all()
        await r1.stop()
        await r2.stop()
        await r3.stop()
