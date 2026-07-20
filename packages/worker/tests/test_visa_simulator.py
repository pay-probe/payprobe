"""VisaSimulator (VISA Base I-style scheme host) driven by a real TcpAdapter."""

from worker.adapters.scheme.visa import VISA_FIELDS, VisaSimulator
from worker.adapters.tcp.adapter import TcpAdapter
from worker.engine import crypto_tools as ct


async def _adapter(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "fields": VISA_FIELDS,
        "sign_on": {"enabled": False},
        "response_timeout_sec": 2,
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    a = TcpAdapter(cfg)
    await a.connect()
    return a


async def _run(visa_cfg=None, rules=None):
    cfg = {"protocol": "visa", "fields": VISA_FIELDS}
    if visa_cfg is not None:
        cfg["visa"] = visa_cfg
    if rules is not None:
        cfg["rules"] = rules
    r = VisaSimulator(cfg)
    port = await r.start()
    a = await _adapter(port)
    return r, a


async def test_stand_in_approves_authorization():
    r, a = await _run({"stand_in": True})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        assert res.success
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"]  # DE 38 generated on approval
    finally:
        await a.disconnect()
        await r.stop()


async def test_amount_limit_declines_61():
    r, a = await _run({"decline_over": "000000100000"})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000100000"}}
        )
        assert res.response_payload["response_code"] == "61"
    finally:
        await a.disconnect()
        await r.stop()


async def test_blocked_bin_declines_05():
    r, a = await _run({"block_bins": ["400000"], "block_response": "05"})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4000001234567899", "4": "000000001000"}}
        )
        assert res.response_payload["response_code"] == "05"
    finally:
        await a.disconnect()
        await r.stop()


async def test_expired_card_declines_54():
    r, a = await _run({"expiry_check": True})
    try:
        # DE 14 = YYMM in the past (Jan 2001).
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000", "14": "0101"}}
        )
        assert res.response_payload["response_code"] == "54"
    finally:
        await a.disconnect()
        await r.stop()


async def test_network_management_echo_test():
    r, a = await _run()
    try:
        res = await a.execute("send_0800", {"values": {"70": "301"}})
        assert res.response_payload["mti"] == "0810"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["fields"].get("70") == "301"  # DE 70 echoed
    finally:
        await a.disconnect()
        await r.stop()


async def test_reversal_advice_acknowledged():
    r, a = await _run()
    try:
        res = await a.execute(
            "send_0420", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        assert res.response_payload["mti"] == "0430"
        assert res.response_payload["response_code"] == "00"
    finally:
        await a.disconnect()
        await r.stop()


async def test_explicit_rule_overrides_visa_default():
    rules = [
        {
            "when": {"de": {"2": {"prefix": "411111"}}},
            "respond": {"echo": ["11"], "set": {"39": "01"}},
        }
    ]
    r, a = await _run({"stand_in": True}, rules=rules)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "4111110000001111", "4": "000000001000"}}
        )
        assert (
            res.response_payload["response_code"] == "01"
        )  # refer-to-issuer, not stand-in approve
    finally:
        await a.disconnect()
        await r.stop()


async def test_no_stand_in_returns_issuer_unavailable():
    r, a = await _run({"stand_in": False})
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": "5555550000001111", "4": "000000001000"}}
        )
        assert res.response_payload["response_code"] == "91"
    finally:
        await a.disconnect()
        await r.stop()


async def test_cvv2_verification_match_and_mismatch():
    pan, expiry, sc, cvk = "4999990000001234", "2512", "000", "0123456789ABCDEFFEDCBA9876543210"
    good = ct.cvv(pan, expiry, sc, cvk)["cvv"]
    visa_cfg = {"verify_cvv2": {"field": "48", "cvk": cvk, "service_code": sc, "decline": "N7"}}

    # matching CVV2 -> approved
    r, a = await _run(visa_cfg)
    try:
        res = await a.execute("send_0200", {"values": {"2": pan, "14": expiry, "48": good}})
        assert res.response_payload["response_code"] == "00"
    finally:
        await a.disconnect()
        await r.stop()

    # wrong CVV2 -> declined N7
    r, a = await _run(visa_cfg)
    try:
        res = await a.execute("send_0200", {"values": {"2": pan, "14": expiry, "48": "999"}})
        assert res.response_payload["response_code"] == "N7"
    finally:
        await a.disconnect()
        await r.stop()
