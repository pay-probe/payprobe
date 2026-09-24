"""Length-prefix encoding shared by everything that frames a TCP stream.

A frame is ``length-prefix || (tpdu) || body``. The prefix is ``length_prefix_bytes``
wide and carries the length in one of three encodings (``framing.length_encoding``):

* ``binary`` (default): an unsigned integer, ``length_byte_order`` big or little
  endian. Two bytes big-endian is the ISO 8583 norm.
* ``ascii``: zero-padded decimal digits, one per prefix byte, so a 4-byte prefix
  reads ``0043``. Some hosts and older switches frame this way; byte order does
  not apply.
* ``bcd``: zero-padded decimal digits packed two per byte, so a 2-byte prefix
  reads ``0x00 0x43`` for 43. Hosts that pack the body as BCD usually frame this
  way too (ADR-0011); byte order does not apply.

The adapter (client), the responder / simulators (server), the proxy and the
chaos engine all go through these functions so the two ends of a PayProbe
connection can never disagree on the arithmetic.
"""

from __future__ import annotations

LENGTH_ENCODINGS = ("binary", "ascii", "bcd")


def normalise_length_encoding(value: object) -> str:
    enc = str(value or "binary").lower()
    if enc not in LENGTH_ENCODINGS:
        raise ValueError(
            f"framing.length_encoding must be one of {', '.join(LENGTH_ENCODINGS)}, got {value!r}"
        )
    return enc


def length_capacity(prefix_bytes: int, encoding: str = "binary") -> int:
    """The largest length a prefix of this width and encoding can carry."""
    if encoding == "ascii":
        return 10**prefix_bytes - 1
    if encoding == "bcd":
        return 10 ** (2 * prefix_bytes) - 1
    return (1 << (8 * prefix_bytes)) - 1


def encode_length(
    value: int, prefix_bytes: int, byte_order: str = "big", encoding: str = "binary"
) -> bytes:
    """The prefix bytes for ``value``; raises ``ValueError`` when it does not fit."""
    if value < 0 or value > length_capacity(prefix_bytes, encoding):
        raise ValueError(
            f"frame length {value} does not fit a {prefix_bytes}-byte {encoding} length prefix"
        )
    if encoding == "ascii":
        return f"{value:0{prefix_bytes}d}".encode("ascii")
    if encoding == "bcd":
        return bytes.fromhex(f"{value:0{2 * prefix_bytes}d}")
    return value.to_bytes(prefix_bytes, byte_order)


def decode_length(prefix: bytes, byte_order: str = "big", encoding: str = "binary") -> int:
    """The length a prefix declares; for ``ascii`` / ``bcd`` a non-digit prefix is
    a ``ValueError`` naming the bytes, the usual sign of a framing mismatch."""
    if encoding == "ascii":
        text = prefix.decode("ascii", "replace")
        if not text.isdigit():
            raise ValueError(
                f"ASCII length prefix {prefix!r} is not decimal digits: the peer frames "
                "differently (framing.length_encoding / length_prefix_bytes mismatch?)"
            )
        return int(text)
    if encoding == "bcd":
        digits = prefix.hex()
        if not digits.isdigit():
            raise ValueError(
                f"BCD length prefix {prefix!r} holds non-decimal nibbles: the peer frames "
                "differently (framing.length_encoding / length_prefix_bytes mismatch?)"
            )
        return int(digits)
    return int.from_bytes(prefix, byte_order)


__all__ = [
    "LENGTH_ENCODINGS",
    "decode_length",
    "encode_length",
    "length_capacity",
    "normalise_length_encoding",
]
