"""TR-31 version B key blocks: the published example opens, and what is sealed
opens again under the same key and nowhere else."""

import pytest

from worker.adapters.hsm import key_block

# The ANSI X9 TR-31 example of a version B block: a PIN encryption key (P0,
# encrypt only) under its key block protection key, with one optional block.
EXAMPLE_KBPK = bytes.fromhex("89E88CF7931444F334BD7547FC3F380C")
EXAMPLE_KEY = "F039121BEC83D26B169BDCD5B22AAF8F"
EXAMPLE_BLOCK = (
    "B0120P0TE12E0100KS1800604B120F9292800000"
    "E6E28F097CB0350B2EB2DF520947F779FA34D9759CEE7E0DEEACF8353DB778D47FA4EC20DA3A9754"
)


def test_the_published_example_opens():
    header, key = key_block.unwrap(EXAMPLE_KBPK, EXAMPLE_BLOCK)
    assert key.hex().upper() == EXAMPLE_KEY
    assert (header.usage, header.algorithm, header.mode_of_use) == ("P0", "T", "E")
    assert (header.key_version, header.exportability) == ("12", "E")


@pytest.mark.parametrize("key_hex", ["0123456789ABCDEFFEDCBA9876543210", "0123456789ABCDEF" * 3])
def test_a_sealed_key_opens_with_its_use(key_hex):
    kbpk = bytes.fromhex("0123456789ABCDEFFEDCBA9876543210")
    header = key_block.Header("B", "M3", "T", "C")
    block = key_block.wrap(kbpk, header, bytes.fromhex(key_hex))
    # header, 32 bytes of key data and an 8-byte MAC, in hex
    assert block.startswith("B0096M3TC00N0000") and block == block.upper()
    opened, key = key_block.unwrap(kbpk, block)
    assert opened == header and key.hex().upper() == key_hex


def test_blocks_that_dont_open():
    other = bytes.fromhex("FEDCBA98765432100123456789ABCDEF")
    cases = {
        "under another key": (other, EXAMPLE_BLOCK, "mac"),
        "with its MAC changed": (EXAMPLE_KBPK, EXAMPLE_BLOCK[:-1] + "5", "mac"),
        "cut short": (EXAMPLE_KBPK, EXAMPLE_BLOCK[:-16], "format"),
        "of another version": (EXAMPLE_KBPK, "A" + EXAMPLE_BLOCK[1:], "version"),
    }
    for name, (kbpk, block, reason) in cases.items():
        with pytest.raises(key_block.KeyBlockError) as caught:
            key_block.unwrap(kbpk, block)
        assert caught.value.reason == reason, name
