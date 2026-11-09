"""Framing shared by everything that frames a TCP stream.

Two envelopes (``framing.mode``):

**``length_prefix``** (default) — ``prefix || (tpdu) || (header) || body || (trailer)``.
The prefix is ``length_prefix_bytes`` wide and carries the length in one of
four encodings (``framing.length_encoding``):

* ``binary`` (default): an unsigned integer, ``length_byte_order`` big or little
  endian. Two bytes big-endian is the ISO 8583 norm.
* ``ascii``: zero-padded decimal digits, one per prefix byte, so a 4-byte prefix
  reads ``0043``. Most POS terminal links and older switches frame this way;
  byte order does not apply. Configuration screens call this ``CHARDEC``,
  accepted as an alias.
* ``bcd``: zero-padded decimal digits packed two per byte, so a 2-byte prefix
  reads ``0x00 0x43`` for 43. Hosts that pack the body as BCD usually frame this
  way too (ADR-0011); byte order does not apply.
* ``hex``: zero-padded hexadecimal digits, one per prefix byte (``002B`` for
  43); the screens call it ``CHARHEX``. Byte order does not apply.

Between the prefix and the body two optional fixed blocks may sit:

* a **TPDU** (``tpdu_outbound_hex`` on send, ``tpdu_bytes`` stripped on receive),
* a **message header** — a fixed string some hosts require in front of every ISO
  message (``framing.header`` as text, ``framing.header_hex`` as bytes, or the
  vendor-style top-level ``mhdr`` key); ``header_bytes`` is the inbound width to
  strip (defaults to the outbound header's length).

After the body an optional fixed **trailer** (``trailer_hex``, e.g. ``03`` for a
mandatory ETX) is appended on send and expected on receive; the declared length
never counts it.

``length_includes_header`` says whether the declared length counts the fixed
blocks; ``length_includes_prefix`` whether it counts the prefix itself.

**``delimited``** — ``start || body || end || (checksum)``: the STX … ETX LRC
envelope of serial-era terminal protocols carried over TCP (``start_hex``
default ``02``, ``end_hex`` default ``03``, ``checksum`` ``lrc`` = XOR of body
and end byte, or ``none``). There is no length, so the body must not contain
the end byte: the spec refuses to frame one that does, which makes this mode
fit text / hex-bitmap dialects and not binary ones.

Presets (``framing.preset``, generic names such as ``ascii4`` or
``stx_etx_lrc``) and compact link-level strings (``framing.link_level``, e.g.
``CHARDEC:false:false:BIG:4:``) are expanded into these keys by
``payprobe_common.link_framing`` — the one vocabulary the registry validates
and this module executes.

:class:`FrameSpec` is the one place this arithmetic lives. The adapter (client),
the responder / simulators (server), the proxy and the chaos engine all go
through it so the two ends of a PayProbe connection can never disagree on the
bytes. :meth:`FrameSpec.read_frame` is the one read loop both ends use.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from payprobe_common.link_framing import (
    DELIMITED_CHECKSUMS,
    FRAMING_MODES,
    FRAMING_PRESETS,
    LENGTH_ENCODING_ALIASES,
    LENGTH_ENCODINGS,
    LinkFramingError,
    expand_framing,
    normalise_length_encoding,
)


def length_capacity(prefix_bytes: int, encoding: str = "binary") -> int:
    """The largest length a prefix of this width and encoding can carry."""
    if encoding == "ascii":
        return 10**prefix_bytes - 1
    if encoding == "bcd":
        return 10 ** (2 * prefix_bytes) - 1
    if encoding == "hex":
        return 16**prefix_bytes - 1
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
    if encoding == "hex":
        return f"{value:0{prefix_bytes}X}".encode("ascii")
    return value.to_bytes(prefix_bytes, byte_order)


def decode_length(prefix: bytes, byte_order: str = "big", encoding: str = "binary") -> int:
    """The length a prefix declares; for ``ascii`` / ``bcd`` / ``hex`` a
    non-digit prefix is a ``ValueError`` naming the bytes, the usual sign of a
    framing mismatch."""
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
    if encoding == "hex":
        text = prefix.decode("ascii", "replace")
        if not text or any(c not in "0123456789abcdefABCDEF" for c in text):
            raise ValueError(
                f"hex length prefix {prefix!r} is not hexadecimal digits: the peer frames "
                "differently (framing.length_encoding / length_prefix_bytes mismatch?)"
            )
        return int(text, 16)
    return int.from_bytes(prefix, byte_order)


def lrc(data: bytes) -> int:
    """Longitudinal redundancy check: XOR of every byte."""
    acc = 0
    for b in data:
        acc ^= b
    return acc


# --------------------------------------------------------------------------- #
# The one framing model
# --------------------------------------------------------------------------- #


def _hex_bytes(value: object, key: str) -> bytes:
    try:
        return bytes.fromhex(str(value or "").replace(" ", "").replace(".", ""))
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
    the same bytes split for a trace, and :meth:`read_frame` (or the lower
    level :meth:`remaining_after_prefix` / :meth:`strip_inbound`) drives the
    read side.
    """

    mode: str = "length_prefix"
    prefix_bytes: int = 2
    byte_order: str = "big"
    length_encoding: str = "binary"
    length_includes_prefix: bool = False
    length_includes_header: bool = True
    tpdu_out: bytes = b""
    tpdu_bytes: int = 0
    header_out: bytes = b""
    header_bytes: int = 0
    trailer_out: bytes = b""
    trailer_bytes: int = 0
    start: bytes = b"\x02"
    end: bytes = b"\x03"
    checksum: str = "none"
    preset: str = ""

    @classmethod
    def from_config(cls, config: dict | None) -> FrameSpec:
        """Read every framing key the adapter / responder honour — after
        expanding ``preset`` / ``link_level`` through the shared vocabulary.
        Unknown or malformed values raise ``ValueError`` at construction — a
        framing typo must never bind silently as the default."""
        config = config or {}
        f = config.get("framing") or {}
        if not isinstance(f, dict):
            raise TypeError("framing must be an object")
        f = expand_framing(f)
        mode = f["mode"]
        header_out = _header_from_config(config, f)
        header_bytes = f.get("header_bytes")
        header_bytes = len(header_out) if header_bytes is None else int(header_bytes)
        if header_bytes < 0:
            raise ValueError("framing.header_bytes must be >= 0")
        tpdu_bytes = int(f.get("tpdu_bytes", 0))
        if tpdu_bytes < 0:
            raise ValueError("framing.tpdu_bytes must be >= 0")
        trailer_out = _hex_bytes(f.get("trailer_hex"), "trailer_hex")
        trailer_bytes = f.get("trailer_bytes")
        trailer_bytes = len(trailer_out) if trailer_bytes is None else int(trailer_bytes)
        if trailer_bytes < 0:
            raise ValueError("framing.trailer_bytes must be >= 0")

        if mode == "delimited":
            start = _hex_bytes(f.get("start_hex", "02"), "start_hex")
            end = _hex_bytes(f.get("end_hex", "03"), "end_hex")
            if not start or not end:
                raise ValueError(
                    "framing.start_hex and framing.end_hex must each be at least one byte "
                    "in delimited mode"
                )
            return cls(
                mode="delimited",
                start=start,
                end=end,
                checksum=f.get("checksum", "none"),
                preset=str(f.get("preset") or ""),
            )

        prefix_bytes = int(f.get("length_prefix_bytes", 2))
        if prefix_bytes < 1:
            raise ValueError(
                "framing.length_prefix_bytes must be >= 1 — a length prefix is "
                "required to frame messages on a multiplexed connection (for a "
                "length-less STX/ETX envelope use framing.mode 'delimited')."
            )
        byte_order = str(f.get("length_byte_order", "big")).lower()
        if byte_order not in ("big", "little"):
            raise ValueError(
                f"framing.length_byte_order must be 'big' or 'little', got {byte_order!r}"
            )
        return cls(
            mode="length_prefix",
            prefix_bytes=prefix_bytes,
            byte_order=byte_order,
            length_encoding=normalise_length_encoding(f.get("length_encoding")),
            length_includes_prefix=bool(f.get("length_includes_prefix", False)),
            length_includes_header=bool(f.get("length_includes_header", True)),
            tpdu_out=_hex_bytes(f.get("tpdu_outbound_hex"), "tpdu_outbound_hex"),
            tpdu_bytes=tpdu_bytes,
            header_out=header_out,
            header_bytes=header_bytes,
            trailer_out=trailer_out,
            trailer_bytes=trailer_bytes,
            preset=str(f.get("preset") or ""),
        )

    # -- outbound ----------------------------------------------------------

    @property
    def delimited(self) -> bool:
        return self.mode == "delimited"

    @property
    def fixed_out(self) -> bytes:
        """Everything between the prefix and the body on send."""
        return self.tpdu_out + self.header_out

    def declared_length(self, body: bytes) -> int:
        counted = len(self.fixed_out) + len(body) if self.length_includes_header else len(body)
        return counted + (self.prefix_bytes if self.length_includes_prefix else 0)

    def prefix(self, body: bytes) -> bytes:
        if self.delimited:
            return self.start
        return encode_length(
            self.declared_length(body), self.prefix_bytes, self.byte_order, self.length_encoding
        )

    def checksum_bytes(self, body: bytes) -> bytes:
        """The checksum a delimited frame carries after its end byte."""
        if self.checksum == "lrc":
            return bytes([lrc(body + self.end)])
        return b""

    def frame(self, body: bytes) -> bytes:
        """The exact bytes that go on the wire for ``body``."""
        if self.delimited:
            if self.end in body:
                raise ValueError(
                    f"delimited framing cannot carry a body containing the end byte "
                    f"{self.end.hex()} (binary bitmap or field?) — use a length-prefix "
                    "framing or a text/hex-bitmap dialect"
                )
            return self.start + body + self.end + self.checksum_bytes(body)
        return self.prefix(body) + self.fixed_out + body + self.trailer_out

    def parts(self, body: bytes) -> dict:
        """The frame split into its blocks (hex), for traces and dry runs."""
        frame = self.frame(body)
        if self.delimited:
            return {
                "hex": frame.hex(),
                "total_bytes": len(frame),
                "mode": "delimited",
                "start_hex": self.start.hex(),
                "end_hex": self.end.hex(),
                "checksum": self.checksum,
                "checksum_hex": self.checksum_bytes(body).hex(),
                "body_hex": body.hex(),
                "body_bytes": len(body),
            }
        return {
            "hex": frame.hex(),
            "total_bytes": len(frame),
            "mode": "length_prefix",
            "prefix_hex": self.prefix(body).hex(),
            "declared_length": self.declared_length(body),
            "tpdu_hex": self.tpdu_out.hex(),
            "header_hex": self.header_out.hex(),
            "body_hex": body.hex(),
            "body_bytes": len(body),
            "trailer_hex": self.trailer_out.hex(),
        }

    # -- inbound -----------------------------------------------------------

    @property
    def fixed_in(self) -> int:
        """Bytes between the prefix and the body on receive (TPDU + header)."""
        return self.tpdu_bytes + self.header_bytes

    def remaining_after_prefix(self, prefix: bytes) -> int:
        """How many bytes follow ``prefix`` in this frame (fixed blocks + body +
        trailer)."""
        declared = decode_length(prefix, self.byte_order, self.length_encoding)
        core = declared - (self.prefix_bytes if self.length_includes_prefix else 0)
        rest = core if self.length_includes_header else core + self.fixed_in
        return rest + self.trailer_bytes

    def strip_inbound(self, data: bytes) -> tuple[bytes, bytes]:
        """Split what was read after the prefix into ``(fixed_blocks, body)``,
        dropping (and checking) the trailer."""
        if self.trailer_bytes:
            trailer, data = data[len(data) - self.trailer_bytes :], data[: -self.trailer_bytes]
            if self.trailer_out and trailer != self.trailer_out:
                raise ValueError(
                    f"frame trailer {trailer.hex()} != expected {self.trailer_out.hex()}: "
                    "the peer frames differently (framing.trailer_hex mismatch?)"
                )
        return data[: self.fixed_in], data[self.fixed_in :]

    async def read_frame(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes] | None:
        """One inbound frame as ``(envelope, body)``, where ``envelope`` is the
        prefix plus fixed blocks (or the start byte), or ``None`` for an empty
        frame. Raises ``asyncio.IncompleteReadError`` at EOF and ``ValueError``
        on a framing mismatch (unreadable length, wrong trailer, bad LRC) —
        callers decide how to log and whether to drop the connection."""
        if self.delimited:
            # skip anything before the start byte (idle noise, a stray ACK)
            while True:
                b = await reader.readexactly(1)
                if b == self.start[:1]:
                    break
            if len(self.start) > 1:
                rest = await reader.readexactly(len(self.start) - 1)
                if rest != self.start[1:]:
                    raise ValueError(
                        f"delimited frame start {(self.start[:1] + rest).hex()} != "
                        f"expected {self.start.hex()}"
                    )
            chunk = await reader.readuntil(self.end)
            body = chunk[: -len(self.end)]
            if self.checksum == "lrc":
                got = await reader.readexactly(1)
                want = self.checksum_bytes(body)
                if got != want:
                    raise ValueError(
                        f"LRC mismatch: got {got.hex()}, computed {want.hex()} over body+ETX"
                    )
            return self.start, body
        prefix = await reader.readexactly(self.prefix_bytes)
        remaining = self.remaining_after_prefix(prefix)
        if remaining <= 0:
            return None
        data = await reader.readexactly(remaining)
        fixed, body = self.strip_inbound(data)
        return prefix + fixed, body

    def describe(self) -> str:
        """One line for logs: ``prefix=4 ascii · header=43446234 (counted)``."""
        if self.delimited:
            bits = [f"delimited start={self.start.hex()} end={self.end.hex()}"]
            if self.checksum != "none":
                bits.append(f"checksum={self.checksum}")
        else:
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
            if self.trailer_out or self.trailer_bytes:
                bits.append(f"trailer={self.trailer_out.hex() or '-'}/{self.trailer_bytes}in")
        if self.preset:
            bits.append(f"preset={self.preset}")
        return " · ".join(bits)

    def with_(self, **changes) -> FrameSpec:
        return replace(self, **changes)


__all__ = [
    "DELIMITED_CHECKSUMS",
    "FRAMING_MODES",
    "FRAMING_PRESETS",
    "LENGTH_ENCODINGS",
    "LENGTH_ENCODING_ALIASES",
    "FrameSpec",
    "LinkFramingError",
    "decode_length",
    "encode_length",
    "expand_framing",
    "length_capacity",
    "lrc",
    "normalise_length_encoding",
]
