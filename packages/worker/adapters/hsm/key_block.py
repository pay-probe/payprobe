"""ANSI X9 TR-31 key blocks, version B, for keys that travel under a ZMK.

A key block carries a key with its use bound to it: a 16-character header (the
version, the block's length, the key usage, algorithm, mode of use, key version
and exportability), the key encrypted, and a MAC over the header and the clear
key data, so a block can't be opened as a key of another use, or altered,
without failing.

Version B, for a TDES key block protection key (KBPK):
- an encryption key and a MAC key are derived from the KBPK with TDES CMAC;
- the key data (the key's length in bits, 2 bytes, then the key, then random
  padding) is MACed together with the header;
- the key data is encrypted with TDES CBC under the derived encryption key,
  with the MAC as its IV;
- the block is the header, the encrypted key data in hex, then the 8-byte MAC
  in hex.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

VERSION_B = "B"
HEADER_LEN = 16
MAC_LEN = 8  # bytes, for version B
#: Key data is padded as for the longest TDES key, so a block doesn't show
#: whether it holds a double- or a triple-length key.
_PADDED_KEY_LEN = 24


class KeyBlockError(ValueError):
    """A key block that can't be opened. ``reason`` is one of ``format``,
    ``version`` or ``mac``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Header:
    version: str
    usage: str
    algorithm: str
    mode_of_use: str
    key_version: str = "00"
    exportability: str = "N"

    def encode(self, length: int) -> str:
        return (
            f"{self.version}{length:04d}{self.usage}{self.algorithm}{self.mode_of_use}"
            f"{self.key_version}{self.exportability}0000"
        )


def _cmac(key: bytes, data: bytes) -> bytes:
    from Crypto.Cipher import DES3
    from Crypto.Hash import CMAC

    return CMAC.new(key, msg=data, ciphermod=DES3).digest()


def _derive(kbpk: bytes) -> tuple[bytes, bytes]:
    """The block encryption key and block MAC key derived from the KBPK."""
    algorithm, bits = (b"\x00\x00", b"\x00\x80") if len(kbpk) == 16 else (b"\x00\x01", b"\x00\xc0")

    def derive(usage: bytes) -> bytes:
        parts = [
            _cmac(kbpk, bytes([counter]) + usage + b"\x00" + algorithm + bits)
            for counter in range(1, len(kbpk) // 8 + 1)
        ]
        return b"".join(parts)

    return derive(b"\x00\x00"), derive(b"\x00\x01")


def _cbc(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    from Crypto.Cipher import DES3

    cipher = DES3.new(key, DES3.MODE_CBC, iv=iv)
    return cipher.encrypt(data) if encrypt else cipher.decrypt(data)


def _check_kbpk(kbpk: bytes) -> None:
    if len(kbpk) not in (16, 24):
        raise KeyBlockError("format", "a version B KBPK is a double- or triple-length TDES key")


def wrap(kbpk: bytes, header: Header, key: bytes) -> str:
    """Seal ``key`` in a version B block under ``kbpk``, with ``header``'s use."""
    _check_kbpk(kbpk)
    if header.version != VERSION_B:
        raise KeyBlockError("version", f"key block version {header.version} isn't supported")
    pad = secrets.token_bytes(_PADDED_KEY_LEN - len(key) + (-(2 + _PADDED_KEY_LEN) % 8))
    key_data = (len(key) * 8).to_bytes(2, "big") + key + pad
    length = HEADER_LEN + 2 * len(key_data) + 2 * MAC_LEN
    text = header.encode(length)
    kbek, kbak = _derive(kbpk)
    mac = _cmac(kbak, text.encode("ascii") + key_data)[:MAC_LEN]
    return text + _cbc(kbek, mac, key_data, True).hex().upper() + mac.hex().upper()


def unwrap(kbpk: bytes, block: str) -> tuple[Header, bytes]:
    """Open a version B block under ``kbpk``: its header and the clear key.
    The header's optional blocks, if any, are covered by the MAC and skipped."""
    _check_kbpk(kbpk)
    if len(block) < HEADER_LEN or not block[1:5].isdigit() or int(block[1:5]) != len(block):
        raise KeyBlockError("format", "the key block's length doesn't match it")
    if block[0] != VERSION_B:
        raise KeyBlockError("version", f"key block version {block[0]} isn't supported")
    header_len = HEADER_LEN
    try:
        for _ in range(int(block[12:14])):
            header_len += int(block[header_len + 2 : header_len + 4], 16)
        key_data = bytes.fromhex(block[header_len : len(block) - 2 * MAC_LEN])
        mac = bytes.fromhex(block[len(block) - 2 * MAC_LEN :])
    except ValueError as exc:
        raise KeyBlockError("format", "the key block isn't well formed") from exc
    if not key_data or len(key_data) % 8:
        raise KeyBlockError("format", "the key block's key data isn't whole blocks")
    kbek, kbak = _derive(kbpk)
    clear = _cbc(kbek, mac, key_data, False)
    if _cmac(kbak, block[:header_len].encode("ascii") + clear)[:MAC_LEN] != mac:
        raise KeyBlockError("mac", "the key block's MAC doesn't verify")
    key_len = int.from_bytes(clear[:2], "big") // 8
    if key_len == 0 or 2 + key_len > len(clear):
        raise KeyBlockError("format", "the key block's key length doesn't hold")
    header = Header(
        version=block[0],
        usage=block[5:7],
        algorithm=block[7],
        mode_of_use=block[8],
        key_version=block[9:11],
        exportability=block[11],
    )
    return header, clear[2 : 2 + key_len]
