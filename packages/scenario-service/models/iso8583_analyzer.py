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
* a ``dict`` for fine control, with keys ``bitmap`` (``hex|binary|ebcdic``),
  ``numeric`` (``ascii|bcd|ebcdic``), ``text`` (``ascii|ebcdic``), ``binary``
  (``hex|raw``), ``length`` (``ascii|bcd|binary|ebcdic``) and optionally ``mti``.

The logical field *values* (digits, text, hex for binary fields) are identical
across encodings — only the wire bytes change — so a value built in one encoding
round-trips when analyzed in the same one.

The codec itself lives once, in ``payprobe_common.iso8583`` (ADR-0011): this
module is the *analysis* layer on top of it (rows, per-field validation messages,
interpretations, TLV trees, diffs). The worker wire path uses the same codec, so
what the Inspector shows is what the socket carried.
"""
from __future__ import annotations

from typing import Any

from payprobe_common import iso8583 as _iso
from payprobe_common.iso8583.tlv import EMV_TAGS, build_tlv, parse_tlv

# Names the API layer and the tests import from here.
ISO8583_1987 = _iso.ISO8583_1987
Iso8583DecodeError = _iso.Iso8583DecodeError
resolve_encoding = _iso.resolve_encoding
validate_field = _iso.validate_field

#: ISO 8583:1987 default field table (used when no Message Format is given) —
#: the shared dictionary, so the Inspector and the wire agree on every DE.
DEFAULT_FIELDS: dict[str, dict] = ISO8583_1987


# --------------------------------------------------------------------------- #
# codec (compatibility surface over payprobe_common.iso8583)
# --------------------------------------------------------------------------- #

def _prefix_width(lt: str) -> int:
    """Length-indicator width for a variable ``len_type`` (defaults to 3)."""
    return _iso.LEN_PREFIX.get(lt, 3)


def iso_pack(mti: str, values: dict, fields: dict[str, dict]) -> str:
    """Pack under the ASCII profile and return the message as text."""
    return _iso.pack(mti, values, fields, "ascii").decode("latin-1")


def iso_pack_bytes(mti: str, values: dict, fields: dict[str, dict], opts: dict) -> bytes:
    """Pack a message into raw bytes under the given codec options."""
    return _iso.pack(mti, values, fields, opts)


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
    """Build the analysis row for one decoded DE."""
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


def _empty(mti: str, *errors: str) -> dict:
    return {"mti": mti, "bitmap": None, "fields": [], "errors": list(errors), "trailing": None}


def analyze_message(message: str, fields: dict[str, dict], encoding: Any = None) -> dict:
    """Decode an ISO 8583 message into a structured, validated view.

    ``encoding`` selects the wire codec (see the module docstring). For any
    non-ASCII encoding ``message`` is a hex string of the raw bytes.
    """
    opts = resolve_encoding(encoding)
    ascii_mode = _iso.is_ascii(opts)
    msg = "".join((message or "").split())
    if ascii_mode:
        if len(msg) < 4 + 16:
            return _empty(msg[:4], "message too short to contain an MTI + bitmap")
        data = msg.encode("latin-1", errors="replace")
    else:
        try:
            data = bytes.fromhex(msg)
        except ValueError as exc:
            return _empty("", f"not a valid hex byte string: {exc}")

    try:
        parsed = _iso.unpack(data, fields, opts, strict=False)
    except Iso8583DecodeError as exc:
        return _empty("", str(exc))

    errors: list[str] = []
    rows: list[dict] = []
    for key, entry in parsed["fields"].items():
        de = int(key)
        if "error" in entry:
            if entry["error"] == "DE not in spec":
                rows.append({"de": de, "name": "(unknown)", "error": "DE not in field table"})
                errors.append(f"DE{de}: not in field table — cannot decode further")
            else:
                rows.append({"de": de, "name": entry.get("name", ""), "error": entry["error"]})
                errors.append(f"DE{de}: {entry['error']}")
            break
        row, errs = _row_for(de, fields[key], entry["value"])
        rows.append(row)
        errors.extend(errs)

    trailing = parsed.get("trailing")
    if trailing:
        trailing_out: str | None = (
            trailing.decode("latin-1") if ascii_mode else trailing.hex().upper()
        )
    else:
        trailing_out = None

    return {
        "mti": parsed["mti"],
        "mti_info": decode_mti(parsed["mti"]),
        "bitmap": {"primary": parsed["bitmap"]["primary"],
                   "secondary": parsed["bitmap"]["secondary"],
                   "present": parsed["de_list"]},
        "fields": rows,
        "errors": errors,
        "trailing": trailing_out,
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
        try:
            opts = resolve_encoding(encoding)
            wire = _iso.pack(mti, values, fields, opts)
            message = wire.decode("latin-1") if _iso.is_ascii(opts) else wire.hex().upper()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return {"message": message, "errors": errors}


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


__all__ = [
    "CURRENCY_CODES",
    "DEFAULT_FIELDS",
    "EMV_TAGS",
    "POS_ENTRY_MODES",
    "PROCESSING_CODES",
    "RESPONSE_CODES",
    "analyze_message",
    "build_message",
    "build_tlv",
    "decode_mti",
    "diff_messages",
    "interpret_de",
    "iso_pack",
    "iso_pack_bytes",
    "parse_tlv",
    "resolve_encoding",
    "validate_field",
    "validate_values",
]
