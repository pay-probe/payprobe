"""Test LMK + key-token codec for the payShield 10K simulator.

A real payShield encrypts every working key under its Local Master Key (LMK) and
hands the host back an opaque *key token*: a one-character **key scheme** tag
followed by the ciphertext as hex. ``U`` = double-length TDES, ``T`` =
triple-length TDES, ``Z`` (or no tag) = single-length DES. The host stores the
token and passes it back on later commands; only the HSM can unwrap it.

This module models that with a single fixed **test LMK** (double-length TDES)
used to wrap/unwrap keys with plain 3DES-ECB. It is deliberately *not* the real
per-key-type variant scheme — a simulator only needs the tokens to round-trip
*within itself* so that, e.g., a key minted by ``A0`` verifies under ``BU`` and
a CVK from ``A0`` produces a checkable CVV under ``CW``/``CY``. Key Check Values
and all payment crypto (CVV, PVV, PIN blocks, MAC) are computed from the *clear*
key, so they are cryptographically correct and verifiable regardless of the LMK.

Tokens minted here will **not** interoperate with a physical payShield (different
LMK); that is expected and fine for protocol/integration testing.
"""

from __future__ import annotations

import secrets

from ...engine import crypto_tools as ct

#: Default double-length TDES test LMK. Override per-simulator via config
#: ``{"lmk": "<32 hex>"}`` to mimic a particular estate's key separation.
DEFAULT_LMK = "0123456789ABCDEFFEDCBA9876543210"

#: Key-scheme tag -> clear-key length in bytes. Tags U/X/T/Y/Z are the DES
#: family; R/W are a simulator convention for AES keys (R = AES-128, W = AES-256)
#: so an AES ZPK/TPK can round-trip like any other token — real payShield carries
#: AES keys in variable-length 'S' key blocks, out of scope here.
SCHEME_BYTES = {"Z": 8, "U": 16, "X": 16, "T": 24, "Y": 24, "R": 16, "W": 32}
#: AES key-scheme tags (clear key is used as an AES, not DES, key).
AES_SCHEMES = {"R", "W"}
#: Clear-key length in bytes -> the scheme tag we mint with.
BYTES_SCHEME = {8: "Z", 16: "U", 24: "T", 32: "W"}

#: Variant number for each key type when ``variant_lmk`` is enabled. Mirrors the
#: real payShield LMK-pair/variant separation closely enough that the simulator
#: enforces *key separation* — a key minted as one type won't unwrap correctly
#: when a command treats it as another (it produces a different clear key, and
#: so a parity/verification error). Keyed by 3-digit key-type code; the leading
#: digit of the type code is ignored, so "001"/"401" etc. collapse sensibly.
KEYTYPE_VARIANT = {
    "000": 0,  # ZMK
    "001": 0,  # ZPK
    "002": 0,  # TPK / PVK / TMK (LMK 14-15 variant 0)
    "402": 4,  # CVK   (LMK 14-15 variant 4)
    "008": 0,  # ZAK / TAK (MAC)
    "109": 1,  # MK-AC / EMV issuer master key (LMK 28-29 variant 1)
    "302": 0,  # BDK / IPEK
}


def variant_for_keytype(code: str) -> int:
    """The variant number a key of ``code`` is wrapped under (0 if unknown)."""
    return KEYTYPE_VARIANT.get((code or "").strip()[:3], 0)


def _set_odd_parity(data: bytes) -> bytes:
    """Force DES odd parity on every byte (cosmetic realism for clear keys)."""
    out = bytearray()
    for b in data:
        b &= 0xFE
        if bin(b).count("1") % 2 == 0:
            b |= 1
        out.append(b)
    return bytes(out)


class TestLmk:
    """Wraps/unwraps clear keys under a fixed test LMK and mints fresh keys."""

    def __init__(self, lmk_hex: str | None = None, variant_lmk: bool = False) -> None:
        self.lmk_hex = (lmk_hex or DEFAULT_LMK).upper()
        #: when True, keys are wrapped under a per-key-type variant of the LMK,
        #: so the simulator enforces key separation (see ``KEYTYPE_VARIANT``).
        self.variant_lmk = bool(variant_lmk)

    def _variant_lmk(self, variant: int) -> str:
        """The LMK with a variant XORed in (a no-op when variant_lmk is off)."""
        if not self.variant_lmk or not variant:
            return self.lmk_hex
        lmk = bytes.fromhex(self.lmk_hex)
        mask = bytes([variant]) + b"\x00" * 7
        out = bytes(a ^ b for a, b in zip(lmk[:8], mask)) + bytes(
            a ^ b for a, b in zip(lmk[8:16], mask)
        )
        return (out + lmk[16:]).hex().upper()

    # -- token round-trip ----------------------------------------------------

    def wrap(self, clear_hex: str, scheme: str | None = None, variant: int = 0) -> str:
        """Clear key (hex) -> key token (scheme tag + ciphertext hex)."""
        clear_hex = clear_hex.upper()
        nbytes = len(clear_hex) // 2
        scheme = scheme or BYTES_SCHEME.get(nbytes, "U")
        ct_hex = ct.des(self._variant_lmk(variant), clear_hex, op="encrypt", mode="ecb")["result"]
        return f"{scheme}{ct_hex}"

    def unwrap(self, token: str, variant: int = 0) -> str:
        """Key token -> clear key (hex). Accepts a leading scheme tag or bare
        hex (legacy single/double-length keys carry no tag)."""
        token = (token or "").strip()
        if token[:1] in SCHEME_BYTES:
            token = token[1:]
        return ct.des(self._variant_lmk(variant), token, op="decrypt", mode="ecb")["result"]

    # -- key generation ------------------------------------------------------

    def generate(self, scheme: str = "U", variant: int = 0) -> tuple[str, str, str]:
        """Mint a random clear key for ``scheme``; return (clear, token, kcv)."""
        nbytes = SCHEME_BYTES.get(scheme, 16)
        raw = secrets.token_bytes(nbytes)
        if scheme not in AES_SCHEMES:  # DES-family keys carry odd parity
            raw = _set_odd_parity(raw)
        clear = raw.hex().upper()
        return clear, self.wrap(clear, scheme, variant), kcv6(clear)

    # -- LMK self-check (NC / console "V") -----------------------------------

    def check_value(self) -> str:
        """The 16-digit LMK check value reported by NC / the console V command."""
        return ct.kcv(self.lmk_hex)["full"][:16].ljust(16, "0")


def kcv6(clear_hex: str) -> str:
    """6-hex Key Check Value of a *clear* key (encrypt 8 zero bytes, take 3)."""
    return ct.kcv(clear_hex)["kcv"]


def read_key_field(data: str) -> tuple[str, str]:
    """Split a key field off the front of ``data``.

    Returns ``(token, rest)``. A leading scheme tag (U/T/X/Y/Z/S) selects the
    token width; an untagged field is assumed double-length (32 hex)."""
    tag = data[:1]
    if tag in ("U", "X", "R"):  # double-length DES or AES-128 (16 bytes)
        return data[:33], data[33:]
    if tag in ("T", "Y"):
        return data[:49], data[49:]
    if tag == "W":  # AES-256 (32 bytes)
        return data[:65], data[65:]
    if tag == "Z":
        return data[:17], data[17:]
    if tag == "S":  # key-block — variable; not minted here, take a sane chunk
        return data[:49], data[49:]
    return data[:32], data[32:]  # bare double-length hex
