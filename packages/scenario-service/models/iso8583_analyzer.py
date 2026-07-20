"""ISO 8583 message analyzer + validating builder.

Powers the portal's ISO 8583 Inspector and the field-level builder/validator:

* ``analyze_message`` decodes a wire message into MTI + bitmap + per-DE rows,
  validates each field against its spec (data-element class / length), and parses
  structured data elements (EMV BER-TLV in DE 55).
* ``build_message`` validates a ``{DE: value}`` map against the field table and
  packs it, returning the message and any validation errors.

**Wire encodings.** Both functions take an optional ``encoding`` argument so the
codec matches real switches, not just the ASCII teaching form:

* ``"ascii"`` (default) — the historical representation: hex-text bitmap, digits
  as ASCII, binary fields as hex text, ASCII length prefixes. The wire message is
  a plain ``str``.
* ``"binary"`` — a common binary profile: 8-byte binary bitmap, **BCD** (packed
  decimal) numeric fields, **raw** binary fields, BCD length prefixes. The wire
  message is exchanged as an uppercase **hex string** of the raw bytes.
* a ``dict`` for fine control, with keys ``bitmap`` (``hex|binary``), ``numeric``
  (``ascii|bcd``), ``text`` (``ascii|ebcdic``), ``binary`` (``hex|raw``) and
  ``length`` (``ascii|bcd``).

The logical field *values* (digits, text, hex for binary fields) are identical
across encodings — only the wire bytes change — so a value built in one encoding
round-trips when analyzed in the same one.

Self-contained on purpose (no import from the worker): the field-table shape is
the same one the Message Format registry stores —
``{ "<de>": {"name", "len_type": "fixed|llvar|lllvar", "length", "type"} }``.
"""
from __future__ import annotations

from typing import Any

#: EBCDIC code page used for ``text == "ebcdic"`` (US/Canada; common on mainframe
#: acquirer hosts). Override per integration by cloning the format if you need a
#: different national variant.
_EBCDIC = "cp037"

# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #

#: Width (digits/bytes-of-digits) of the length indicator per variable len_type.
_LEN_PREFIX = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}


def _prefix_width(lt: str) -> int:
    """Length-indicator width for a variable ``len_type`` (defaults to 3)."""
    return _LEN_PREFIX.get(lt, 3)


def _bits_from_hex(h: str) -> set[int]:
    if not h:
        return set()
    n = int(h, 16)
    w = len(h) * 4
    return {i + 1 for i in range(w) if n & (1 << (w - 1 - i))}


def _bitmap(des: set[int], width: int) -> str:
    n = 0
    for d in des:
        n |= 1 << (width - d)
    return format(n, "0%dX" % (width // 4))


def iso_pack(mti: str, values: dict, fields: dict[str, dict]) -> str:
    values = {str(k): str(v) for k, v in values.items()}
    des = sorted(int(d) for d in values)
    has_sec = any(d > 64 for d in des)
    primary = {d for d in des if d <= 64}
    if has_sec:
        primary.add(1)
    out = mti + _bitmap(primary, 64)
    if has_sec:
        out += _bitmap({d - 64 for d in des if d > 64}, 64)
    for d in des:
        sp = fields[str(d)]
        v = values[str(d)]
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            out += v
        else:
            out += str(len(v)).zfill(_prefix_width(lt)) + v
    return out


# --------------------------------------------------------------------------- #
# wire encodings — binary bitmap, BCD numerics, EBCDIC text, raw binary fields
# --------------------------------------------------------------------------- #

#: a fully-ASCII codec profile (== historical behaviour)
_ASCII_OPTS = {"bitmap": "hex", "numeric": "ascii", "text": "ascii",
               "binary": "hex", "length": "ascii"}
#: a representative binary profile (binary bitmap + packed-BCD + raw binary)
_BINARY_OPTS = {"bitmap": "binary", "numeric": "bcd", "text": "ascii",
                "binary": "raw", "length": "bcd"}


def resolve_encoding(encoding: Any) -> dict:
    """Normalise an ``encoding`` argument into a full codec-options dict.

    Accepts ``None``/``"ascii"`` (default), ``"binary"``, or a partial ``dict``
    that overrides individual axes of the ASCII profile.
    """
    if encoding is None or encoding == "ascii":
        return dict(_ASCII_OPTS)
    if encoding == "binary":
        return dict(_BINARY_OPTS)
    if isinstance(encoding, dict):
        opts = dict(_ASCII_OPTS)
        opts.update({k: encoding[k] for k in _ASCII_OPTS if k in encoding})
        return opts
    return dict(_ASCII_OPTS)


def _is_ascii(opts: dict) -> bool:
    return opts == _ASCII_OPTS


def _bcd_encode(digits: str) -> bytes:
    """Pack a decimal string into BCD bytes (left zero-pad to a whole byte)."""
    if len(digits) % 2:
        digits = "0" + digits
    return bytes.fromhex(digits or "")


def _enc_bitmap_bytes(des: set[int], opts: dict) -> bytes:
    n = 0
    for d in des:
        n |= 1 << (64 - d)
    raw = n.to_bytes(8, "big")
    return raw if opts["bitmap"] == "binary" else raw.hex().upper().encode("ascii")


def _enc_value_bytes(v: str, typ: str, opts: dict) -> bytes:
    if typ == "b":
        return bytes.fromhex(v) if opts["binary"] == "raw" else v.encode("ascii")
    if typ == "n":
        return _bcd_encode(v) if opts["numeric"] == "bcd" else v.encode("ascii")
    return v.encode(_EBCDIC) if opts["text"] == "ebcdic" else v.encode("ascii")


def _value_count(v: str, typ: str, opts: dict) -> int:
    """Length-indicator value: byte count for raw-binary fields, else char count."""
    if typ == "b" and opts["binary"] == "raw":
        return len(v) // 2
    return len(v)


def _enc_len_bytes(count: int, width: int, opts: dict) -> bytes:
    if opts["length"] == "bcd":
        return _bcd_encode(str(count).zfill(width))
    return str(count).zfill(width).encode("ascii")


def iso_pack_bytes(mti: str, values: dict, fields: dict[str, dict], opts: dict) -> bytes:
    """Pack a message into raw bytes under the given codec options."""
    values = {str(k): str(v) for k, v in values.items()}
    des = sorted(int(d) for d in values)
    has_sec = any(d > 64 for d in des)
    primary = {d for d in des if d <= 64}
    if has_sec:
        primary.add(1)
    out = bytearray()
    out += _bcd_encode(mti) if opts["numeric"] == "bcd" else mti.encode("ascii")
    out += _enc_bitmap_bytes(primary, opts)
    if has_sec:
        out += _enc_bitmap_bytes({d - 64 for d in des if d > 64}, opts)
    for d in des:
        sp = fields[str(d)]
        v = values[str(d)]
        typ = (sp.get("type") or "").lower()
        lt = sp.get("len_type", "fixed")
        body = _enc_value_bytes(v, typ, opts)
        if lt == "fixed":
            out += body
        else:
            width = _prefix_width(lt)
            out += _enc_len_bytes(_value_count(v, typ, opts), width, opts)
            out += body
    return bytes(out)


def _bitmap_width_bytes(opts: dict) -> int:
    return 8 if opts["bitmap"] == "binary" else 16


def _dec_bitmap(data: bytes, pos: int, opts: dict) -> tuple[set[int], int, str]:
    w = _bitmap_width_bytes(opts)
    chunk = data[pos:pos + w]
    pos += w
    if opts["bitmap"] == "binary":
        hexstr = chunk.hex().upper()
    else:
        hexstr = chunk.decode("ascii")
    return _bits_from_hex(hexstr), pos, hexstr


def _dec_field_bytes(data: bytes, pos: int, sp: dict, opts: dict) -> tuple[str, int]:
    """Decode one DE from ``data`` at ``pos``; return (logical value, new pos)."""
    typ = (sp.get("type") or "").lower()
    lt = sp.get("len_type", "fixed")
    declared = int(sp.get("length", 0) or 0)

    if lt == "fixed":
        if typ == "n":
            if opts["numeric"] == "bcd":
                nbytes = (declared + 1) // 2
                digits = data[pos:pos + nbytes].hex().upper()
                return digits[-declared:] if declared else digits, pos + nbytes
            return data[pos:pos + declared].decode("ascii"), pos + declared
        if typ == "b":
            if opts["binary"] == "raw":
                nbytes = declared // 2
                return data[pos:pos + nbytes].hex().upper(), pos + nbytes
            return data[pos:pos + declared].decode("ascii"), pos + declared
        # text classes
        enc = _EBCDIC if opts["text"] == "ebcdic" else "ascii"
        return data[pos:pos + declared].decode(enc), pos + declared

    # variable length: read the indicator first
    width = _prefix_width(lt)
    if opts["length"] == "bcd":
        nbytes = (width + 1) // 2
        count = int(data[pos:pos + nbytes].hex()[-width:] or 0)
        pos += nbytes
    else:
        count = int(data[pos:pos + width].decode("ascii") or 0)
        pos += width
    if typ == "n" and opts["numeric"] == "bcd":
        nbytes = (count + 1) // 2
        digits = data[pos:pos + nbytes].hex().upper()
        return digits[-count:] if count else "", pos + nbytes
    if typ == "b" and opts["binary"] == "raw":
        return data[pos:pos + count].hex().upper(), pos + count
    enc = _EBCDIC if (typ not in ("n", "b") and opts["text"] == "ebcdic") else "ascii"
    return data[pos:pos + count].decode(enc), pos + count


# --------------------------------------------------------------------------- #
# validation — ISO 8583 data-element classes (n / a / an / ans / b / z …)
# --------------------------------------------------------------------------- #

def validate_field(value: str, spec: dict) -> str | None:
    """Return an error string if ``value`` violates the DE spec, else None.

    Length is checked first (fixed = exact, var = max), then the data-element
    class declared in ``type``: ``n`` numeric, ``a`` alphabetic, ``an``
    alphanumeric, ``ans``/``p`` alphanumeric-special (printable), ``z`` track
    2/3 data, ``b`` binary (hex, whole bytes).
    """
    value = str(value)
    lt = spec.get("len_type", "fixed")
    length = int(spec.get("length", 0) or 0)
    typ = (spec.get("type") or "").lower()

    if lt == "fixed" and length and len(value) != length:
        return f"expected {length} chars, got {len(value)}"
    if lt in ("llvar", "lllvar", "llllvar", "lllllvar") and length and len(value) > length:
        return f"exceeds max length {length} (got {len(value)})"

    if not value:
        return None
    if typ == "n":
        if not value.isdigit():
            return "must be numeric"
    elif typ == "a":
        if not value.isalpha():
            return "must be alphabetic"
    elif typ == "an":
        if not value.isalnum():
            return "must be alphanumeric"
    elif typ in ("ans", "anp", "p", "s"):
        if any(not (0x20 <= ord(c) <= 0x7E) for c in value):
            return "must be printable ASCII (ans)"
    elif typ == "z":
        if any(c not in "0123456789ABCDEFabcdef=D" for c in value):
            return "must be track 2/3 data (digits, '=', 'D')"
    elif typ == "b":
        try:
            int(value, 16)
        except ValueError:
            return "must be hexadecimal"
        if len(value) % 2:
            return "binary field must have an even number of hex digits"
    return None


# --------------------------------------------------------------------------- #
# EMV BER-TLV (DE 55 and other constructed fields)
# --------------------------------------------------------------------------- #

EMV_TAGS: dict[str, str] = {
    "4F": "Application Identifier (AID)", "50": "Application Label",
    "57": "Track 2 Equivalent Data", "5A": "Application PAN",
    "82": "Application Interchange Profile (AIP)", "84": "Dedicated File Name",
    "8A": "Authorisation Response Code", "95": "Terminal Verification Results (TVR)",
    "9A": "Transaction Date", "9C": "Transaction Type",
    "5F2A": "Transaction Currency Code", "5F34": "PAN Sequence Number",
    "9F02": "Amount, Authorised", "9F03": "Amount, Other",
    "9F10": "Issuer Application Data (IAD)", "9F1A": "Terminal Country Code",
    "9F26": "Application Cryptogram (ARQC/TC/AAC)", "9F27": "Cryptogram Information Data",
    "9F33": "Terminal Capabilities", "9F34": "CVM Results",
    "9F36": "Application Transaction Counter (ATC)",
    "9F37": "Unpredictable Number", "9F1E": "IFD Serial Number",
    "9F09": "Application Version Number", "9F35": "Terminal Type",
    "9F53": "Transaction Category Code", "9F6E": "Form Factor Indicator",
}


def parse_tlv(hexstr: str) -> list[dict]:
    """Parse a BER-TLV byte string (hex) into a nested tag/length/value tree."""
    data = bytes.fromhex(hexstr)
    return _tlv(data, 0, len(data))


def _encode_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return bytes([0x80 | len(body)]) + body


def build_tlv(nodes: list[dict]) -> str:
    """Inverse of :func:`parse_tlv` — encode tag/value (or tag/children) nodes
    into a BER-TLV hex string. A node with ``children`` is constructed; a node
    with ``value`` (hex) is primitive."""
    out = bytearray()
    for node in nodes:
        tag = bytes.fromhex(node["tag"])
        children = node.get("children")
        if children:
            value = bytes.fromhex(build_tlv(children))
        else:
            value = bytes.fromhex((node.get("value") or "").replace(" ", ""))
        out += tag
        out += _encode_len(len(value))
        out += value
    return out.hex().upper()


def diff_messages(a: str, b: str, fields: dict[str, dict], encoding: Any = None) -> dict:
    """Field-level diff of two ISO 8583 messages (same field table + encoding)."""
    aa = analyze_message(a, fields, encoding)
    bb = analyze_message(b, fields, encoding)
    amap = {r["de"]: r for r in aa["fields"] if "de" in r}
    bmap = {r["de"]: r for r in bb["fields"] if "de" in r}
    rows = []
    for de in sorted(set(amap) | set(bmap)):
        av = amap.get(de, {}).get("value")
        bv = bmap.get(de, {}).get("value")
        name = amap.get(de, {}).get("name") or bmap.get(de, {}).get("name") or ""
        if de in amap and de not in bmap:
            change = "removed"
        elif de in bmap and de not in amap:
            change = "added"
        elif av != bv:
            change = "changed"
        else:
            change = "same"
        rows.append({"de": de, "name": name, "a": av, "b": bv, "change": change})
    return {
        "mti_a": aa["mti"], "mti_b": bb["mti"],
        "mti_changed": aa["mti"] != bb["mti"],
        "fields": rows,
        "summary": {
            "added": sum(r["change"] == "added" for r in rows),
            "removed": sum(r["change"] == "removed" for r in rows),
            "changed": sum(r["change"] == "changed" for r in rows),
            "same": sum(r["change"] == "same" for r in rows),
        },
    }


def _tlv(data: bytes, i: int, end: int) -> list[dict]:
    out: list[dict] = []
    while i < end:
        first = data[i]
        tag_bytes = [first]
        i += 1
        if first & 0x1F == 0x1F:  # multi-byte tag
            while i < end:
                tag_bytes.append(data[i])
                more = data[i] & 0x80
                i += 1
                if not more:
                    break
        tag = bytes(tag_bytes).hex().upper()
        constructed = bool(first & 0x20)
        if i >= end:
            break
        length_byte = data[i]
        i += 1
        if length_byte & 0x80:
            n = length_byte & 0x7F
            length = int.from_bytes(data[i:i + n], "big")
            i += n
        else:
            length = length_byte
        value = data[i:i + length]
        i += length
        node: dict[str, Any] = {"tag": tag, "name": EMV_TAGS.get(tag),
                                "length": length}
        if constructed:
            node["children"] = _tlv(value, 0, len(value))
        else:
            node["value"] = value.hex().upper()
        out.append(node)
    return out


# --------------------------------------------------------------------------- #
# semantic interpretation (MTI breakdown + common DE value dictionaries)
# --------------------------------------------------------------------------- #

_MTI_VERSION = {"0": "ISO 8583:1987", "1": "ISO 8583:1993", "2": "ISO 8583:2003",
                "8": "National", "9": "Private"}
_MTI_CLASS = {"1": "Authorization", "2": "Financial", "3": "File actions",
              "4": "Reversal/Chargeback", "5": "Reconciliation",
              "6": "Administrative", "7": "Fee collection",
              "8": "Network management", "9": "Reserved"}
_MTI_FUNCTION = {"0": "Request", "1": "Request response", "2": "Advice",
                 "3": "Advice response", "4": "Notification",
                 "8": "Response acknowledgement", "9": "Negative acknowledgement"}
_MTI_ORIGIN = {"0": "Acquirer", "1": "Acquirer repeat", "2": "Issuer",
               "3": "Issuer repeat", "4": "Other", "5": "Other repeat"}

RESPONSE_CODES = {
    "00": "Approved", "01": "Refer to card issuer", "03": "Invalid merchant",
    "04": "Pick up card", "05": "Do not honor", "12": "Invalid transaction",
    "13": "Invalid amount", "14": "Invalid card number", "30": "Format error",
    "41": "Lost card", "43": "Stolen card", "51": "Insufficient funds",
    "54": "Expired card", "55": "Incorrect PIN", "57": "Txn not permitted to cardholder",
    "58": "Txn not permitted to terminal", "61": "Exceeds withdrawal limit",
    "62": "Restricted card", "65": "Exceeds withdrawal frequency",
    "75": "PIN tries exceeded", "91": "Issuer or switch inoperative",
    "96": "System malfunction",
}
PROCESSING_CODES = {
    "00": "Purchase (goods/services)", "01": "Cash withdrawal",
    "09": "Purchase with cashback", "17": "Cash disbursement",
    "20": "Refund", "28": "Payment", "30": "Balance inquiry",
}
POS_ENTRY_MODES = {
    "01": "Manual", "02": "Magstripe", "05": "Chip (ICC)",
    "07": "Contactless ICC", "80": "Fallback magstripe",
    "90": "Magstripe (full track)", "91": "Contactless magstripe",
}
CURRENCY_CODES = {
    "840": "USD", "978": "EUR", "826": "GBP", "392": "JPY", "756": "CHF",
    "124": "CAD", "036": "AUD", "156": "CNY", "949": "TRY", "981": "GEL",
    "643": "RUB", "356": "INR",
}


def decode_mti(mti: str) -> dict | None:
    if not (isinstance(mti, str) and len(mti) == 4 and mti.isdigit()):
        return None
    return {
        "mti": mti,
        "version": _MTI_VERSION.get(mti[0], mti[0]),
        "message_class": _MTI_CLASS.get(mti[1], mti[1]),
        "function": _MTI_FUNCTION.get(mti[2], mti[2]),
        "origin": _MTI_ORIGIN.get(mti[3], mti[3]),
    }


def _fmt_amount(value: str) -> str | None:
    if value and value.isdigit():
        return f"{int(value) / 100:.2f}"
    return None


def interpret_de(de: int, value: str) -> str | None:
    """Human-readable meaning for a known data element value."""
    v = value or ""
    if de == 39:
        return RESPONSE_CODES.get(v)
    if de == 3 and len(v) >= 2:
        return PROCESSING_CODES.get(v[:2])
    if de == 22 and len(v) >= 2:
        return POS_ENTRY_MODES.get(v[:2])
    if de == 49:
        return CURRENCY_CODES.get(v)
    if de in (4, 5, 6):
        amt = _fmt_amount(v)
        return f"{amt} (major units)" if amt else None
    if de == 2 and len(v) >= 10:           # mask PAN
        return v[:6] + "*" * (len(v) - 10) + v[-4:]
    if de == 7 and len(v) == 10:
        return f"{v[0:2]}-{v[2:4]} {v[4:6]}:{v[6:8]}:{v[8:10]} (MMDD hh:mm:ss)"
    if de == 12 and len(v) >= 6:
        return f"{v[0:2]}:{v[2:4]}:{v[4:6]}"
    if de == 13 and len(v) == 4:
        return f"{v[0:2]}-{v[2:4]} (MM-DD)"
    return None


# --------------------------------------------------------------------------- #
# analyze / build
# --------------------------------------------------------------------------- #

#: data elements that carry BER-TLV (EMV) content
_TLV_DES = {"55"}


def _row_for(de: int, spec: dict, raw: str) -> tuple[dict, list[str]]:
    """Build the analysis row for one decoded DE (shared by both wire paths)."""
    errs: list[str] = []
    row: dict[str, Any] = {
        "de": de, "name": spec.get("name", ""), "type": spec.get("type", ""),
        "len_type": spec.get("len_type", "fixed"), "length": spec.get("length"),
        "value": raw, "length_actual": len(raw),
    }
    err = validate_field(raw, spec)
    if err:
        row["error"] = err
        errs.append(f"DE{de}: {err}")
    interp = interpret_de(de, raw)
    if interp:
        row["interpretation"] = interp
    if str(de) in _TLV_DES and raw:
        try:
            row["tlv"] = parse_tlv(raw)
        except Exception as exc:  # noqa: BLE001
            row["tlv_error"] = str(exc)
    return row, errs


def _analyze_binary(message: str, fields: dict[str, dict], opts: dict) -> dict:
    """Decode a non-ASCII (binary/BCD/EBCDIC) message supplied as a hex string."""
    errors: list[str] = []
    try:
        data = bytes.fromhex("".join((message or "").split()))
    except ValueError as exc:
        return {"mti": "", "bitmap": None, "fields": [],
                "errors": [f"not a valid hex byte string: {exc}"], "trailing": None}

    pos = 0
    if opts["numeric"] == "bcd":
        mti = data[pos:pos + 2].hex().upper()
        pos += 2
    else:
        mti = data[pos:pos + 4].decode("ascii", "replace")
        pos += 4
    present, pos, primary_hex = _dec_bitmap(data, pos, opts)
    secondary_hex = None
    if 1 in present:
        present.discard(1)
        more, pos, secondary_hex = _dec_bitmap(data, pos, opts)
        present |= {b + 64 for b in more}

    rows: list[dict] = []
    for de in sorted(present):
        spec = fields.get(str(de))
        if not spec:
            rows.append({"de": de, "name": "(unknown)", "error": "DE not in field table"})
            errors.append(f"DE{de}: not in field table — cannot decode further")
            break
        try:
            raw, pos = _dec_field_bytes(data, pos, spec, opts)
        except Exception as exc:  # noqa: BLE001
            rows.append({"de": de, "name": spec.get("name", ""), "error": str(exc)})
            errors.append(f"DE{de}: {exc}")
            break
        row, errs = _row_for(de, spec, raw)
        rows.append(row)
        errors.extend(errs)

    return {
        "mti": mti, "mti_info": decode_mti(mti),
        "bitmap": {"primary": primary_hex, "secondary": secondary_hex,
                   "present": sorted(present)},
        "fields": rows, "errors": errors,
        "trailing": data[pos:].hex().upper() or None,
    }


def analyze_message(message: str, fields: dict[str, dict], encoding: Any = None) -> dict:
    """Decode an ISO 8583 message into a structured, validated view.

    ``encoding`` selects the wire codec (see the module docstring). For any
    non-ASCII encoding ``message`` is a hex string of the raw bytes.
    """
    opts = resolve_encoding(encoding)
    if not _is_ascii(opts):
        return _analyze_binary(message, fields, opts)
    msg = "".join((message or "").split())
    errors: list[str] = []
    if len(msg) < 4 + 16:
        return {"mti": msg[:4], "bitmap": None, "fields": [],
                "errors": ["message too short to contain an MTI + bitmap"],
                "trailing": None}

    pos = 0
    mti = msg[0:4]
    pos = 4
    primary_hex = msg[pos:pos + 16]
    pos += 16
    present = _bits_from_hex(primary_hex)
    secondary_hex = None
    if 1 in present:
        secondary_hex = msg[pos:pos + 16]
        pos += 16
        present |= {b + 64 for b in _bits_from_hex(secondary_hex)}
        present.discard(1)

    rows: list[dict] = []
    for de in sorted(present):
        spec = fields.get(str(de))
        if not spec:
            rows.append({"de": de, "name": "(unknown)", "error": "DE not in field table"})
            errors.append(f"DE{de}: not in field table — cannot decode further")
            break
        lt = spec.get("len_type", "fixed")
        if lt == "fixed":
            ln = int(spec.get("length", 0) or 0)
            raw = msg[pos:pos + ln]
            pos += ln
        elif lt == "llvar":
            ln = int(msg[pos:pos + 2] or 0)
            pos += 2
            raw = msg[pos:pos + ln]
            pos += ln
        else:
            ln = int(msg[pos:pos + 3] or 0)
            pos += 3
            raw = msg[pos:pos + ln]
            pos += ln
        row: dict[str, Any] = {
            "de": de, "name": spec.get("name", ""), "type": spec.get("type", ""),
            "len_type": lt, "length": spec.get("length"),
            "value": raw, "length_actual": len(raw),
        }
        err = validate_field(raw, spec)
        if err:
            row["error"] = err
            errors.append(f"DE{de}: {err}")
        interp = interpret_de(de, raw)
        if interp:
            row["interpretation"] = interp
        if str(de) in _TLV_DES and raw:
            try:
                row["tlv"] = parse_tlv(raw)
            except Exception as exc:  # noqa: BLE001
                row["tlv_error"] = str(exc)
        rows.append(row)

    return {
        "mti": mti,
        "mti_info": decode_mti(mti),
        "bitmap": {"primary": primary_hex, "secondary": secondary_hex,
                   "present": sorted(present)},
        "fields": rows,
        "errors": errors,
        "trailing": msg[pos:] or None,
    }


def validate_values(mti: str, values: dict, fields: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    if not (isinstance(mti, str) and len(mti) == 4 and mti.isdigit()):
        errors.append("MTI must be exactly 4 digits")
    for de, val in values.items():
        spec = fields.get(str(de))
        if not spec:
            errors.append(f"DE{de}: not in field table")
            continue
        err = validate_field(str(val), spec)
        if err:
            errors.append(f"DE{de}: {err}")
    return errors


def build_message(mti: str, values: dict, fields: dict[str, dict],
                  encoding: Any = None) -> dict:
    """Validate ``{DE: value}`` then pack. Returns ``{message, errors}``.

    With the default ASCII ``encoding`` the message is a plain string; under any
    binary encoding it is an uppercase hex string of the raw wire bytes.
    """
    errors = validate_values(mti, values, fields)
    message = None
    if not errors:
        opts = resolve_encoding(encoding)
        try:
            if _is_ascii(opts):
                message = iso_pack(mti, values, fields)
            else:
                message = iso_pack_bytes(mti, values, fields, opts).hex().upper()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return {"message": message, "errors": errors}


#: ISO 8583:1987 default field table (used when no Message Format is given).
#: Names, classes and lengths follow the published 1987 data-element directory.
DEFAULT_FIELDS: dict[str, dict] = {
    "2":  {"name": "Primary Account Number (PAN)",      "len_type": "llvar",  "length": 19, "type": "n"},
    "3":  {"name": "Processing Code",                   "len_type": "fixed",  "length": 6,  "type": "n"},
    "4":  {"name": "Amount, Transaction",               "len_type": "fixed",  "length": 12, "type": "n"},
    "5":  {"name": "Amount, Settlement",                "len_type": "fixed",  "length": 12, "type": "n"},
    "6":  {"name": "Amount, Cardholder Billing",        "len_type": "fixed",  "length": 12, "type": "n"},
    "7":  {"name": "Transmission Date & Time (MMDDhhmmss)", "len_type": "fixed", "length": 10, "type": "n"},
    "9":  {"name": "Conversion Rate, Settlement",       "len_type": "fixed",  "length": 8,  "type": "n"},
    "10": {"name": "Conversion Rate, Cardholder Billing", "len_type": "fixed", "length": 8,  "type": "n"},
    "11": {"name": "System Trace Audit Number (STAN)",  "len_type": "fixed",  "length": 6,  "type": "n"},
    "12": {"name": "Time, Local Transaction (hhmmss)",  "len_type": "fixed",  "length": 6,  "type": "n"},
    "13": {"name": "Date, Local Transaction (MMDD)",    "len_type": "fixed",  "length": 4,  "type": "n"},
    "14": {"name": "Date, Expiration (YYMM)",           "len_type": "fixed",  "length": 4,  "type": "n"},
    "15": {"name": "Date, Settlement (MMDD)",           "len_type": "fixed",  "length": 4,  "type": "n"},
    "18": {"name": "Merchant Type (MCC)",               "len_type": "fixed",  "length": 4,  "type": "n"},
    "19": {"name": "Acquiring Institution Country Code", "len_type": "fixed", "length": 3,  "type": "n"},
    "22": {"name": "POS Entry Mode",                    "len_type": "fixed",  "length": 3,  "type": "n"},
    "23": {"name": "Card Sequence Number",              "len_type": "fixed",  "length": 3,  "type": "n"},
    "25": {"name": "POS Condition Code",                "len_type": "fixed",  "length": 2,  "type": "n"},
    "32": {"name": "Acquiring Institution ID Code",     "len_type": "llvar",  "length": 11, "type": "n"},
    "33": {"name": "Forwarding Institution ID Code",    "len_type": "llvar",  "length": 11, "type": "n"},
    "35": {"name": "Track 2 Data",                      "len_type": "llvar",  "length": 37, "type": "z"},
    "36": {"name": "Track 3 Data",                      "len_type": "lllvar", "length": 104, "type": "z"},
    "37": {"name": "Retrieval Reference Number",        "len_type": "fixed",  "length": 12, "type": "an"},
    "38": {"name": "Authorization ID Response",         "len_type": "fixed",  "length": 6,  "type": "an"},
    "39": {"name": "Response Code",                     "len_type": "fixed",  "length": 2,  "type": "an"},
    "41": {"name": "Card Acceptor Terminal ID",         "len_type": "fixed",  "length": 8,  "type": "ans"},
    "42": {"name": "Card Acceptor ID Code",             "len_type": "fixed",  "length": 15, "type": "ans"},
    "43": {"name": "Card Acceptor Name/Location",       "len_type": "fixed",  "length": 40, "type": "ans"},
    "44": {"name": "Additional Response Data",          "len_type": "llvar",  "length": 25, "type": "an"},
    "45": {"name": "Track 1 Data",                      "len_type": "llvar",  "length": 76, "type": "an"},
    "48": {"name": "Additional Data — Private",         "len_type": "lllvar", "length": 999, "type": "ans"},
    "49": {"name": "Currency Code, Transaction",        "len_type": "fixed",  "length": 3,  "type": "n"},
    "50": {"name": "Currency Code, Settlement",         "len_type": "fixed",  "length": 3,  "type": "n"},
    "51": {"name": "Currency Code, Cardholder Billing", "len_type": "fixed",  "length": 3,  "type": "n"},
    "52": {"name": "PIN Data",                          "len_type": "fixed",  "length": 16, "type": "b"},
    "53": {"name": "Security Related Control Information", "len_type": "fixed", "length": 16, "type": "n"},
    "54": {"name": "Additional Amounts",                "len_type": "lllvar", "length": 120, "type": "an"},
    "55": {"name": "ICC Data (EMV)",                    "len_type": "lllvar", "length": 999, "type": "b"},
    "60": {"name": "Reserved Private",                  "len_type": "lllvar", "length": 999, "type": "ans"},
    "61": {"name": "Reserved Private",                  "len_type": "lllvar", "length": 999, "type": "ans"},
    "62": {"name": "Reserved Private",                  "len_type": "lllvar", "length": 999, "type": "ans"},
    "63": {"name": "Reserved Private",                  "len_type": "lllvar", "length": 999, "type": "ans"},
    "64": {"name": "Message Authentication Code (MAC)", "len_type": "fixed",  "length": 16, "type": "b"},
    "70": {"name": "Network Management Information Code", "len_type": "fixed", "length": 3,  "type": "n"},
    "90": {"name": "Original Data Elements",            "len_type": "fixed",  "length": 42, "type": "n"},
    "95": {"name": "Replacement Amounts",               "len_type": "fixed",  "length": 42, "type": "an"},
    "100": {"name": "Receiving Institution ID Code",    "len_type": "llvar",  "length": 11, "type": "n"},
    "102": {"name": "Account Identification 1",         "len_type": "llvar",  "length": 28, "type": "ans"},
    "103": {"name": "Account Identification 2",         "len_type": "llvar",  "length": 28, "type": "ans"},
    "128": {"name": "Message Authentication Code (MAC)", "len_type": "fixed", "length": 16, "type": "b"},
}
