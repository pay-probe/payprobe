"""Link-level framing vocabulary shared by the worker and the config registry.

Every TCP connection in PayProbe frames its stream one way, and both ends of a
connection (the worker's client adapter, the responder / simulators, the proxy,
the chaos engine) must agree on it byte for byte. The arithmetic lives in the
worker (``worker.adapters.tcp.framing.FrameSpec``); *this* module is the
vocabulary the registry validates against and the worker executes — kept here
so the scenario-service never carries a hand-mirrored copy of the worker's
tables (the drift that invariant #3 of CLAUDE.md exists to prevent).

Three ways to say the same thing, from most to least explicit:

1. **Explicit keys** on ``framing`` — ``length_prefix_bytes``,
   ``length_encoding``, ``length_byte_order``, ``length_includes_prefix``,
   ``length_includes_header``, ``header_hex`` / ``header``, ``tpdu_*``,
   ``trailer_hex``, ``mode`` / ``start_hex`` / ``end_hex`` / ``checksum``.
2. **A preset** — ``framing.preset`` names one of :data:`FRAMING_PRESETS`,
   generic, structure-describing names (``ascii4``, ``binary2_pad2``,
   ``stx_etx_lrc`` …) rather than any vendor's or scheme's brand. A preset is
   a bundle of defaults: explicit keys set beside it win.
3. **A compact link-level string** — ``framing.link_level`` carries the six
   colon-separated tokens terminal test tools and switch configuration screens
   commonly print, ``<length-encoding>:<counts-prefix>:<counts-header>:
   <byte-order>:<prefix-width>:<header-bytes-dotted-hex>`` (for example
   ``CHARDEC:false:false:BIG:4:`` is a 4-character ASCII-decimal length that
   counts nothing but the body). :func:`parse_link_level` turns one into
   explicit keys so a configuration can be copied straight off such a screen.

:func:`expand_framing` applies 3 then 2 then 1 and returns one explicit dict;
the registry stores the explicit form (plus the ``preset`` / ``link_level``
label it came from) so a record always reads unambiguously, and the worker's
``FrameSpec.from_config`` runs the same expansion so a raw adapter config with a
preset behaves identically.
"""

from __future__ import annotations

from typing import Any

#: Length-prefix encodings. ``hex`` is ASCII hexadecimal digits, one per prefix
#: byte (``002B`` for 43) — used by some terminal gateways and the compact
#: link-level ``CHARHEX`` token.
LENGTH_ENCODINGS = ("binary", "ascii", "bcd", "hex")

#: Vendor / legacy spellings folded to the canonical name. ``chardec`` is the
#: common configuration-screen term for "characters, decimal" (an ASCII digit
#: prefix), ``charhex`` for ASCII hexadecimal digits, ``bch`` for a binary
#: (byte) header.
LENGTH_ENCODING_ALIASES = {
    "chardec": "ascii",
    "asciichardec": "ascii",
    "char": "ascii",
    "decimal": "ascii",
    "bin": "binary",
    "bch": "binary",
    "bchpack": "binary",
    "charhex": "hex",
    "asciicharhex": "hex",
    "bcdpack": "bcd",
}

#: How a frame is delimited on the stream.
#:
#: * ``length_prefix`` (default): ``prefix || tpdu || header || body || trailer``.
#: * ``delimited``: ``start || body || end || checksum`` — the classic
#:   STX … ETX LRC envelope of serial-era terminal links carried over TCP. The
#:   body must not contain the end byte, so it suits text / hex-bitmap
#:   dialects, not binary ones; the worker refuses to frame a body that does.
FRAMING_MODES = ("length_prefix", "delimited")

#: Checksums a ``delimited`` frame may carry after the end byte.
#: ``lrc`` is the XOR of every body byte and the end byte (one byte).
DELIMITED_CHECKSUMS = ("none", "lrc")

#: Generic, structure-named presets. Each is a bundle of explicit framing keys;
#: keys set beside the preset override it. The ``_desc`` entry is for catalogs
#: and editors (``describe_presets``) and is stripped on expansion.
FRAMING_PRESETS: dict[str, dict[str, Any]] = {
    "binary2": {
        "_desc": "2-byte big-endian binary length that counts only the body (the ISO 8583 norm).",
        "length_prefix_bytes": 2,
        "length_encoding": "binary",
        "length_byte_order": "big",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "binary2_inclusive": {
        "_desc": "2-byte big-endian binary length that counts itself as well as the body.",
        "length_prefix_bytes": 2,
        "length_encoding": "binary",
        "length_byte_order": "big",
        "length_includes_prefix": True,
        "length_includes_header": True,
    },
    "binary2_pad2": {
        "_desc": "2-byte binary length followed by two fixed zero bytes the length does not count.",
        "length_prefix_bytes": 2,
        "length_encoding": "binary",
        "length_byte_order": "big",
        "length_includes_prefix": False,
        "length_includes_header": False,
        "header_hex": "0000",
    },
    "binary4_pad14_inclusive": {
        "_desc": "4-byte binary length counting itself and the 14 fixed zero bytes that follow it (gateway-style).",
        "length_prefix_bytes": 4,
        "length_encoding": "binary",
        "length_byte_order": "big",
        "length_includes_prefix": True,
        "length_includes_header": True,
        "header_hex": "00" * 14,
    },
    "bcd2": {
        "_desc": "2-byte packed-BCD length (0x00 0x43 for 43) counting only the body.",
        "length_prefix_bytes": 2,
        "length_encoding": "bcd",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "ascii2": {
        "_desc": "2-character ASCII-decimal length (max 99) counting only the body.",
        "length_prefix_bytes": 2,
        "length_encoding": "ascii",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "ascii4": {
        "_desc": "4-character ASCII-decimal length (0043) counting only the body — the usual POS terminal link.",
        "length_prefix_bytes": 4,
        "length_encoding": "ascii",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "ascii5": {
        "_desc": "5-character ASCII-decimal length counting only the body.",
        "length_prefix_bytes": 5,
        "length_encoding": "ascii",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "ascii6": {
        "_desc": "6-character ASCII-decimal length counting only the body.",
        "length_prefix_bytes": 6,
        "length_encoding": "ascii",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "ascii4_trailer": {
        "_desc": "4-character ASCII-decimal length, body, then a mandatory ETX (0x03) trailer the length does not count.",
        "length_prefix_bytes": 4,
        "length_encoding": "ascii",
        "length_includes_prefix": False,
        "length_includes_header": True,
        "trailer_hex": "03",
    },
    "hex4": {
        "_desc": "4-character ASCII-hexadecimal length (002B for 43) counting only the body.",
        "length_prefix_bytes": 4,
        "length_encoding": "hex",
        "length_includes_prefix": False,
        "length_includes_header": True,
    },
    "stx_etx_lrc": {
        "_desc": "No length: STX (0x02), body, ETX (0x03), then an LRC byte (XOR of body and ETX).",
        "mode": "delimited",
        "start_hex": "02",
        "end_hex": "03",
        "checksum": "lrc",
    },
}

#: Compact link-level tokens that name a whole preset instead of six fields.
LINK_LEVEL_WORDS = {
    "stx-etx-lrc": "stx_etx_lrc",
    "stx_etx_lrc": "stx_etx_lrc",
}

#: Keys ``expand_framing`` may emit (and the registry accepts) under ``framing``.
FRAMING_KEYS = (
    "preset",
    "link_level",
    "mode",
    "length_prefix_bytes",
    "length_encoding",
    "length_byte_order",
    "length_includes_prefix",
    "length_includes_header",
    "header",
    "header_hex",
    "header_bytes",
    "mhdr",
    "tpdu_outbound_hex",
    "tpdu_bytes",
    "trailer_hex",
    "trailer_bytes",
    "start_hex",
    "end_hex",
    "checksum",
    "encoding",
)


class LinkFramingError(ValueError):
    """A framing option is malformed; the message names the key and the fix."""


def normalise_length_encoding(value: object) -> str:
    """Canonical length-prefix encoding for ``value`` (aliases folded);
    ``None`` / empty is the ``binary`` default."""
    enc = str(value or "binary").strip().lower()
    enc = LENGTH_ENCODING_ALIASES.get(enc, enc)
    if enc not in LENGTH_ENCODINGS:
        raise LinkFramingError(
            f"framing.length_encoding must be one of {', '.join(LENGTH_ENCODINGS)} "
            f"(aliases: {', '.join(sorted(LENGTH_ENCODING_ALIASES))}), got {value!r}"
        )
    return enc


def normalise_mode(value: object) -> str:
    mode = str(value or "length_prefix").strip().lower().replace("-", "_")
    if mode not in FRAMING_MODES:
        raise LinkFramingError(
            f"framing.mode must be one of {', '.join(FRAMING_MODES)}, got {value!r}"
        )
    return mode


def normalise_checksum(value: object) -> str:
    cs = str(value or "none").strip().lower()
    if cs not in DELIMITED_CHECKSUMS:
        raise LinkFramingError(
            f"framing.checksum must be one of {', '.join(DELIMITED_CHECKSUMS)}, got {value!r}"
        )
    return cs


def _parse_bool(token: str, what: str) -> bool:
    t = token.strip().lower()
    if t in ("true", "1", "yes", "y"):
        return True
    if t in ("false", "0", "no", "n", ""):
        return False
    raise LinkFramingError(f"framing.link_level: {what} must be true/false, got {token!r}")


def _dotted_hex(token: str) -> str:
    """``00.00`` / ``00 00`` / ``0000`` -> ``0000`` (validated hex, even length)."""
    hx = token.replace(".", "").replace(" ", "").replace(":", "").strip()
    if not hx:
        return ""
    try:
        bytes.fromhex(hx)
    except ValueError as exc:
        raise LinkFramingError(
            f"framing.link_level: header bytes must be dotted hex like 00.00, got {token!r}"
        ) from exc
    return hx.upper()


def parse_link_level(text: str) -> dict[str, Any]:
    """Explicit framing keys for a compact link-level string.

    ``<length-encoding>:<counts-prefix>:<counts-header>:<byte-order>:
    <prefix-width>:<header-bytes>`` — the sixth token is optional and may be
    empty; dotted hex (``00.00``) is accepted for the header bytes. A single
    word naming a delimiter envelope (``STX-ETX-LRC``) expands to that preset.

    A prefix width of ``0`` (no length at all, "read whatever arrives") is not
    a framing PayProbe will run on a multiplexed connection; the error says to
    pick ``delimited`` mode instead.
    """
    raw = str(text or "").strip()
    if not raw:
        raise LinkFramingError("framing.link_level is empty")
    word = LINK_LEVEL_WORDS.get(raw.lower())
    if word:
        return {k: v for k, v in FRAMING_PRESETS[word].items() if k != "_desc"}
    tokens = raw.split(":")
    if len(tokens) == 5:
        tokens.append("")
    if len(tokens) != 6:
        raise LinkFramingError(
            "framing.link_level must have six colon-separated tokens "
            "<length-encoding>:<counts-prefix>:<counts-header>:<byte-order>:"
            f"<prefix-width>:<header-bytes>, got {raw!r}"
        )
    enc_tok, incl_prefix, incl_header, order_tok, width_tok, pad_tok = tokens
    encoding = normalise_length_encoding(enc_tok)
    try:
        width = int(width_tok.strip() or "0")
    except ValueError as exc:
        raise LinkFramingError(
            f"framing.link_level: prefix width must be an integer, got {width_tok!r}"
        ) from exc
    if width < 1:
        raise LinkFramingError(
            "framing.link_level: a prefix width of 0 means no length prefix at all, "
            "which cannot frame a multiplexed stream; use framing.mode 'delimited' "
            "(STX/ETX/LRC) or a real length width"
        )
    order = (order_tok.strip() or "big").lower()
    if order not in ("big", "little"):
        raise LinkFramingError(
            f"framing.link_level: byte order must be BIG or LITTLE, got {order_tok!r}"
        )
    out: dict[str, Any] = {
        "mode": "length_prefix",
        "length_prefix_bytes": width,
        "length_encoding": encoding,
        "length_byte_order": order,
        "length_includes_prefix": _parse_bool(incl_prefix, "counts-prefix"),
        "length_includes_header": _parse_bool(incl_header, "counts-header"),
    }
    pad = _dotted_hex(pad_tok)
    if pad:
        out["header_hex"] = pad
    return out


def describe_presets() -> list[dict[str, Any]]:
    """Catalog rows for editors: ``[{name, description, keys}]``."""
    rows = []
    for name, spec in FRAMING_PRESETS.items():
        rows.append(
            {
                "name": name,
                "description": spec.get("_desc", ""),
                "keys": {k: v for k, v in spec.items() if k != "_desc"},
            }
        )
    return rows


def expand_framing(framing: dict[str, Any] | None) -> dict[str, Any]:
    """One explicit framing dict from explicit keys + ``preset`` + ``link_level``.

    Precedence (lowest to highest): ``link_level`` tokens, ``preset`` bundle,
    explicit keys. The ``preset`` and ``link_level`` labels are kept in the
    result so a record remembers where its values came from. Normalises
    ``length_encoding`` / ``mode`` / ``checksum`` spellings; raises
    :class:`LinkFramingError` on anything malformed. Never mutates its input.
    """
    src = dict(framing or {})
    if not isinstance(framing, (dict, type(None))):
        raise LinkFramingError("framing must be an object")
    out: dict[str, Any] = {}
    link_level = src.get("link_level")
    if link_level not in (None, ""):
        out.update(parse_link_level(str(link_level)))
    preset = src.get("preset")
    if preset not in (None, ""):
        key = str(preset).strip().lower().replace("-", "_")
        if key not in FRAMING_PRESETS:
            raise LinkFramingError(
                f"framing.preset must be one of {', '.join(FRAMING_PRESETS)}, got {preset!r}"
            )
        out.update({k: v for k, v in FRAMING_PRESETS[key].items() if k != "_desc"})
        src["preset"] = key
    for k, v in src.items():
        if k in ("preset", "link_level"):
            continue
        if v is None:
            continue
        out[k] = v
    if "preset" in src and src["preset"] not in (None, ""):
        out["preset"] = src["preset"]
    if link_level not in (None, ""):
        out["link_level"] = str(link_level)
    # canonical spellings
    if "length_encoding" in out:
        out["length_encoding"] = normalise_length_encoding(out["length_encoding"])
    out["mode"] = normalise_mode(out.get("mode"))
    if out["mode"] == "delimited":
        out["checksum"] = normalise_checksum(out.get("checksum"))
        out.setdefault("start_hex", "02")
        out.setdefault("end_hex", "03")
    elif "checksum" in out:
        out.pop("checksum")
    if out["mode"] == "length_prefix" and out.get("length_prefix_bytes") is None:
        pass  # the worker's default (2) applies; keep records sparse
    return out


__all__ = [
    "DELIMITED_CHECKSUMS",
    "FRAMING_KEYS",
    "FRAMING_MODES",
    "FRAMING_PRESETS",
    "LENGTH_ENCODINGS",
    "LENGTH_ENCODING_ALIASES",
    "LINK_LEVEL_WORDS",
    "LinkFramingError",
    "describe_presets",
    "expand_framing",
    "normalise_checksum",
    "normalise_length_encoding",
    "normalise_mode",
    "parse_link_level",
]
