"""The one ISO 8583 wire codec (ADR-0011).

Packs ``{DE: logical value}`` maps into wire bytes and unpacks wire bytes back,
under a **profile** that says how each part of the message is encoded. The
logical values are the same whatever the profile (digits for ``n``, text for the
text classes, uppercase hex for ``b``) so a message built under one profile and
analysed under the same one round-trips exactly.

Profiles are five axes plus an optional MTI axis::

    bitmap   hex | binary | ebcdic      16 hex chars, 8 raw bytes, or hex chars in EBCDIC
    numeric  ascii | bcd | ebcdic       ``n`` fields (and the MTI unless ``mti`` says otherwise)
    text     ascii | ebcdic             ``a`` / ``an`` / ``ans`` / ``z`` fields
    binary   hex | raw                  ``b`` fields as hex text or raw bytes
    length   ascii | bcd | binary | ebcdic   the LL / LLL variable-length indicators
    mti      ascii | bcd | ebcdic       optional; defaults to bcd when numeric is bcd, else ascii

``"ascii"`` (the default, and the historical behaviour) and ``"binary"`` (a
representative binary-bitmap / packed-BCD / raw-binary / BCD-length profile) are
named profiles; a dict overrides individual axes of the ASCII profile.

A field spec may carry its own ``encoding`` dict to override the ``numeric`` /
``text`` / ``binary`` / ``length`` axes for that DE alone, plus three keys that
only matter for packed BCD: ``pad`` (``left`` default, or ``right``), the pad
``pad_nibble`` (default ``0`` when left, ``F`` when right) for odd digit counts,
and ``separator`` (default ``D``) for the field separator of a BCD-packed track
(``z``) field. A ``z`` field is packed as BCD only when *its own* override says
``numeric: bcd``; the profile's numeric axis never touches track data.

Errors: :class:`Iso8583DecodeError` (a ``ValueError``) when the bytes cannot be
a message under the profile (short data, non-digit length indicator, non-hex
bitmap). A DE that the field table does not define stops decoding and is
reported on the result (``error`` + ``truncated: True``), never silently.
"""

from __future__ import annotations

from typing import Any

#: EBCDIC code page used for every ``ebcdic`` axis value (US/Canada; the
#: common mainframe acquirer choice). Clone a format to pin another variant.
EBCDIC = "cp037"

#: Width (digits) of the length indicator per variable ``len_type``.
LEN_PREFIX = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}

AXES = ("bitmap", "numeric", "text", "binary", "length")
AXIS_VALUES: dict[str, tuple[str, ...]] = {
    "bitmap": ("hex", "binary", "ebcdic"),
    "numeric": ("ascii", "bcd", "ebcdic"),
    "text": ("ascii", "ebcdic"),
    "binary": ("hex", "raw"),
    "length": ("ascii", "bcd", "binary", "ebcdic"),
    "mti": ("ascii", "bcd", "ebcdic"),
}

#: The fully-ASCII profile == historical behaviour.
ASCII_OPTS: dict[str, str] = {
    "bitmap": "hex",
    "numeric": "ascii",
    "text": "ascii",
    "binary": "hex",
    "length": "ascii",
}
#: A representative binary profile (binary bitmap + packed BCD + raw binary).
BINARY_OPTS: dict[str, str] = {
    "bitmap": "binary",
    "numeric": "bcd",
    "text": "ascii",
    "binary": "raw",
    "length": "bcd",
}
PROFILES: dict[str, dict[str, str]] = {"ascii": ASCII_OPTS, "binary": BINARY_OPTS}

#: Per-field override keys accepted in ``spec["encoding"]``.
FIELD_OVERRIDE_KEYS = ("numeric", "text", "binary", "length", "pad", "pad_nibble", "separator")


class Iso8583DecodeError(ValueError):
    """The bytes are not an ISO 8583 message under the given profile."""


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #


def resolve_encoding(encoding: Any) -> dict:
    """Normalise an ``encoding`` argument into a full codec-options dict.

    Accepts ``None`` / ``"ascii"`` (default), ``"binary"``, or a dict that
    overrides individual axes of the ASCII profile (an ``mti`` key is kept only
    when given). Unknown profile names and axis values raise ``ValueError`` so a
    misspelt format never silently binds as ASCII.
    """
    if encoding is None:
        return dict(ASCII_OPTS)
    if isinstance(encoding, str):
        try:
            return dict(PROFILES[encoding.lower()])
        except KeyError:
            raise ValueError(
                f"unknown ISO 8583 encoding profile {encoding!r}; "
                f"expected one of {', '.join(sorted(PROFILES))} or an axis dict"
            ) from None
    if isinstance(encoding, dict):
        opts = dict(ASCII_OPTS)
        for axis, value in encoding.items():
            if axis not in AXIS_VALUES:
                continue  # foreign keys (e.g. a per-field 'pad') are not axes
            value = str(value).lower()
            if value not in AXIS_VALUES[axis]:
                raise ValueError(
                    f"ISO 8583 encoding axis {axis!r} cannot be {value!r}; "
                    f"expected one of {', '.join(AXIS_VALUES[axis])}"
                )
            opts[axis] = value
        return opts
    raise ValueError(f"ISO 8583 encoding must be a profile name or an axis dict, got {encoding!r}")


def is_ascii(opts: dict) -> bool:
    """True when ``opts`` is the plain-ASCII profile (wire == text)."""
    return (
        all(opts.get(axis) == ASCII_OPTS[axis] for axis in AXES)
        and opts.get("mti", "ascii") == "ascii"
    )


def field_options(spec: dict, opts: dict) -> dict:
    """The effective options for one DE: profile ``opts`` with the field's own
    ``encoding`` override merged on top (axes validated like a profile)."""
    override = spec.get("encoding")
    if not isinstance(override, dict) or not override:
        return opts
    fo = dict(opts)
    for key in FIELD_OVERRIDE_KEYS:
        if key not in override:
            continue
        value = str(override[key])
        if key in AXIS_VALUES:
            value = value.lower()
            if value not in AXIS_VALUES[key]:
                raise ValueError(
                    f"field encoding {key!r} cannot be {value!r}; "
                    f"expected one of {', '.join(AXIS_VALUES[key])}"
                )
        fo[key] = value
    return fo


def _mti_mode(opts: dict) -> str:
    return opts.get("mti") or ("bcd" if opts["numeric"] == "bcd" else "ascii")


def _pad(fo: dict) -> tuple[str, str]:
    side = str(fo.get("pad", "left")).lower()
    if side not in ("left", "right"):
        raise ValueError(f"field encoding 'pad' must be left or right, got {side!r}")
    nibble = str(fo.get("pad_nibble", "0" if side == "left" else "F")).upper()[:1] or "0"
    return side, nibble


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #


def bcd_encode(digits: str, pad: str = "left", nibble: str = "0") -> bytes:
    """Pack a nibble string (digits, or track data with D/F nibbles) into BCD,
    padding an odd count with ``nibble`` on the ``pad`` side."""
    if len(digits) % 2:
        digits = nibble + digits if pad == "left" else digits + nibble
    try:
        return bytes.fromhex(digits or "")
    except ValueError:
        raise ValueError(f"value {digits!r} is not packable as BCD") from None


def bcd_decode(raw: bytes, count: int, pad: str = "left") -> str:
    """Inverse of :func:`bcd_encode`: ``count`` nibbles out of ``raw``."""
    nibbles = raw.hex().upper()
    if count % 2:
        nibbles = nibbles[1:] if pad == "left" else nibbles[:-1]
    return nibbles[:count]


def _text(value: str, mode: str) -> bytes:
    return value.encode(EBCDIC if mode == "ebcdic" else "ascii")


def _untext(raw: bytes, mode: str) -> str:
    return raw.decode(EBCDIC if mode == "ebcdic" else "ascii", errors="replace")


def _z_is_bcd(spec: dict) -> bool:
    override = spec.get("encoding")
    return isinstance(override, dict) and str(override.get("numeric", "")).lower() == "bcd"


def encode_mti(mti: str, opts: dict) -> bytes:
    mode = _mti_mode(opts)
    if mode == "bcd":
        return bcd_encode(mti)
    return _text(mti, mode)


def encode_bitmap(des: set[int], opts: dict) -> bytes:
    n = 0
    for d in des:
        n |= 1 << (64 - d)
    raw = n.to_bytes(8, "big")
    mode = opts["bitmap"]
    if mode == "binary":
        return raw
    return _text(raw.hex().upper(), mode)


def encode_length(count: int, width: int, fo: dict) -> bytes:
    mode = fo["length"]
    if mode == "bcd":
        return bcd_encode(str(count).zfill(width))
    if mode == "binary":
        nbytes = 1 if width <= 2 else 2
        if count >= 1 << (8 * nbytes):
            raise ValueError(f"length {count} does not fit a {nbytes}-byte binary indicator")
        return count.to_bytes(nbytes, "big")
    return _text(str(count).zfill(width), mode)


def encode_value(value: str, spec: dict, fo: dict) -> tuple[bytes, int]:
    """Wire bytes for one logical value plus the count its length indicator
    carries (bytes for raw binary, otherwise logical units)."""
    typ = (spec.get("type") or "").lower()
    if typ == "b":
        if fo["binary"] == "raw":
            try:
                return bytes.fromhex(value), len(value) // 2
            except ValueError:
                raise ValueError(f"binary field value {value!r} is not hex") from None
        return value.encode("ascii"), len(value)
    if typ == "n":
        if fo["numeric"] == "bcd":
            side, nibble = _pad(fo)
            return bcd_encode(value, side, nibble), len(value)
        return _text(value, fo["numeric"]), len(value)
    if typ == "z" and _z_is_bcd(spec):
        side, nibble = _pad(fo)
        return bcd_encode(value.replace("=", "D").upper(), side, nibble), len(value)
    return _text(value, fo["text"]), len(value)


def _take(data: bytes, pos: int, n: int, what: str) -> bytes:
    if n < 0 or pos + n > len(data):
        raise Iso8583DecodeError(
            f"{what}: message ends after {max(len(data) - pos, 0)} of {n} bytes"
        )
    return data[pos : pos + n]


def decode_mti(data: bytes, pos: int, opts: dict) -> tuple[str, int]:
    mode = _mti_mode(opts)
    if mode == "bcd":
        return _take(data, pos, 2, "MTI").hex().upper(), pos + 2
    return _untext(_take(data, pos, 4, "MTI"), mode), pos + 4


def decode_bitmap(data: bytes, pos: int, opts: dict) -> tuple[set[int], int, str]:
    """One 64-bit bitmap at ``pos``: (bits set, new pos, bitmap as hex)."""
    mode = opts["bitmap"]
    if mode == "binary":
        hexstr = _take(data, pos, 8, "bitmap").hex().upper()
        pos += 8
    else:
        hexstr = _untext(_take(data, pos, 16, "bitmap"), mode).upper()
        pos += 16
        if not all(c in "0123456789ABCDEF" for c in hexstr):
            raise Iso8583DecodeError(f"bitmap {hexstr!r} is not hexadecimal")
    n = int(hexstr, 16)
    return {i + 1 for i in range(64) if n & (1 << (63 - i))}, pos, hexstr


def decode_length(data: bytes, pos: int, width: int, fo: dict, what: str) -> tuple[int, int]:
    mode = fo["length"]
    if mode == "bcd":
        nbytes = (width + 1) // 2
        digits = _take(data, pos, nbytes, what).hex()[-width:]
    elif mode == "binary":
        nbytes = 1 if width <= 2 else 2
        return int.from_bytes(_take(data, pos, nbytes, what), "big"), pos + nbytes
    else:
        nbytes = width
        digits = _untext(_take(data, pos, nbytes, what), mode)
    if not digits.isdigit():
        raise Iso8583DecodeError(f"{what}: length indicator {digits!r} is not decimal digits")
    return int(digits), pos + nbytes


def decode_value(data: bytes, pos: int, spec: dict, fo: dict, what: str) -> tuple[str, int]:
    """One DE at ``pos`` (indicator included for variable fields): (value, new pos)."""
    typ = (spec.get("type") or "").lower()
    lt = spec.get("len_type", "fixed")
    if lt == "fixed":
        count = int(spec.get("length", 0) or 0)
    else:
        count, pos = decode_length(data, pos, LEN_PREFIX.get(lt, 3), fo, what)

    if typ == "b":
        if fo["binary"] == "raw":
            nbytes = count // 2 if lt == "fixed" else count
            return _take(data, pos, nbytes, what).hex().upper(), pos + nbytes
        return _untext(_take(data, pos, count, what), "ascii"), pos + count
    if typ == "n" and fo["numeric"] == "bcd":
        nbytes = (count + 1) // 2
        side, _ = _pad(fo)
        return bcd_decode(_take(data, pos, nbytes, what), count, side), pos + nbytes
    if typ == "n":
        return _untext(_take(data, pos, count, what), fo["numeric"]), pos + count
    if typ == "z" and _z_is_bcd(spec):
        nbytes = (count + 1) // 2
        side, _ = _pad(fo)
        nibbles = bcd_decode(_take(data, pos, nbytes, what), count, side)
        return nibbles.replace("D", str(fo.get("separator", "D"))), pos + nbytes
    return _untext(_take(data, pos, count, what), fo["text"]), pos + count


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #


def pack(mti: str, values: dict, fields: dict[str, dict], encoding: Any = None) -> bytes:
    """Pack ``mti`` + ``{DE: logical value}`` into wire bytes under ``encoding``.

    A DE missing from ``fields`` raises ``KeyError`` (the table is the contract);
    a value that cannot be encoded raises ``ValueError`` naming the DE.
    """
    opts = resolve_encoding(encoding)
    values = {str(k): str(v) for k, v in values.items()}
    des = sorted(int(d) for d in values)
    has_secondary = any(d > 64 for d in des)
    primary = {d for d in des if d <= 64}
    if has_secondary:
        primary.add(1)
    out = bytearray()
    out += encode_mti(str(mti), opts)
    out += encode_bitmap(primary, opts)
    if has_secondary:
        out += encode_bitmap({d - 64 for d in des if d > 64}, opts)
    for d in des:
        key = str(d)
        if key not in fields:
            raise KeyError(f"DE {key} is not defined in the field table")
        spec = fields[key]
        fo = field_options(spec, opts)
        try:
            body, count = encode_value(values[key], spec, fo)
            if spec.get("len_type", "fixed") != "fixed":
                out += encode_length(count, LEN_PREFIX.get(spec.get("len_type"), 3), fo)
        except ValueError as exc:
            raise ValueError(f"DE {key}: {exc}") from None
        out += body
    return bytes(out)


def unpack(
    data: bytes,
    fields: dict[str, dict],
    encoding: Any = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Decode wire bytes into ``{mti, de_list, fields, bitmap, trailing}``.

    ``fields`` in the result maps ``"<de>"`` to ``{"name", "value"}``. A DE the
    table does not define stops decoding: the entry gets ``error`` and the result
    carries ``error`` + ``truncated: True``. Structural failures raise
    :class:`Iso8583DecodeError`; with ``strict=False`` a failure inside one DE is
    reported the same way as an unknown DE instead (analysis mode).
    """
    opts = resolve_encoding(encoding)
    data = bytes(data)
    mti, pos = decode_mti(data, 0, opts)
    present, pos, primary_hex = decode_bitmap(data, pos, opts)
    secondary_hex: str | None = None
    if 1 in present:
        present.discard(1)
        more, pos, secondary_hex = decode_bitmap(data, pos, opts)
        present |= {b + 64 for b in more}

    parsed: dict[str, dict] = {}
    result: dict[str, Any] = {
        "mti": mti,
        "de_list": sorted(present),
        "fields": parsed,
        "bitmap": {"primary": primary_hex, "secondary": secondary_hex},
    }
    for de in sorted(present):
        key = str(de)
        spec = fields.get(key)
        if not spec:
            parsed[key] = {"name": "(unknown)", "value": "", "error": "DE not in spec"}
            result["error"] = f"DE {de}: not in field table; decoding stopped"
            result["truncated"] = True
            break
        try:
            value, pos = decode_value(data, pos, spec, field_options(spec, opts), f"DE {de}")
        except (Iso8583DecodeError, ValueError) as exc:
            if strict:
                if isinstance(exc, Iso8583DecodeError):
                    raise
                raise Iso8583DecodeError(f"DE {de}: {exc}") from None
            parsed[key] = {"name": spec.get("name", ""), "value": "", "error": str(exc)}
            result["error"] = f"DE {de}: {exc}"
            result["truncated"] = True
            break
        parsed[key] = {"name": spec.get("name", ""), "value": value}
    result["trailing"] = data[pos:] or None
    return result


__all__ = [
    "ASCII_OPTS",
    "AXES",
    "AXIS_VALUES",
    "BINARY_OPTS",
    "EBCDIC",
    "FIELD_OVERRIDE_KEYS",
    "LEN_PREFIX",
    "PROFILES",
    "Iso8583DecodeError",
    "bcd_decode",
    "bcd_encode",
    "decode_bitmap",
    "decode_length",
    "decode_mti",
    "decode_value",
    "encode_bitmap",
    "encode_length",
    "encode_mti",
    "encode_value",
    "field_options",
    "is_ascii",
    "pack",
    "resolve_encoding",
    "unpack",
]
