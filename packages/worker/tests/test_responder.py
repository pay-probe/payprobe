"""TcpResponder (host simulator) driven by a real TcpAdapter client."""

from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.responder import TcpResponder


async def _adapter(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "sign_on": {"enabled": False},
        "response_timeout_sec": 2,
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    a = TcpAdapter(cfg)
    await a.connect()
    return a


async def _run(responder_cfg, adapter_kw=None):
    r = TcpResponder(responder_cfg)
    port = await r.start()
    a = await _adapter(port, **(adapter_kw or {}))
    return r, a, port


ISO_RULES = {
    "protocol": "iso8583",
    "rules": [
        {
            "when": {"mti": "0200", "de": {"4": {"gte": "000000100000"}}},
            "respond": {"echo": ["11"], "set": {"39": "61"}},
        },  # over-limit
        {
            "when": {"de": {"2": {"prefix": "400000"}}},
            "respond": {"echo": ["11"], "set": {"39": "05"}},
        },  # BIN block
    ],
    "default": {"echo": ["11", "37"], "set": {"39": "00", "38": "AUTH99"}},
}


async def test_default_rule_approves_and_echoes_stan():
    r, a, _ = await _run(ISO_RULES)
    try:
        res = await a.execute("send_0200", {"amount": 1000, "pan": "5111111111111111"})
        assert res.success
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["auth_code"] == "AUTH99"
        assert res.response_payload["stan"]  # STAN echoed back (correlation worked)
    finally:
        await a.disconnect()
        await r.stop()


async def test_amount_rule_declines_over_limit():
    r, a, _ = await _run(ISO_RULES)
    try:
        res = await a.execute("send_0200", {"amount": 100000, "pan": "5111111111111111"})
        assert res.response_payload["response_code"] == "61"  # over-limit decline
    finally:
        await a.disconnect()
        await r.stop()


async def test_bin_rule_declines_specific_pan():
    r, a, _ = await _run(ISO_RULES)
    try:
        res = await a.execute("send_0200", {"amount": 1000, "pan": "4000001234567899"})
        assert res.response_payload["response_code"] == "05"  # BIN block
    finally:
        await a.disconnect()
        await r.stop()


async def test_drop_rule_causes_client_timeout():
    cfg = {
        "protocol": "iso8583",
        "rules": [{"when": {"mti": "0200"}, "respond": {"drop": True}}],
        "default": {"set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg, adapter_kw={"response_timeout_sec": 0.3})
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success is False
        assert "No response" in (res.error or "")
    finally:
        await a.disconnect()
        await r.stop()


async def test_delay_rule_still_answers():
    cfg = {
        "protocol": "iso8583",
        "rules": [
            {
                "when": {"mti": "0200"},
                "respond": {"echo": ["11"], "set": {"39": "00"}, "delay_ms": 50},
            }
        ],
        "default": {"set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg)
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success and res.response_payload["response_code"] == "00"
        assert res.duration_ms >= 40
    finally:
        await a.disconnect()
        await r.stop()


async def test_header_echo_responder():
    cfg = {
        "protocol": "header_echo",
        "header_echo": {"header_bytes": 4},
        "rules": [{"when": {"command": {"eq": "A0"}}, "respond": {"command": "A1", "error": "00"}}],
        "default": {"error": "00"},
    }
    r = TcpResponder(cfg)
    port = await r.start()
    a = await _adapter(port, protocol="header_echo")
    try:
        res = await a.execute("send_command", {"command": "A0", "data": "DEADBEEF"})
        assert res.success
        assert res.response_payload["response_code"] == "00"
        assert res.response_payload["command"] == "A1"
        assert res.response_payload["data"] == "DEADBEEF"
    finally:
        await a.disconnect()
        await r.stop()
