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
LMK); that is expected and fine for protocol/integration testing. To interoperate
with an HSM or tool loaded with the published Thales variant test LMK, set
``lmk_scheme`` to ``"thales-variant"`` (see :class:`TestLmk`).
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
        if (b).bit_count() % 2 == 0:
            b |= 1
        out.append(b)
    return bytes(out)


#: The published Thales double-length variant test LMK: LMK pairs 00-01 to
#: 38-39, each as its left and right key.
THALES_TEST_LMK = (
    ("0101010101010101", "7902CD1FD36EF8BA"),
    ("2020202020202020", "3131313131313131"),
    ("4040404040404040", "5151515151515151"),
    ("6161616161616161", "7070707070707070"),
    ("8080808080808080", "9191919191919191"),
    ("A1A1A1A1A1A1A1A1", "B0B0B0B0B0B0B0B0"),
    ("C1C1010101010101", "D0D0010101010101"),
    ("E0E0010101010101", "F1F1010101010101"),
    ("1C587F1C13924FEF", "0101010101010101"),
    ("0101010101010101", "0101010101010101"),
    ("0202020202020202", "0404040404040404"),
    ("0707070707070707", "1010101010101010"),
    ("1313131313131313", "1515151515151515"),
    ("1616161616161616", "1919191919191919"),
    ("1A1A1A1A1A1A1A1A", "1C1C1C1C1C1C1C1C"),
    ("2323232323232323", "2525252525252525"),
    ("2626262626262626", "2929292929292929"),
    ("2A2A2A2A2A2A2A2A", "2C2C2C2C2C2C2C2C"),
    ("2F2F2F2F2F2F2F2F", "3131313131313131"),
    ("0101010101010101", "0101010101010101"),
)

#: Key type -> (LMK pair, as an index into THALES_TEST_LMK; variant number).
#: The key type's middle and last digits name the LMK pair, its first digit the
#: variant.
THALES_KEY_TYPES = {
    "000": (2, 0),  # ZMK                       LMK 04-05
    "100": (2, 1),  # ZMK component
    "200": (2, 2),  # KML
    "001": (3, 0),  # ZPK                       LMK 06-07
    "002": (7, 0),  # TPK / PVK / TMK           LMK 14-15
    "102": (7, 1),
    "202": (7, 2),
    "302": (7, 3),  # IKEY / IPEK
    "402": (7, 4),  # CVK
    "003": (8, 0),  # TAK                       LMK 16-17
    "006": (11, 0),  # WWK                      LMK 22-23
    "008": (13, 0),  # ZAK                      LMK 26-27
    "009": (14, 0),  # BDK type 1               LMK 28-29
    "109": (14, 1),  # MK-AC
    "209": (14, 2),  # MK-SMI
    "309": (14, 3),  # MK-SMC
    "409": (14, 4),  # MK-DAC
    "509": (14, 5),  # MK-DN
}

#: Variant number -> the byte XORed into the first byte of the LMK pair's left key.
_THALES_VARIANTS = {1: 0xA6, 2: 0x5A, 3: 0x6A, 4: 0xDE, 5: 0x2B, 6: 0x50, 7: 0x74, 8: 0x9C, 9: 0xFA}
#: Key scheme -> the byte XORed into the first byte of the pair's right key for
#: each 8-byte part of the key.
_THALES_SCHEME_VARIANTS = {"U": (0xA6, 0x5A), "T": (0x6A, 0xDE, 0x2B)}

#: ``lmk_scheme`` value selecting the Thales variant test LMK.
THALES_VARIANT = "thales-variant"


class KeyParityError(ValueError):
    """A key under the LMK whose clear value doesn't have DES odd parity."""


def _has_odd_parity(clear_hex: str) -> bool:
    return all(b.bit_count() % 2 == 1 for b in bytes.fromhex(clear_hex))


class TestLmk:
    """Wraps/unwraps clear keys under a test LMK and mints fresh keys.

    By default that is a single fixed double-length key (``lmk``), optionally
    with a per-key-type variant (``variant_lmk``). With ``lmk_scheme`` set to
    ``"thales-variant"`` it is the published Thales variant test LMK, applied
    as a payShield applies a variant LMK: the key type picks the LMK pair and
    its variant, and each part of a double- (``U``) or triple-length (``T``)
    key is encrypted under the pair with that part's key-scheme variant. Keys
    then interoperate with any HSM or tool loaded with the same test LMK. A
    key taken from under it must have DES odd parity, as on a payShield.
    """

    def __init__(
        self, lmk_hex: str | None = None, variant_lmk: bool = False, lmk_scheme: str | None = None
    ) -> None:
        self.lmk_hex = (lmk_hex or DEFAULT_LMK).upper()
        #: when True, keys are wrapped under a per-key-type variant of the LMK,
        #: so the simulator enforces key separation (see ``KEYTYPE_VARIANT``).
        self.variant_lmk = bool(variant_lmk)
        if lmk_scheme not in (None, "", THALES_VARIANT):
            raise ValueError(f"unknown lmk_scheme {lmk_scheme!r}")
        self.thales = lmk_scheme == THALES_VARIANT

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

    def _thales_part_keys(self, key_type: str, scheme: str) -> list[str]:
        """The triple-DES key each 8-byte part of a key of ``key_type`` and
        ``scheme`` is encrypted under, in the Thales variant test LMK."""
        if key_type not in THALES_KEY_TYPES:
            raise ValueError(f"key type {key_type!r} isn't in the test LMK's key type table")
        if scheme not in _THALES_SCHEME_VARIANTS:
            raise ValueError(f"key scheme {scheme!r} isn't a U or T key under a variant LMK")
        pair, variant = THALES_KEY_TYPES[key_type]
        left, right = (bytearray.fromhex(half) for half in THALES_TEST_LMK[pair])
        left[0] ^= _THALES_VARIANTS.get(variant, 0)
        keys = []
        for scheme_variant in _THALES_SCHEME_VARIANTS[scheme]:
            part_right = bytearray(right)
            part_right[0] ^= scheme_variant
            keys.append((bytes(left) + bytes(part_right) + bytes(left)).hex())
        return keys

    def _thales(self, hex_in: str, key_type: str, scheme: str, op: str) -> str:
        keys = self._thales_part_keys(key_type, scheme)
        if len(hex_in) != 16 * len(keys):
            raise ValueError(f"a {scheme} key is {8 * len(keys)} bytes")
        return "".join(
            ct.des(key, hex_in[16 * i : 16 * (i + 1)], op=op, mode="ecb")["result"]
            for i, key in enumerate(keys)
        )

    # -- token round-trip ----------------------------------------------------

    def wrap(self, clear_hex: str, scheme: str | None = None, key_type: str = "") -> str:
        """Clear key (hex) of ``key_type`` -> key token (scheme tag + ciphertext hex)."""
        clear_hex = clear_hex.upper()
        nbytes = len(clear_hex) // 2
        scheme = scheme or BYTES_SCHEME.get(nbytes, "U")
        if self.thales:
            return f"{scheme}{self._thales(clear_hex, key_type, scheme, 'encrypt')}"
        lmk = self._variant_lmk(variant_for_keytype(key_type))
        return f"{scheme}{ct.des(lmk, clear_hex, op='encrypt', mode='ecb')['result']}"

    def unwrap(self, token: str, key_type: str = "") -> str:
        """Key token of ``key_type`` -> clear key (hex). Accepts a leading
        scheme tag or bare hex (legacy single/double-length keys carry no tag;
        the Thales variant test LMK takes only U and T tokens)."""
        token = (token or "").strip()
        scheme = ""
        if token[:1] in SCHEME_BYTES:
            scheme, token = token[:1], token[1:]
        if self.thales:
            # A payShield checks every DES key it takes from under the LMK.
            clear = self._thales(token, key_type, scheme, "decrypt")
            if not _has_odd_parity(clear):
                raise KeyParityError("the key under the LMK doesn't have odd parity")
            return clear
        lmk = self._variant_lmk(variant_for_keytype(key_type))
        return ct.des(lmk, token, op="decrypt", mode="ecb")["result"]

    # -- key generation ------------------------------------------------------

    def generate(self, scheme: str = "U", key_type: str = "") -> tuple[str, str, str]:
        """Mint a random clear key for ``scheme``; return (clear, token, kcv)."""
        nbytes = SCHEME_BYTES.get(scheme, 16)
        raw = secrets.token_bytes(nbytes)
        if scheme not in AES_SCHEMES:  # DES-family keys carry odd parity
            raw = _set_odd_parity(raw)
        clear = raw.hex().upper()
        return clear, self.wrap(clear, scheme, key_type), kcv6(clear)

    # -- LMK self-check (NC / console "V") -----------------------------------

    def check_value(self) -> str:
        """The 16-digit LMK check value reported by NC / the console V command:
        8 zero bytes encrypted under the LMK (for the Thales variant test LMK,
        under LMK pair 00-01)."""
        if self.thales:
            return ct.kcv("".join(THALES_TEST_LMK[0]))["full"][:16]
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
