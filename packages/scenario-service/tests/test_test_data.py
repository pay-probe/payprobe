"""TestDataStore: CRUD + slugging, BIN→PAN generation, key masking/encryption."""
import json

from api.crypto import SecretBox
from api.test_data_store import (
    BinRangeDraft,
    CardPoolDraft,
    KeyDraft,
    TerminalPoolDraft,
    TestDataStore,
    luhn_check_digit,
)


def _luhn_valid(pan: str) -> bool:
    return luhn_check_digit(pan[:-1]) == pan[-1]


def test_card_pool_round_trip_and_slug():
    s = TestDataStore(":memory:")
    s.upsert_card_pool("Visa Goods", CardPoolDraft(cards=["4111111111111111"], description="x"))
    pools = s.list_card_pools()
    assert [p.name for p in pools] == ["visa_goods"]  # slugged
    got = s.get_card_pool("visa_goods")
    assert got.cards == ["4111111111111111"] and got.description == "x"
    s.delete_card_pool("visa_goods")
    assert s.list_card_pools() == []


def test_card_pool_accepts_structured_records():
    s = TestDataStore(":memory:")
    rec = {"pan": "4111111111111111", "expiry": "2609", "cvv": "321", "type": "visa"}
    s.upsert_card_pool("structured", CardPoolDraft(cards=[rec, "5500000000000004"]))
    got = s.get_card_pool("structured")
    assert got.cards[0] == rec  # structured entry preserved verbatim
    assert got.cards[1] == "5500000000000004"  # bare PAN still allowed


def test_terminal_pool_round_trip():
    s = TestDataStore(":memory:")
    s.upsert_terminal_pool("lane_1", TerminalPoolDraft(terminals=["T0001", "T0002"]))
    assert s.get_terminal_pool("lane_1").terminals == ["T0001", "T0002"]


def test_bin_range_generate_produces_luhn_valid_pans():
    s = TestDataStore(":memory:")
    s.upsert_bin_range("visa_test", BinRangeDraft(bin="411111", length=16, brand="visa"))
    pans = s.generate_pans("visa_test", 25, seed=42)
    assert len(pans) == 25
    assert all(len(p) == 16 and p.startswith("411111") for p in pans)
    assert all(_luhn_valid(p) for p in pans)


def test_bin_generate_as_pool_creates_card_pool():
    s = TestDataStore(":memory:")
    s.upsert_bin_range("mc_test", BinRangeDraft(bin="555555", length=16))
    s.generate_pans("mc_test", 5, seed=1, as_pool="mc_cards")
    pool = s.get_card_pool("mc_cards")
    assert pool is not None and len(pool.cards) == 5


def test_key_value_masked_on_read_but_recoverable_internally():
    s = TestDataStore(":memory:")
    s.upsert_key("bdk_primary", KeyDraft(type="bdk", value="0123456789ABCDEF0123456789ABCDEF"))
    view = s.get_key_view("bdk_primary")
    # masked: type/length/fingerprint only, no material
    assert view.type == "bdk" and view.length == 16 and view.fingerprint
    assert not hasattr(view, "value")
    # internal resolver can still recover plaintext
    assert s.get_key_value("bdk_primary") == "0123456789ABCDEF0123456789ABCDEF"


def test_key_update_without_value_keeps_existing():
    s = TestDataStore(":memory:")
    s.upsert_key("pvk", KeyDraft(type="pvk", value="AABBCCDDEEFF0011"))
    s.upsert_key("pvk", KeyDraft(type="pvk", value="", description="updated"))
    assert s.get_key_value("pvk") == "AABBCCDDEEFF0011"
    assert s.get_key_view("pvk").description == "updated"


def test_keys_encrypted_at_rest(tmp_path):
    path = tmp_path / "test_data.json"
    box = SecretBox(SecretBox.generate_key())
    s = TestDataStore(str(path), secret_box=box)
    s.upsert_key("zmk", KeyDraft(type="zmk", value="0123456789ABCDEF"))
    on_disk = json.loads(path.read_text())
    stored = on_disk["keys"]["zmk"]["value"]
    assert stored.startswith("enc:v1:") and "0123456789ABCDEF" not in stored
    # a fresh store with the same key decrypts back to plaintext in memory
    s2 = TestDataStore(str(path), secret_box=box)
    assert s2.get_key_value("zmk") == "0123456789ABCDEF"
