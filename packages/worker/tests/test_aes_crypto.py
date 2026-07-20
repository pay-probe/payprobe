"""AES payment crypto — validated against published reference vectors.

* AES-CMAC against the RFC 4493 test vectors.
* ISO 9564-1 format-4 PIN block round-trip + a fixed decode known-answer test.
* AES DUKPT (ANSI X9.24-3) against the official X9.24-3-2017 test vectors
  (BDK FEDCBA9876543210F1F1F1F1F1F1F1F1, initial-key id 1234567890123456):
  the initial key, the per-transaction derivation key, and the PIN / MAC /
  data working keys for the first two transaction counters.

These require pycryptodome (an environmental dependency, per CLAUDE.md).
"""

from worker.engine import crypto_tools as ct

# -- AES-CMAC (RFC 4493) -----------------------------------------------------

_CMAC_KEY = "2B7E151628AED2A6ABF7158809CF4F3C"
_CMAC_MSG = (
    "6BC1BEE22E409F96E93D7E117393172A"
    "AE2D8A571E03AC9C9EB76FAC45AF8E51"
    "30C81C46A35CE411E5FBC1191A0A52EF"
    "F69F2445DF4F9B17AD2B417BE66C3710"
)


def test_aes_cmac_rfc4493_vectors():
    cases = {
        0: "BB1D6929E95937287FA37D129B756746",
        16: "070A16B46B4D4144F79BDD9DD04A287C",
        40: "DFA66747DE9AE63030CA32611497C827",
        64: "51F0BEBF7E3B9D92FC49741779363CFE",
    }
    for nbytes, expected in cases.items():
        assert ct.aes_cmac(_CMAC_KEY, _CMAC_MSG[: nbytes * 2])["mac"] == expected


# -- ISO 9564-1 format-4 PIN block -------------------------------------------


def test_iso4_pin_block_round_trip_all_lengths():
    key = "00112233445566778899AABBCCDDEEFF"
    pan = "1234567890123456"
    for pin in ("1234", "12345", "123456789012"):
        block = ct.pin_block_encode(pin, pan, key, "4")["pin_block"]
        assert len(block) == 32  # 16-byte AES block
        assert ct.pin_block_decode(block, pan, key, "4")["pin"] == pin


def test_iso4_pin_block_randomises_pad():
    key, pan, pin = "00112233445566778899AABBCCDDEEFF", "1234567890123456", "1234"
    a = ct.pin_block_encode(pin, pan, key, "4")["pin_block"]
    b = ct.pin_block_encode(pin, pan, key, "4")["pin_block"]
    assert a != b  # random pad -> different blocks, both decode back
    assert ct.pin_block_decode(a, pan, key, "4")["pin"] == pin
    assert ct.pin_block_decode(b, pan, key, "4")["pin"] == pin


def test_iso4_pin_block_decode_known_answer():
    # A fixed valid ISO-4 block (the pad is irrelevant to decoding).
    key, pan = "0123456789ABCDEF0123456789ABCDEF", "43219876543210987"
    block = "D3B650140FE182DA3D814D7F52615064"
    assert ct.pin_block_decode(block, pan, key, "4")["pin"] == "87654321"


# -- AES DUKPT (ANSI X9.24-3) ------------------------------------------------

_BDK = "FEDCBA9876543210F1F1F1F1F1F1F1F1"
_IKID = "1234567890123456"
_IK = "1273671EA26AC29AFA4D1084127652A1"


def test_aes_dukpt_initial_key_vector():
    assert ct.aes_dukpt_initial_key(_BDK, _IKID)["ik"] == _IK


def test_aes_dukpt_working_keys_x9243_vectors():
    # (counter, variant) -> expected key (or transaction/derivation key).
    expected = {
        (1, "txn"): "4F21B565BAD9835E112B6465635EAE44",
        (1, "pin"): "AF8CB133A78F8DC2D1359F18527593FB",
        (1, "mac_gen"): "A2DC23DE6FDE0824A2BC321E08E4B8B7",
        (1, "data_enc"): "A35C412EFD41FDB98B69797C02DCD08F",
        (2, "txn"): "2F34D68DE10F68D38091A73B9E7C437C",
        (2, "pin"): "D30BDC73EC9714B000BEC66BDB7B6D09",
    }
    for (counter, variant), want in expected.items():
        ksn = _IKID + f"{counter:08X}"
        result = ct.aes_dukpt_derive_key(ksn, "pin" if variant == "txn" else variant, bdk_hex=_BDK)
        got = result["transaction_key"] if variant == "txn" else result["key"]
        assert got == want, (counter, variant, got)


def test_aes_dukpt_host_and_terminal_paths_agree():
    ksn = _IKID + "00000005"
    from_bdk = ct.aes_dukpt_derive_key(ksn, "pin", bdk_hex=_BDK)["key"]
    from_ik = ct.aes_dukpt_derive_key(ksn, "pin", ik_hex=_IK)["key"]
    assert from_bdk == from_ik


def test_aes_dukpt_pin_block_decodes_under_derived_key():
    ksn = _IKID + "0000000A"
    out = ct.aes_dukpt_pin_block("2468", "1234567890123456", ksn, bdk_hex=_BDK)
    assert (
        ct.pin_block_decode(out["pin_block"], "1234567890123456", out["pin_key"], "4")["pin"]
        == "2468"
    )
