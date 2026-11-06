"""Length-prefix framing shared by everything that frames a TCP stream.

A frame is ``length-prefix || (tpdu) || (header) || body``. The prefix is
``length_prefix_bytes`` wide and carries the length in one of three encodings
(``framing.length_encoding``):

* ``binary`` (default): an unsigned integer, ``length_byte_order`` big or little
  endian. Two bytes big-endian is the ISO 8583 norm.
* ``ascii``: zero-padded decimal digits, one per prefix byte, so a 4-byte prefix
  reads ``0043``. Some hosts and older switches frame this way; byte order does
  not apply. Vendor configuration screens call this ``CHARDEC`` (BPC SmartVista:
  "standard header GENERIC4, header type CHARDEC, bytes length 4"), which is
  accepted as an alias and folded to ``ascii``.
* ``bcd``: zero-padded decimal digits packed two per byte, so a 2-byte prefix
  reads ``0x00 0x43`` for 43. Hosts that pack the body as BCD usually frame this
  way too (ADR-0011); byte order does not apply.

Between the prefix and the body two optional fixed blocks may sit:

* a **TPDU** (``tpdu_outbound_hex`` on send, ``tpdu_bytes`` stripped on receive),
* a **message header** — a fixed string some hosts require in front of every ISO
  message (``framing.header`` as text, ``framing.header_hex`` as bytes, or the
  vendor-style top-level ``mhdr`` key); ``header_bytes`` is the inbound width to
  strip (defaults to the outbound header's length).

``length_includes_header`` says whether the declared length counts those fixed
blocks; ``length_includes_prefix`` whether it counts the prefix itself.

:class:`FrameSpec` is the one place this arithmetic lives. The adapter (client),
the responder / simulators (server), the proxy and the chaos engine all go
through it so the two ends of a PayProbe connection can never disagree on the
bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

LENGTH_ENCODINGS = ("binary", "ascii", "bcd")

#: Vendor / legacy spellings folded to the canonical name. ``chardec`` is the
#: BPC SmartVista term for "characters, decimal" — an ASCII digit prefix.
LENGTH_ENCODING_ALIASES = {
    "chardec": "ascii",
    "char": "ascii",
    "decimal": "ascii",
    "bin": "binary",
}


def normalise_length_encoding(value: object) -> str:
    enc = str(value or "binary").lower()
    enc = LENGTH_ENCODING_ALIASES.get(enc, enc)
    if enc not in LENGTH_ENCODINGS:
        raise ValueError(
            f"framing.length_encoding must be one of {', '.join(LENGTH_ENCODINGS)} "
            f"(aliases: {', '.join(sorted(LENGTH_ENCODING_ALIASES))}), got {value!r}"
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


# --------------------------------------------------------------------------- #
# The one framing model
# --------------------------------------------------------------------------- #


def _hex_bytes(value: object, key: str) -> bytes:
    try:
        return bytes.fromhex(str(value or "").replace(" ", ""))
    except ValueError as exc:
        raise ValueError(f"framing.{key} must be hex digits, got {value!r}") from exc


def _header_from_config(config: dict, f: dict) -> bytes:
    """The outbound message header: ``framing.header_hex`` (bytes) or
    ``framing.header`` / ``framing.mhdr`` / top-level ``mhdr`` (ASCII text).
    Several spellings may be present only when they agree."""
    candidates: dict[str, bytes] = {}
    if f.get("header_hex"):
        candidates["framing.header_hex"] = _hex_bytes(f["header_hex"], "header_hex")
    for key, value in (
        ("framing.header", f.get("header")),
        ("framing.mhdr", f.get("mhdr")),
        ("mhdr", config.get("mhdr")),
    ):
        if value in (None, ""):
            continue
        try:
            candidates[key] = str(value).encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"{key} must be ASCII text (use framing.header_hex for other bytes), "
                f"got {value!r}"
            ) from exc
    distinct = set(candidates.values())
    if len(distinct) > 1:
        names = ", ".join(f"{k}={v!r}" for k, v in candidates.items())
        raise ValueError(f"conflicting message headers configured: {names}; keep one")
    return next(iter(distinct)) if distinct else b""


@dataclass(frozen=True)
class FrameSpec:
    """How one connection frames the stream — shared by both ends.

    Build it from a connection / simulator config with :meth:`from_config`;
    :meth:`frame` produces the exact outbound bytes for a body, :meth:`parts`
    the same bytes split for a trace, and :meth:`remaining_after_prefix` /
    :meth:`strip_inbound` drive the read side.
    """

    prefix_bytes: int = 2
    byte_order: str = "big"
    length_encoding: str = "binary"
    length_includes_prefix: bool = False
    length_includes_header: bool = True
    tpdu_out: bytes = b""
    tpdu_bytes: int = 0
    header_out: bytes = b""
    header_bytes: int = 0

    @classmethod
    def from_config(cls, config: dict | None) -> FrameSpec:
        """Read every framing key the adapter / responder honour. Unknown or
        malformed values raise ``ValueError`` at construction — a framing typo
        must never bind silently as the default."""
        config = config or {}
        f = config.get("framing") or {}
        if not isinstance(f, dict):
            raise TypeError("framing must be an object")
        prefix_bytes = int(f.get("length_prefix_bytes", 2))
        if prefix_bytes < 1:
            raise ValueError(
                "framing.length_prefix_bytes must be >= 1 — a length prefix is "
                "required to frame messages on a multiplexed connection."
            )
        byte_order = str(f.get("length_byte_order", "big")).lower()
        if byte_order not in ("big", "little"):
            raise ValueError(
                f"framing.length_byte_order must be 'big' or 'little', got {byte_order!r}"
            )
        header_out = _header_from_config(config, f)
        header_bytes = f.get("header_bytes")
        header_bytes = len(header_out) if header_bytes is None else int(header_bytes)
        if header_bytes < 0:
            raise ValueError("framing.header_bytes must be >= 0")
        tpdu_bytes = int(f.get("tpdu_bytes", 0))
        if tpdu_bytes < 0:
            raise ValueError("framing.tpdu_bytes must be >= 0")
        return cls(
            prefix_bytes=prefix_bytes,
            byte_order=byte_order,
            length_encoding=normalise_length_encoding(f.get("length_encoding")),
            length_includes_prefix=bool(f.get("length_includes_prefix", False)),
            length_includes_header=bool(f.get("length_includes_header", True)),
            tpdu_out=_hex_bytes(f.get("tpdu_outbound_hex"), "tpdu_outbound_hex"),
            tpdu_bytes=tpdu_bytes,
            header_out=header_out,
            header_bytes=header_bytes,
        )

    # -- outbound ----------------------------------------------------------

    @property
    def fixed_out(self) -> bytes:
        """Everything between the prefix and the body on send."""
        return self.tpdu_out + self.header_out

    def declared_length(self, body: bytes) -> int:
        counted = len(self.fixed_out) + len(body) if self.length_includes_header else len(body)
        return counted + (self.prefix_bytes if self.length_includes_prefix else 0)

    def prefix(self, body: bytes) -> bytes:
        return encode_length(
            self.declared_length(body), self.prefix_bytes, self.byte_order, self.length_encoding
        )

    def frame(self, body: bytes) -> bytes:
        """The exact bytes that go on the wire for ``body``."""
        return self.prefix(body) + self.fixed_out + body

    def parts(self, body: bytes) -> dict:
        """The frame split into its blocks (hex), for traces and dry runs."""
        frame = self.frame(body)
        return {
            "hex": frame.hex(),
            "total_bytes": len(frame),
            "prefix_hex": self.prefix(body).hex(),
            "declared_length": self.declared_length(body),
            "tpdu_hex": self.tpdu_out.hex(),
            "header_hex": self.header_out.hex(),
            "body_hex": body.hex(),
            "body_bytes": len(body),
        }

    # -- inbound -----------------------------------------------------------

    @property
    def fixed_in(self) -> int:
        """Bytes between the prefix and the body on receive (TPDU + header)."""
        return self.tpdu_bytes + self.header_bytes

    def remaining_after_prefix(self, prefix: bytes) -> int:
        """How many bytes follow ``prefix`` in this frame (fixed blocks + body)."""
        declared = decode_length(prefix, self.byte_order, self.length_encoding)
        core = declared - (self.prefix_bytes if self.length_includes_prefix else 0)
        return core if self.length_includes_header else core + self.fixed_in

    def strip_inbound(self, data: bytes) -> tuple[bytes, bytes]:
        """Split what was read after the prefix into ``(fixed_blocks, body)``."""
        return data[: self.fixed_in], data[self.fixed_in :]

    def describe(self) -> str:
        """One line for logs: ``prefix=4 ascii · header=43446234 (counted)``."""
        bits = [f"prefix={self.prefix_bytes} {self.length_encoding}"]
        if self.length_encoding == "binary":
            bits[0] += f" {self.byte_order}-endian"
        if self.length_includes_prefix:
            bits.append("length counts prefix")
        if self.tpdu_out or self.tpdu_bytes:
            bits.append(f"tpdu={self.tpdu_out.hex() or '-'}/{self.tpdu_bytes}in")
        if self.header_out or self.header_bytes:
            bits.append(
                f"header={self.header_out.hex() or '-'}/{self.header_bytes}in "
                f"({'counted' if self.length_includes_header else 'not counted'})"
            )
        return " · ".join(bits)

    def with_(self, **changes) -> FrameSpec:
        return replace(self, **changes)


__all__ = [
    "LENGTH_ENCODINGS",
    "LENGTH_ENCODING_ALIASES",
    "FrameSpec",
    "decode_length",
    "encode_length",
    "length_capacity",
    "normalise_length_encoding",
]
