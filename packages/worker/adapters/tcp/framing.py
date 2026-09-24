"""Length-prefix codec shared by the TCP adapter, responder, proxy and chaos.

Every TCP frame is ``length-prefix || (tpdu) || body``. The prefix is
``framing.length_prefix_bytes`` wide and carries the frame length in one of two
encodings, picked by ``framing.length_encoding``:

- ``"binary"`` (default) — an unsigned integer in ``framing.length_byte_order``
  (``"big"`` | ``"little"``). A 2-byte big-endian 43 is ``b"\\x00\\x2b"``.
- ``"ascii"`` — zero-padded decimal digits, as some host links expect (e.g.
  Tieto Card Suite); with 6 digits 43 is ``b"000043"``. Byte order does not apply.
"""

from __future__ import annotations

LENGTH_ENCODINGS = ("binary", "ascii")


def length_encoding(framing: dict) -> str:
    """Validated ``framing.length_encoding`` (default ``"binary"``)."""
    enc = str(framing.get("length_encoding") or "binary").strip().lower()
    if enc not in LENGTH_ENCODINGS:
        raise ValueError(
            f"framing.length_encoding must be one of {', '.join(LENGTH_ENCODINGS)}; got {enc!r}"
        )
    return enc


def max_length(width: int, encoding: str) -> int:
    """Largest length value a ``width``-byte prefix can carry."""
    return 10**width - 1 if encoding == "ascii" else (1 << (width * 8)) - 1


def encode_length(value: int, width: int, byte_order: str, encoding: str) -> bytes:
    """Render ``value`` as a ``width``-byte length prefix."""
    if value < 0 or value > max_length(width, encoding):
        raise ValueError(f"frame length {value} does not fit a {width}-byte {encoding} prefix")
    if encoding == "ascii":
        return str(value).zfill(width).encode("ascii")
    return value.to_bytes(width, byte_order)


def decode_length(prefix: bytes, byte_order: str, encoding: str) -> int:
    """Parse a length prefix read off the wire."""
    if encoding == "ascii":
        if not (prefix.isascii() and prefix.isdigit()):
            raise ValueError(f"length prefix {prefix!r} is not {len(prefix)} ASCII digits")
        return int(prefix)
    return int.from_bytes(prefix, byte_order)
