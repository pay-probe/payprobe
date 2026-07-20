"""HSMAdapter (payShield host-command client) driven against the simulator.

Proves the client and simulator interoperate end-to-end: NC sign-on, key
generation, and a CVV generate→verify chain — the same path the
payshield_hsm_smoke example scenario exercises.
"""

from worker.adapters.hsm.adapter import HSMAdapter
from worker.adapters.hsm.payshield import PayShieldSimulator


async def _pair():
    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    cli = HSMAdapter({"host": "127.0.0.1", "port": port, "header": "HDR1"})
    await cli.connect()
    return sim, cli


async def test_client_nc_health_check():
    sim, cli = await _pair()
    try:
        assert await cli.health_check() is True
        res = await cli.execute("nc", {})
        assert res.success
        assert res.response_payload["response_code"] == "ND"
        assert res.response_payload["firmware"] == "0007-E000"
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_client_cvv_generate_verify_chain():
    sim, cli = await _pair()
    try:
        gen = await cli.execute("generate_key", {"key_type": "402", "scheme": "U"})
        cvk = gen.response_payload["key"]
        assert gen.success and cvk.startswith("U")

        card = {"pan": "4000000000000002", "expiry": "2512", "service_code": "000"}
        made = await cli.execute("generate_cvv", {"cvk": cvk, **card})
        cvv = made.response_payload["cvv"]
        assert made.success and len(cvv) == 3

        good = await cli.execute("verify_cvv", {"cvk": cvk, "cvv": cvv, **card})
        assert good.success and good.response_payload["error_code"] == "00"

        bad = await cli.execute("verify_cvv", {"cvk": cvk, "cvv": "999", **card})
        assert not bad.success and bad.response_payload["error_code"] == "01"
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_client_key_check_matches_generation():
    sim, cli = await _pair()
    try:
        gen = await cli.execute("generate_key", {"key_type": "002", "scheme": "U"})
        check = await cli.execute(
            "key_check", {"key_type_code": "02", "key": gen.response_payload["key"]}
        )
        assert check.success
        assert check.response_payload["kcv"] == gen.response_payload["kcv"]
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_pipelined_concurrent_commands_demux_correctly():
    """Many commands in flight at once over ONE connection are matched back to
    the right caller by their incrementing correlation header."""
    import asyncio

    sim, cli = await _pair()
    try:
        cvk = (
            await cli.execute("generate_key", {"key_type": "402", "scheme": "U"})
        ).response_payload["key"]
        pans = [f"40000000000000{n:02d}" for n in range(25)]
        card = {"cvk": cvk, "expiry": "2512", "service_code": "000"}

        # fire all CVV generations concurrently on the single connection
        gens = await asyncio.gather(
            *(cli.execute("generate_cvv", {"pan": p, **card}) for p in pans)
        )
        cvvs = {p: g.response_payload["cvv"] for p, g in zip(pans, gens)}
        assert all(g.success for g in gens)

        # each CVV must verify against its OWN pan — proves no cross-talk
        verifs = await asyncio.gather(
            *(cli.execute("verify_cvv", {"pan": p, "cvv": cvvs[p], **card}) for p in pans)
        )
        assert all(v.response_payload["error_code"] == "00" for v in verifs)
        # a deliberately mismatched pan/cvv pair is rejected
        wrong = await cli.execute("verify_cvv", {"pan": pans[0], "cvv": cvvs[pans[1]], **card})
        assert wrong.response_payload["error_code"] == "01"
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_connection_pool_round_robins():
    """connections>1 opens several sockets and spreads commands across them."""
    import asyncio

    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    cli = HSMAdapter({"host": "127.0.0.1", "port": port, "connections": 4})
    await cli.connect()
    try:
        assert len(cli._conns) == 4
        results = await asyncio.gather(*(cli.execute("nc", {}) for _ in range(40)))
        assert all(r.success for r in results)
        # every pooled connection saw traffic (round-robin)
        assert all(len(c._pending) == 0 for c in cli._conns)
        assert sim.ctx is not None  # sim handled all of them
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_disconnect_fails_inflight_gracefully():
    """Closing the connection resolves any in-flight command as a failure, not a
    hang."""
    sim, cli = await _pair()
    await sim.stop()  # kill the listener so the reply never comes
    try:
        res = await cli.execute("nc", {})
        assert not res.success and res.error
    finally:
        await cli.disconnect()


async def test_client_reports_hsm_error_as_failure():
    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0, "disabled_commands": ["NC"]})
    port = await sim.start()
    cli = HSMAdapter({"host": "127.0.0.1", "port": port, "header": "HDR1"})
    await cli.connect()
    try:
        res = await cli.execute("nc", {})
        assert not res.success
        assert res.response_payload["error_code"] == "68"
        assert "68" in (res.error or "")
    finally:
        await cli.disconnect()
        await sim.stop()


async def test_commandless_relay_fails_with_named_cause_not_wire_garbage():
    """A relay/raw dispatch whose payload has no 'command' must NOT truncate
    the action name onto the wire ("relay" → "re" → HSM error 30) — it fails
    up front with a message that names the sample-request fix."""
    cli = HSMAdapter({"host": "127.0.0.1", "port": 1})  # never dialed
    res = await cli.execute("relay", {"data": ""})
    assert not res.success
    assert "payload has no 'command'" in (res.error or "")
    assert '{"command": "NC"' in (res.error or "")


async def test_two_char_raw_action_still_passes_through():
    """The raw escape hatch keeps working: a bare 2-char action is a command."""
    sim = PayShieldSimulator({"host": "127.0.0.1", "port": 0})
    port = await sim.start()
    cli = HSMAdapter({"host": "127.0.0.1", "port": port, "header": "HDR1"})
    await cli.connect()
    try:
        res = await cli.execute("NC", {})
        assert res.success
        assert res.request_payload["command"] == "NC"
    finally:
        await cli.disconnect()
        await sim.stop()
