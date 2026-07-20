"""Simulator lifecycle endpoints: start a responder, drive it, inspect, stop."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from fastapi import Response  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.simulator_store import SimulatorStore  # noqa: E402
from worker.adapters.tcp.adapter import TcpAdapter  # noqa: E402


async def _drive(port: int, amounts: list[int]) -> None:
    """Hit a running simulator with a few 0200s over a fresh connection."""
    adapter = TcpAdapter({"host": "127.0.0.1", "port": port,
                          "sign_on": {"enabled": False},
                          "reconnect": {"enabled": False}, "response_timeout_sec": 2})
    await adapter.connect()
    try:
        for amt in amounts:
            await adapter.execute("send_0200", {"amount": amt})
    finally:
        await adapter.disconnect()


async def test_start_drive_inspect_stop_simulator():
    m.SIMULATORS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="switch-sim",
        config={"protocol": "iso8583",
                "rules": [{"when": {"mti": "0200", "de": {"4": {"gte": "000000100000"}}},
                           "respond": {"echo": ["11"], "set": {"39": "61"}}}],
                "default": {"echo": ["11", "37"], "set": {"39": "00"}}},
    ))
    sid, port = started["id"], started["port"]
    assert port and started["rules"] == 1

    # a real client hits the running simulator
    adapter = TcpAdapter({"host": "127.0.0.1", "port": port,
                          "sign_on": {"enabled": False},
                          "reconnect": {"enabled": False}, "response_timeout_sec": 2})
    await adapter.connect()
    try:
        approved = await adapter.execute("send_0200", {"amount": 1000})
        assert approved.response_payload["response_code"] == "00"
        declined = await adapter.execute("send_0200", {"amount": 100000})
        assert declined.response_payload["response_code"] == "61"
    finally:
        await adapter.disconnect()

    listed = await m.list_simulators()
    assert any(s["id"] == sid and s["received"] >= 2 for s in listed)

    detail = await m.get_simulator(sid)
    assert len(detail["log"]) >= 2
    assert detail["log"][0]["mti"] == "0200"

    resp = await m.stop_simulator(sid)
    assert isinstance(resp, Response) and resp.status_code == 204
    assert sid not in m.SIMULATORS


async def test_saved_simulator_registry_lifecycle():
    """Save a simulator, start it, drive traffic, read metrics, clear, stop, delete."""
    m.SIMULATORS.clear()
    m.SIM_SAMPLES.clear()
    m._SIM_PREV.clear()
    m.simulator_store = SimulatorStore(":memory:")

    created = await m.create_saved_simulator(m.SavedSimulatorDraft(
        label="switch-sim",
        config={"protocol": "iso8583",
                "rules": [{"when": {"mti": "0200"},
                           "respond": {"echo": ["11"], "set": {"39": "00"}}}],
                "default": {"set": {"39": "00"}}},
        enabled=True,
    ))
    cid = created["id"]
    assert created["enabled"] and not created["running"]
    assert any(s["id"] == cid for s in await m.list_saved_simulators())

    started = await m.start_saved_simulator(cid)
    assert started["running"] and started["port"]
    await _drive(started["port"], [1000, 2000])

    mets = await m.simulator_metrics(cid)
    assert mets["stats"]["received"] >= 2
    assert mets["stats"]["by_mti"].get("0200", 0) >= 2
    assert mets["stats"]["by_response_code"].get("00", 0) >= 2

    # clear resets the live counters but keeps the simulator listening
    cleared = await m.clear_simulator(cid)
    assert cleared["received"] == 0
    assert (await m.simulator_metrics(cid))["stats"]["received"] == 0

    # stop removes the running responder but keeps the saved config
    stopped = await m.stop_saved_simulator(cid)
    assert not stopped["running"]
    assert cid not in m.SIMULATORS
    assert m.simulator_store.has(cid)

    # delete forgets it entirely
    resp = await m.delete_saved_simulator(cid)
    assert isinstance(resp, Response) and resp.status_code == 204
    assert not m.simulator_store.has(cid)


async def test_enabled_toggle_and_autostart():
    """Enabled saved simulators come up via the boot auto-start hook."""
    m.SIMULATORS.clear()
    m.simulator_store = SimulatorStore(":memory:")

    created = await m.create_saved_simulator(m.SavedSimulatorDraft(
        label="auto-sim",
        config={"protocol": "iso8583", "default": {"set": {"39": "00"}}},
        enabled=False,
    ))
    cid = created["id"]

    toggled = await m.set_saved_simulator_enabled(cid, True)
    assert toggled["enabled"]

    await m._autostart_simulators()
    assert cid in m.SIMULATORS

    await m.stop_saved_simulator(cid)
    assert cid not in m.SIMULATORS


async def test_payshield_simulator_starts_and_answers_nc():
    """A saved simulator with protocol 'payshield' boots the HSM simulator and
    answers an NC diagnostics command over the wire."""
    import asyncio

    from worker.adapters.hsm.payshield import PayShieldSimulator

    m.SIMULATORS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="hsm-sim",
        config={"protocol": "payshield", "host": "127.0.0.1", "port": 0},
    ))
    sid, port = started["id"], started["port"]
    assert isinstance(m.SIMULATORS[sid], PayShieldSimulator)
    assert started["protocol"] == "payshield" and port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        body = b"HDR1NC"
        writer.write(len(body).to_bytes(2, "big") + body)
        await writer.drain()
        prefix = await reader.readexactly(2)
        resp = (await reader.readexactly(int.from_bytes(prefix, "big"))).decode()
        assert resp[:4] == "HDR1"          # header echoed
        assert resp[4:6] == "ND"           # NC -> ND response code
        assert resp[6:8] == "00"           # no error
    finally:
        writer.close()

    await m.stop_simulator(sid)
    assert sid not in m.SIMULATORS


async def test_hsm_command_drives_payshield_simulator():
    """POST /hsm/command sends a host command to a running payShield simulator
    via the client adapter and returns the parsed reply."""
    m.SIMULATORS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="hsm-sim",
        config={"protocol": "payshield", "host": "127.0.0.1", "port": 0},
    ))
    port = started["port"]
    try:
        nc = await m.hsm_command(m.HsmCommandRequest(host="127.0.0.1", port=port, action="nc"))
        assert nc["ok"] and nc["response"]["response_code"] == "ND"

        gen = await m.hsm_command(m.HsmCommandRequest(
            host="127.0.0.1", port=port, action="generate_key",
            params={"key_type": "402", "scheme": "U"}))
        cvk = gen["response"]["key"]
        assert gen["ok"] and cvk.startswith("U")

        made = await m.hsm_command(m.HsmCommandRequest(
            host="127.0.0.1", port=port, action="generate_cvv",
            params={"cvk": cvk, "pan": "4000000000000002",
                    "expiry": "2512", "service_code": "000"}))
        assert made["ok"] and len(made["response"]["cvv"]) == 3

        raw = await m.hsm_command(m.HsmCommandRequest(
            host="127.0.0.1", port=port, action="raw", command="NC"))
        assert raw["ok"] and raw["response"]["response_code"] == "ND"
    finally:
        await m.stop_simulator(started["id"])


async def test_save_running_simulator_persists_and_keeps_running():
    """An ad-hoc running simulator can be saved into the registry; it keeps
    running under the new saved id and shows on the saved list."""
    m.SIMULATORS.clear()
    m.SIM_SAMPLES.clear()
    m._SIM_PREV.clear()
    m.simulator_store = SimulatorStore(":memory:")

    started = await m.start_simulator(m.SimulatorDraft(
        label="payShield 10K",
        config={"protocol": "payshield", "host": "127.0.0.1", "port": 0},
    ))
    sid, port = started["id"], started["port"]
    assert not started["saved"]
    assert not await m.list_saved_simulators()  # nothing persisted yet

    saved = await m.save_running_simulator(sid, enabled=True)
    cid = saved["id"]
    assert saved["running"] and saved["enabled"] and saved["protocol"] == "payshield"
    # re-keyed: old ad-hoc id is gone, new saved id is the running one
    assert cid in m.SIMULATORS and sid not in m.SIMULATORS
    assert any(s["id"] == cid and s["running"] for s in await m.list_saved_simulators())
    # the relisted port is unchanged — still the same live listener
    assert (await m.get_saved_simulator(cid))["port"] == port

    await m.stop_saved_simulator(cid)


async def test_save_running_simulator_404_when_not_running():
    m.SIMULATORS.clear()
    try:
        await m.save_running_simulator("nope")
        assert False, "expected 404"
    except Exception as exc:  # HTTPException
        assert "404" in str(getattr(exc, "status_code", "")) or "no running" in str(exc)


async def test_hsm_command_unreachable_is_graceful():
    res = await m.hsm_command(m.HsmCommandRequest(host="127.0.0.1", port=1, action="nc"))
    assert res["ok"] is False and res["error"]


def test_responder_for_selects_payshield():
    """_responder_for picks PayShieldSimulator for payshield, TcpResponder else."""
    from worker.adapters.hsm.payshield import PayShieldSimulator
    from worker.adapters.tcp.responder import TcpResponder

    assert isinstance(m._responder_for({"protocol": "payshield"}), PayShieldSimulator)
    assert isinstance(m._responder_for({"kind": "payshield"}), PayShieldSimulator)
    assert isinstance(m._responder_for({"protocol": "iso8583"}), TcpResponder)


async def test_visa_simulator_binds_registry_format_by_default(monkeypatch):
    """A visa config with no message_format_id / inline fields binds visa-base1
    from the registry, injecting its DE table and presence matrix."""
    fake = {"id": "visa-base1", "definition": {
        "fields": {"2": {"name": "PAN", "len_type": "llvar", "length": 19, "type": "n"}},
        "presence": {"0200": {"mandatory": ["2", "4", "11"]}},
    }}
    captured = {}

    async def _fake_get(url):
        captured["url"] = url
        return fake

    monkeypatch.setattr(m, "_http_get_json", _fake_get)

    cfg = await m._resolve_simulator_config({"protocol": "visa", "port": 7010})
    assert captured["url"].endswith("/formats/visa-base1")
    assert cfg["fields"]["2"]["length"] == 19
    assert cfg["validate"]["presence"]["0200"]["mandatory"] == ["2", "4", "11"]
    assert cfg["validate"]["mode"] == "warn"


async def test_inline_fields_skip_default_format_binding(monkeypatch):
    """Inline fields (or an explicit format) take precedence: no registry fetch."""
    called = False

    async def _fake_get(url):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(m, "_http_get_json", _fake_get)

    inline = {"7": {"name": "x", "len_type": "fixed", "length": 10}}
    cfg = await m._resolve_simulator_config({"protocol": "visa", "fields": inline})
    assert cfg["fields"] == inline
    assert called is False


def test_responder_stats_and_reset():
    """TcpResponder tracks MTI / response-code breakdowns and resets them."""
    from worker.adapters.tcp.responder import TcpResponder

    r = TcpResponder({"protocol": "iso8583", "default": {"set": {"39": "00"}}})
    # simulate two requests landing in the counters
    r.received.append({"mti": "0200", "de": {}})
    r.by_mti["0200"] = 2
    r.by_rc["00"] = 2
    stats = r.stats()
    assert stats["by_mti"]["0200"] == 2
    assert stats["by_response_code"]["00"] == 2

    r.reset_stats()
    assert r.stats()["received"] == 0
    assert r.by_mti == {} and r.by_rc == {}


async def test_proxy_simulator_relays_and_reports_outbound_peer():
    """A kind:'proxy' simulator relays to a backend host and surfaces its
    upstream socket as the /peers outbound leg. Since the socket registry
    landed, the test's own client adapter is ALSO (correctly) reported — as
    the ``engine`` outbound leg — so assert per source instead of a total."""
    from worker.adapters.tcp.responder import TcpResponder

    m.SIMULATORS.clear()
    backend = TcpResponder({"protocol": "iso8583",
                            "default": {"echo": ["11", "37"],
                                        "set": {"39": "00", "38": "AUTH99"}}})
    up_port = await backend.start()
    started = await m.start_simulator(m.SimulatorDraft(
        label="proxy-1",
        config={"kind": "proxy", "protocol": "iso8583", "mode": "tap",
                "upstream": {"host": "127.0.0.1", "port": up_port}},
    ))
    sid, pport = started["id"], started["port"]
    assert pport
    adapter = TcpAdapter({"host": "127.0.0.1", "port": pport,
                          "sign_on": {"enabled": False},
                          "reconnect": {"enabled": False}, "response_timeout_sec": 2})
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.response_payload["response_code"] == "00"

        peers = await m.list_peers()
        assert peers["counts"]["inbound"] >= 1
        by_source: dict[str, list[dict]] = {}
        for row in peers["outbound"]:
            assert row["direction"] == "outbound"
            by_source.setdefault(row["source"], []).append(row)
        # the proxy's upstream socket to the backend host
        proxies = by_source.get("proxy") or []
        assert len(proxies) == 1
        assert proxies[0]["source_id"] == sid
        # the test's own client adapter, reported by the socket registry
        engines = by_source.get("engine") or []
        assert any(e["peer"].endswith(f":{pport}") for e in engines)
    finally:
        await adapter.disconnect()
        await m.stop_simulator(sid)
        await backend.stop()
    assert sid not in m.SIMULATORS


async def test_proxy_capture_read_save_and_clear(monkeypatch):
    """Capture endpoints: read redacted rows, save them as a scenario, clear."""
    from worker.adapters.tcp.responder import TcpResponder

    m.SIMULATORS.clear()
    backend = TcpResponder({"protocol": "iso8583",
                            "default": {"echo": ["11", "37"], "set": {"39": "00"}}})
    up_port = await backend.start()
    started = await m.start_simulator(m.SimulatorDraft(
        label="proxy-cap",
        config={"kind": "proxy", "protocol": "iso8583", "mode": "tap",
                "upstream": {"host": "127.0.0.1", "port": up_port}},
    ))
    sid, pport = started["id"], started["port"]
    adapter = TcpAdapter({"host": "127.0.0.1", "port": pport,
                          "sign_on": {"enabled": False},
                          "reconnect": {"enabled": False}, "response_timeout_sec": 2})
    await adapter.connect()
    try:
        await adapter.execute("send_0200", {"amount": 1000, "pan": "4111111111111111"})

        cap = await m.get_capture(sid)
        assert cap["count"] >= 2
        req = next(r for r in cap["rows"] if r["direction"] == "c2u")
        assert req["values"]["2"] == "411111******1111"  # redacted at rest

        # save-as-scenario posts a draft to scenario-service (mocked)
        posted = {}

        async def _fake_post(url, body):
            posted["url"] = url
            posted["body"] = body
            return {"id": "scn_123"}

        monkeypatch.setattr(m, "_http_post_json", _fake_post)
        out = await m.save_capture_as_scenario(
            sid, m.SaveCaptureRequest(name="from-capture"))
        assert out["scenario_id"] == "scn_123" and out["steps"] == 1
        assert posted["url"].endswith("/scenarios")
        assert posted["body"]["scenario"]["steps"][0]["action"] == "send_message"

        await m.clear_capture(sid)
        assert (await m.get_capture(sid))["count"] == 0
    finally:
        await adapter.disconnect()
        await m.stop_simulator(sid)
        await backend.stop()


async def test_capture_endpoint_rejects_non_proxy():
    import pytest
    from fastapi import HTTPException

    m.SIMULATORS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="plain", config={"protocol": "iso8583",
                               "default": {"set": {"39": "00"}}}))
    sid = started["id"]
    try:
        with pytest.raises(HTTPException) as exc:
            await m.get_capture(sid)
        assert exc.value.status_code == 400
    finally:
        await m.stop_simulator(sid)


# -- live chaos dial + storms -------------------------------------------------


async def _plain_sim() -> str:
    m.SIMULATORS.clear()
    m.CHAOS_STORMS.clear()
    started = await m.start_simulator(m.SimulatorDraft(
        label="chaos-sim",
        config={"protocol": "iso8583", "default": {"echo": ["11"], "set": {"39": "00"}}},
    ))
    return started["id"]


async def test_set_and_read_live_chaos():
    sid = await _plain_sim()
    try:
        status = await m.get_simulator_chaos(sid)
        assert status["chaos"] == {} and status["storm"] is None

        applied = await m.set_simulator_chaos(sid, {"drop_pct": 20})
        assert applied["chaos"] == {"drop_pct": 20}
        # the live responder actually carries it
        assert m.SIMULATORS[sid].global_chaos == {"drop_pct": 20}

        # empty body turns it off
        off = await m.set_simulator_chaos(sid, {})
        assert off["chaos"] == {}
    finally:
        await m.stop_simulator(sid)


async def test_set_chaos_rejects_unknown_keys():
    import pytest
    from fastapi import HTTPException

    sid = await _plain_sim()
    try:
        with pytest.raises(HTTPException) as exc:
            await m.set_simulator_chaos(sid, {"drop_pct": 10, "bogus": 1})
        assert exc.value.status_code == 400
    finally:
        await m.stop_simulator(sid)


async def test_set_chaos_rejects_bad_malformed_mode():
    import pytest
    from fastapi import HTTPException

    sid = await _plain_sim()
    try:
        with pytest.raises(HTTPException) as exc:
            await m.set_simulator_chaos(sid, {"malformed_pct": 100, "malformed_mode": "nope"})
        assert exc.value.status_code == 400
    finally:
        await m.stop_simulator(sid)


async def test_chaos_storm_runs_phases_and_restores_baseline():
    import asyncio

    sid = await _plain_sim()
    try:
        # a baseline chaos block should come back after the storm ends
        await m.set_simulator_chaos(sid, {"latency_ms": 10})
        started = await m.start_chaos_storm(
            sid,
            m.ChaosStormRequest(phases=[
                m.ChaosPhase(duration_s=0.15, chaos={"drop_pct": 100}, label="outage"),
            ]),
        )
        assert started["storm"]["active"]
        # mid-storm the live block is the phase's, not the baseline
        await asyncio.sleep(0.05)
        assert m.SIMULATORS[sid].global_chaos == {"drop_pct": 100}
        assert sid in m.CHAOS_STORMS
        # after it finishes, the baseline is restored and the storm is cleared
        await asyncio.sleep(0.2)
        assert sid not in m.CHAOS_STORMS
        assert m.SIMULATORS[sid].global_chaos == {"latency_ms": 10}
    finally:
        await m.stop_simulator(sid)


async def test_chaos_storm_cancel_restores_baseline():
    import asyncio
    from fastapi import Response

    sid = await _plain_sim()
    try:
        await m.start_chaos_storm(
            sid,
            m.ChaosStormRequest(
                phases=[m.ChaosPhase(duration_s=5, chaos={"drop_pct": 100})],
            ),
        )
        await asyncio.sleep(0.05)
        assert sid in m.CHAOS_STORMS
        resp = await m.cancel_chaos_storm(sid)
        assert isinstance(resp, Response) and resp.status_code == 204
        assert sid not in m.CHAOS_STORMS
        # baseline was empty, so chaos is back to off
        assert m.SIMULATORS[sid].global_chaos == {}
    finally:
        await m.stop_simulator(sid)


async def test_manual_chaos_cancels_running_storm():
    import asyncio

    sid = await _plain_sim()
    try:
        await m.start_chaos_storm(
            sid,
            m.ChaosStormRequest(phases=[m.ChaosPhase(duration_s=5, chaos={"drop_pct": 100})]),
        )
        await asyncio.sleep(0.05)
        assert sid in m.CHAOS_STORMS
        # a manual dial move takes precedence and cancels the storm
        out = await m.set_simulator_chaos(sid, {"latency_ms": 50})
        assert out["storm"] is None
        assert sid not in m.CHAOS_STORMS
        assert m.SIMULATORS[sid].global_chaos == {"latency_ms": 50}
    finally:
        await m.stop_simulator(sid)


# -- stuck load-run reconciliation --------------------------------------------


async def test_reconcile_stuck_load_runs():
    from orchestrator.api import main as m
    from orchestrator.api.run_store import RunStore

    m.run_store = RunStore(":memory:")
    m.load_coordinator.runs.clear()

    # a load run stranded as "running" with no live coordinator (crash/restart)
    m.run_store.create("stuck1", "staging", 1, label="load:ghost")
    m.run_store.mark_running("stuck1")
    # a finished load run must be left alone
    m.run_store.create("done1", "staging", 1, label="load:ok")
    m.run_store.finish("done1", "completed", {"received": 10})
    # a non-load stuck run must be left alone (scoped to load: prefix)
    m.run_store.create("func1", "staging", 1, label="")
    m.run_store.mark_running("func1")

    res = await m.reconcile_load_runs()
    assert res["count"] == 1 and res["reconciled"] == ["stuck1"]
    assert m.run_store.get("stuck1")["status"] == "interrupted"
    assert m.run_store.get("done1")["status"] == "completed"
    assert m.run_store.get("func1")["status"] == "running"  # untouched


async def test_reconcile_skips_live_runs():
    from orchestrator.api import main as m
    from orchestrator.api.run_store import RunStore

    m.run_store = RunStore(":memory:")
    m.load_coordinator.runs.clear()

    m.run_store.create("live1", "staging", 1, label="load:busy")
    m.run_store.mark_running("live1")

    class _Live:
        status = "running"
    m.load_coordinator.runs["live1"] = _Live()
    try:
        res = await m.reconcile_load_runs()
        assert res["count"] == 0
        assert m.run_store.get("live1")["status"] == "running"
    finally:
        m.load_coordinator.runs.pop("live1", None)


async def test_stop_reconciles_a_stranded_run():
    from orchestrator.api import main as m
    from orchestrator.api.run_store import RunStore

    m.run_store = RunStore(":memory:")
    m.load_coordinator.runs.clear()

    m.run_store.create("orphan", "staging", 1, label="load:x")
    m.run_store.mark_running("orphan")
    out = await m.stop_load_run("orphan")
    assert out["status"] == "interrupted"
    assert m.run_store.get("orphan")["status"] == "interrupted"
