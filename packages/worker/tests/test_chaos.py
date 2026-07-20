"""Chaos / fault injection: ChaosEngine unit tests + responder integration."""

from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.chaos import ChaosEngine
from worker.adapters.tcp.responder import TcpResponder

# -- ChaosEngine unit tests --------------------------------------------------


def test_drop_pct_bounds_are_deterministic():
    eng = ChaosEngine()
    assert eng.plan({"drop_pct": 100}).drop is True
    assert eng.plan({"drop_pct": 0}).drop is False
    assert eng.plan({"drop": True}).drop is True
    assert eng.plan(None).active is False
    assert eng.plan({}).active is False


def test_seed_makes_rolls_reproducible():
    cfg = {"drop_pct": 50}
    a = ChaosEngine(seed=42)
    b = ChaosEngine(seed=42)
    seq_a = [a.plan(cfg).drop for _ in range(20)]
    seq_b = [b.plan(cfg).drop for _ in range(20)]
    assert seq_a == seq_b
    assert any(seq_a) and not all(seq_a)  # the 50% actually varies


def test_latency_flat_and_jitter_range():
    eng = ChaosEngine(seed=1)
    assert eng.plan({"latency_ms": 120}).latency_ms == 120.0
    for _ in range(50):
        ms = eng.plan({"latency_ms": {"min": 50, "max": 200}}).latency_ms
        assert 50.0 <= ms <= 200.0


def test_latency_pct_gate_can_skip():
    eng = ChaosEngine(seed=7)
    # 0% chance -> never adds latency
    assert all(
        eng.plan({"latency_ms": {"min": 10, "max": 20, "pct": 0}}).latency_ms == 0.0
        for _ in range(10)
    )


def test_malformed_mode_falls_back_to_garbage():
    eng = ChaosEngine()
    assert eng.plan({"malformed_pct": 100, "malformed_mode": "bogus"}).malformed == "garbage"
    assert eng.plan({"malformed_pct": 100, "malformed_mode": "bad_mti"}).malformed == "bad_mti"


def test_corrupt_garbage_keeps_framing_changes_body():
    eng = ChaosEngine(seed=3)
    frame = (12).to_bytes(2, "big") + b"0210AUTH00ABCD"
    out = eng.corrupt(frame, "garbage", prefix_bytes=2)
    assert out[:2] == frame[:2]  # length prefix untouched
    assert len(out) == len(frame)  # framing length unchanged
    assert out[2:] != frame[2:]  # body corrupted


def test_corrupt_bad_length_changes_prefix_only():
    frame = (12).to_bytes(2, "big") + b"0210AUTH00ABCD"
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=2)
    assert out[:2] != frame[:2]
    assert out[2:] == frame[2:]
    assert int.from_bytes(out[:2], "big") > 12  # client waits for bytes that never come


def test_corrupt_bad_mti_rewrites_message_type():
    frame = (12).to_bytes(2, "big") + b"02100000ABCD"
    out = ChaosEngine().corrupt(frame, "bad_mti", prefix_bytes=2)
    assert out[2:6] == b"9999"


def test_corrupt_flip_bits_same_length_different_body():
    frame = (12).to_bytes(2, "big") + b"0210AUTH00ABCD"
    out = ChaosEngine(seed=9).corrupt(frame, "flip_bits", prefix_bytes=2)
    assert len(out) == len(frame)
    assert out != frame


def test_partial_len():
    eng = ChaosEngine()
    frame = b"0123456789"
    assert eng.partial_len(frame, 4) == 4
    assert eng.partial_len(frame, 999) == len(frame)
    assert eng.partial_len(frame, "half") == 5
    assert eng.partial_len(frame, None) == 5


# -- responder integration (real adapter client) -----------------------------


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


async def test_chaos_drop_causes_client_timeout():
    cfg = {
        "chaos": {"drop_pct": 100},
        "default": {"echo": ["11"], "set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg, adapter_kw={"response_timeout_sec": 0.3})
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success is False
        assert r.faults == 1
    finally:
        await a.disconnect()
        await r.stop()


async def test_chaos_latency_delays_but_answers():
    cfg = {
        "chaos": {"latency_ms": {"min": 60, "max": 80}},
        "default": {"echo": ["11"], "set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg)
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success and res.response_payload["response_code"] == "00"
        assert res.duration_ms >= 50
        assert r.faults == 1
    finally:
        await a.disconnect()
        await r.stop()


async def test_chaos_partial_read_breaks_client():
    cfg = {
        "chaos": {"partial_pct": 100, "partial_bytes": "half"},
        "default": {"echo": ["11"], "set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg, adapter_kw={"response_timeout_sec": 0.5})
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success is False  # truncated frame -> read error / timeout
        assert r.faults == 1
    finally:
        await a.disconnect()
        await r.stop()


async def test_chaos_malformed_garbage_breaks_client():
    cfg = {
        "chaos": {"malformed_pct": 100, "malformed_mode": "garbage"},
        "default": {"echo": ["11"], "set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg, adapter_kw={"response_timeout_sec": 0.5})
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success is False  # unparseable / uncorrelated reply
        assert r.faults == 1
    finally:
        await a.disconnect()
        await r.stop()


async def test_per_rule_chaos_overrides_global():
    # No global chaos; a single rule injects a guaranteed drop.
    cfg = {
        "chaos": {},
        "rules": [
            {"when": {"mti": "0200"}, "respond": {"set": {"39": "00"}, "chaos": {"drop_pct": 100}}},
        ],
        "default": {"echo": ["11"], "set": {"39": "00"}},
    }
    r, a, _ = await _run(cfg, adapter_kw={"response_timeout_sec": 0.3})
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success is False
        assert r.faults == 1
    finally:
        await a.disconnect()
        await r.stop()


async def test_no_chaos_block_is_normal():
    cfg = {"default": {"echo": ["11"], "set": {"39": "00"}}}
    r, a, _ = await _run(cfg)
    try:
        res = await a.execute("send_0200", {"amount": 1000})
        assert res.success and res.response_payload["response_code"] == "00"
        assert r.faults == 0
    finally:
        await a.disconnect()
        await r.stop()


# -- live chaos dial (set_chaos) ---------------------------------------------


def test_set_chaos_swaps_the_live_block():
    r = TcpResponder({"default": {"set": {"39": "00"}}})
    assert r.global_chaos == {}
    applied = r.set_chaos({"drop_pct": 25})
    assert applied == {"drop_pct": 25}
    assert r.global_chaos == {"drop_pct": 25}
    # kept in sync on the config so save-as-simulator persists what runs
    assert r.config["chaos"] == {"drop_pct": 25}
    # empty turns it back off
    r.set_chaos({})
    assert r.global_chaos == {}


def test_set_chaos_seed_restarts_rng_for_reproducibility():
    r1 = TcpResponder({})
    r1.set_chaos({"drop_pct": 50, "seed": 99})
    seq1 = [r1.chaos.plan({"drop_pct": 50}).drop for _ in range(20)]
    r2 = TcpResponder({})
    r2.set_chaos({"drop_pct": 50, "seed": 99})
    seq2 = [r2.chaos.plan({"drop_pct": 50}).drop for _ in range(20)]
    assert seq1 == seq2


async def test_set_chaos_takes_effect_on_next_reply():
    r, a, _ = await _run(
        {"default": {"echo": ["11"], "set": {"39": "00"}}},
        adapter_kw={"response_timeout_sec": 0.4},
    )
    try:
        ok = await a.execute("send_0200", {"amount": 1000})
        assert ok.success and r.faults == 0
        # dial faults up mid-session — no restart
        r.set_chaos({"drop_pct": 100})
        bad = await a.execute("send_0200", {"amount": 1000})
        assert bad.success is False
        assert r.faults == 1
        # dial back off
        r.set_chaos({})
        again = await a.execute("send_0200", {"amount": 1000})
        assert again.success and again.response_payload["response_code"] == "00"
    finally:
        await a.disconnect()
        await r.stop()


def test_http_responder_has_set_chaos():
    # the HTTP responder exposes the same duck-typed dial
    from worker.adapters.http.responder import HttpResponder

    h = HttpResponder({})
    applied = h.set_chaos({"latency_ms": 120})
    assert applied == {"latency_ms": 120}
    assert h.global_chaos == {"latency_ms": 120}
