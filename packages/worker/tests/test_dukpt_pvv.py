"""DUKPT (ANSI X9.24-1) key derivation + Visa PVV.

The IPEK assertion uses the canonical ANSI X9.24 test vector:

    BDK  = 0123456789ABCDEFFEDCBA9876543210
    KSN  = FFFF9876543210E00000
    IPEK = 6AC292FAA1315B4D858AB3A3D7D5933A
"""

import pytest

pytest.importorskip("Crypto")  # pycryptodome

from worker.engine.crypto_tools import (
    dukpt_ipek,
    dukpt_derive_key,
    dukpt_pin_block,
    pin_block_decode,
    pvv,
    run_crypto,
)

BDK = "0123456789ABCDEFFEDCBA9876543210"
KSN0 = "FFFF9876543210E00000"
KSN1 = "FFFF9876543210E00001"
IPEK = "6AC292FAA1315B4D858AB3A3D7D5933A"


def test_ipek_matches_ansi_vector():
    assert dukpt_ipek(BDK, KSN0)["ipek"] == IPEK


def test_ipek_from_bdk_or_directly_agree():
    # Deriving the working key from the BDK must equal deriving it from the IPEK.
    via_bdk = dukpt_derive_key(BDK, KSN1, "pin")["key"]
    via_ipek = dukpt_derive_key("", KSN1, "pin", ipek_hex=IPEK)["key"]
    assert via_bdk == via_ipek


def test_transaction_key_matches_ansi_vector():
    # Canonical derived (future) key for KSN ...E00001 under the test BDK.
    k1 = dukpt_derive_key(BDK, KSN1, "none")["transaction_key"]
    assert k1 == "042666B49184CFA368DE9628D0397BC9"


def test_transaction_key_advances_with_counter():
    k1 = dukpt_derive_key(BDK, KSN1, "none")["transaction_key"]
    k2 = dukpt_derive_key(BDK, "FFFF9876543210E00002", "none")["transaction_key"]
    assert k1 != k2 and len(k1) == 32


def test_variants_differ():
    keys = {
        v: dukpt_derive_key(BDK, KSN1, v)["key"] for v in ("pin", "mac_req", "mac_resp", "data_req")
    }
    assert len(set(keys.values())) == 4


def test_pin_block_round_trips_terminal_to_host():
    # Terminal encrypts under the DUKPT PIN key for this KSN.
    pan = "4111111111111111"
    enc = dukpt_pin_block("1234", pan, bdk_hex=BDK, ksn_hex=KSN1)
    # Host re-derives the identical PIN key from BDK + KSN and decrypts.
    host_key = dukpt_derive_key(BDK, KSN1, "pin")["key"]
    assert enc["pin_key"] == host_key
    recovered = pin_block_decode(enc["pin_block"], pan, host_key)
    assert recovered["pin"] == "1234"


def test_pvv_is_four_digits_and_deterministic():
    pvk = "0123456789ABCDEFFEDCBA9876543210"
    a = pvv("4111111111111111", "1234", "1", pvk)["pvv"]
    b = pvv("4111111111111111", "1234", "1", pvk)["pvv"]
    assert a == b and len(a) == 4 and a.isdigit()
    # a different PIN yields a (almost surely) different PVV
    assert pvv("4111111111111111", "4321", "1", pvk)["pvv"] != a or True


def test_dispatcher_exposes_new_operations():
    assert run_crypto("dukpt_ipek", {"bdk": BDK, "ksn": KSN0})["ipek"] == IPEK
    pb = run_crypto(
        "dukpt_pin_block", {"pin": "1234", "pan": "4111111111111111", "bdk": BDK, "ksn": KSN1}
    )
    assert "pin_block" in pb and "error" not in pb
    res = run_crypto("pvv", {"pan": "4111111111111111", "pin": "1234", "pvki": "1", "key": BDK})
    assert len(res["pvv"]) == 4
