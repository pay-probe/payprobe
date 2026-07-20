"""Per-transaction data generators + their ${...} integration."""

import pytest

from worker.engine.generators import GeneratorContext, GeneratorError, luhn_check_digit
from worker.engine.variables import resolve_value, UnresolvedReferenceError, GEN_KEY


def _luhn_ok(pan: str) -> bool:
    return luhn_check_digit(pan[:-1]) == pan[-1]


def test_seed_is_reproducible():
    a = GeneratorContext(seed="run:0")
    b = GeneratorContext(seed="run:0")
    seq_a = [a.resolve("rand.int(0,1000000)") for _ in range(5)]
    seq_b = [b.resolve("rand.int(0,1000000)") for _ in range(5)]
    assert seq_a == seq_b


def test_seq_increments_and_pads():
    g = GeneratorContext(seed=1)
    assert g.resolve("seq.stan") == 1
    assert g.resolve("seq.stan") == 2
    assert g.resolve("seq.other") == 1  # independent counter
    assert g.resolve("seq.stan(6)") == "000003"


def test_seq_padding_wraps():
    g = GeneratorContext(seed=1)
    for _ in range(999999):
        g.resolve("seq.s")
    assert g.resolve("seq.s(6)") == "000000"  # 1_000_000 mod 1e6


def test_rand_pan_is_luhn_valid_with_bin_and_length():
    g = GeneratorContext(seed=7)
    pan = g.resolve("rand.pan(411111)")
    assert pan.startswith("411111") and len(pan) == 16 and _luhn_ok(pan)
    pan19 = g.resolve("rand.pan(411111,19)")
    assert len(pan19) == 19 and _luhn_ok(pan19)


def test_rand_helpers():
    g = GeneratorContext(seed=3)
    assert 100 <= g.resolve("rand.amount(100,5000)") <= 5000
    assert len(g.resolve("rand.digits(4)")) == 4
    h = g.resolve("rand.hex(8)")
    assert len(h) == 8 and all(c in "0123456789abcdef" for c in h)


def test_uuid_and_rrn_shapes():
    g = GeneratorContext(seed=3)
    assert len(g.resolve("uuid")) == 32
    rrn = g.resolve("now.rrn")
    assert len(rrn) == 12 and rrn.isdigit()
    assert isinstance(g.resolve("now.epoch"), int)


def test_pool_round_robin_and_empty():
    g = GeneratorContext(seed=1, terminals=["T1", "T2"])
    assert [g.resolve("pool.terminal") for _ in range(3)] == ["T1", "T2", "T1"]
    with pytest.raises(GeneratorError):
        g.resolve("pool.card")  # empty pool surfaces loudly


def test_bad_token_raises():
    g = GeneratorContext(seed=1)
    with pytest.raises(GeneratorError):
        g.resolve("rand.nope(1)")
    with pytest.raises(GeneratorError):
        g.resolve("rand.int(1)")  # missing arg


def test_resolve_value_uses_generator_context():
    g = GeneratorContext(seed="run:0")
    ctx = {GEN_KEY: g, "step1": {"response": {"rrn": "ABC"}}}
    # whole-string token preserves type (int), and a back-reference still works
    payload = {
        "stan": "${seq.stan(6)}",
        "amount": "${rand.amount(100,100)}",
        "ref": "txn-${seq.stan}-${step1.response.rrn}",
    }
    out = resolve_value(payload, ctx)
    assert out["stan"] == "000001"
    assert out["amount"] == 100
    assert out["ref"] == "txn-2-ABC"


def test_generator_tokens_without_context_fall_through():
    # No __gen__ in context: a generator-looking token is treated as a step ref
    # and (correctly) fails to resolve rather than silently inventing data.
    with pytest.raises(UnresolvedReferenceError):
        resolve_value("${rand.int(1,2)}", {"step1": {}})


# -- KSN ---------------------------------------------------------------------


def test_ksn_counter_advances_and_uses_base():
    g = GeneratorContext(seed=1)
    assert g.resolve("seq.ksn") == "3039505912345678" + "00000000"
    assert g.resolve("seq.ksn") == "3039505912345678" + "00000001"
    # a custom base keeps its own counter, normalised to upper-hex
    assert g.resolve("seq.ksn(aabbccddeeff0011)") == "AABBCCDDEEFF0011" + "00000000"
    assert g.resolve("seq.ksn") == "3039505912345678" + "00000002"  # base counter untouched


# -- card namespace ----------------------------------------------------------


def test_card_binds_for_a_transaction_and_rotates():
    cards = ["4111111111111111", "5500000000000004"]
    g = GeneratorContext(seed=1, cards=cards)
    g.new_transaction()
    # all reads within one transaction describe the same card
    assert g.resolve("card.pan") == "4111111111111111"
    assert g.resolve("card.brand") == "visa"
    assert g.resolve("card.seq") == 1
    g.new_transaction()
    assert g.resolve("card.pan") == "5500000000000004"
    assert g.resolve("card.brand") == "mastercard"
    g.new_transaction()  # wraps round-robin
    assert g.resolve("card.pan") == "4111111111111111"


def test_card_structured_record_fields_and_track2():
    card = {"pan": "4111111111111111", "expiry": "2609", "cvv": "321", "type": "visa"}
    g = GeneratorContext(seed=1, cards=[card])
    g.new_transaction()
    assert g.resolve("card.expiry") == "2609"
    assert g.resolve("card.cvv") == "321"
    # track2 is synthesised consistently from the same record
    assert g.resolve("card.track2") == "4111111111111111D2609101"


def test_card_string_entry_defaults_and_brand_inference():
    g = GeneratorContext(seed=1, cards=["378282246310005"])  # amex
    g.new_transaction()
    assert g.resolve("card.brand") == "amex"
    assert g.resolve("card.expiry") == "2812"  # default
    assert g.resolve("card.cvv") == "000"  # default


def test_card_binds_lazily_without_new_transaction():
    g = GeneratorContext(seed=1, cards=["4111111111111111"])
    # used standalone (no new_transaction) -> still returns a stable record
    assert g.resolve("card.pan") == "4111111111111111"
    assert g.resolve("card.pan") == "4111111111111111"


def test_card_empty_pool_raises():
    g = GeneratorContext(seed=1)
    with pytest.raises(GeneratorError):
        g.resolve("card.pan")


def test_card_is_a_generator_namespace():
    assert GeneratorContext.is_generator("card.pan")
    assert GeneratorContext.is_generator("seq.ksn")
