"""Self-contained ASCII ISO 8583 codec for the worker TCP adapter.

Mirrors the configuration-driven ASCII codec used by the scenario-service
``iso_catalog`` step (same bitmap/length-type rules) so packing here and
parsing in the editor stay consistent. Kept self-contained on purpose: the
worker must not import from the scenario-service package.

A ``FIELDS`` table maps each data element (DE) number, as a string key, to a
spec::

    {"name": "...", "len_type": "fixed" | "llvar" | "lllvar", "length": int}

Values are ASCII text. Variable-length fields are prefixed with a 2-digit
(llvar) or 3-digit (lllvar) ASCII length.
"""

from __future__ import annotations

from typing import Any


def _bits_from_hex(h: str) -> set[int]:
    n = int(h, 16)
    w = len(h) * 4
    return {i + 1 for i in range(w) if n & (1 << (w - 1 - i))}


def _bitmap(des: set[int], width: int) -> str:
    n = 0
    for d in des:
        n |= 1 << (width - d)
    return format(n, "0%dX" % (width // 4))


def iso_unpack(msg: str, fields: dict[str, dict]) -> dict[str, Any]:
    """Parse an ASCII ISO 8583 message into ``{mti, de_list, fields}``."""
    msg = "".join(msg.split())
    pos = 0
    mti = msg[0:4]
    pos = 4
    present = _bits_from_hex(msg[pos : pos + 16])
    pos += 16
    if 1 in present:
        present |= {b + 64 for b in _bits_from_hex(msg[pos : pos + 16])}
        pos += 16
        present.discard(1)
    parsed: dict[str, dict] = {}
    for de in sorted(present):
        sp = fields.get(str(de))
        if not sp:
            parsed[str(de)] = {"name": "(unknown)", "value": "", "error": "DE not in spec"}
            break
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            ln = int(sp.get("length", 0))
            v = msg[pos : pos + ln]
            pos += ln
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            ln = int(msg[pos : pos + pw])
            pos += pw
            v = msg[pos : pos + ln]
            pos += ln
        parsed[str(de)] = {"name": sp.get("name", ""), "value": v}
    return {"mti": mti, "de_list": sorted(present), "fields": parsed}


def _charset_ok(value: str, type_code: str) -> bool:
    """Does ``value`` satisfy an ISO 8583 field class? ``b`` (binary) is carried
    as opaque text in this ASCII codec, so it is never charset-failed."""
    t = (type_code or "").lower()
    if t == "n":
        return value.isdigit()
    if t == "an":
        return value.isalnum()
    if t == "ans":
        return value.isprintable()
    if t == "z":  # track-2: digits plus the field/end separators
        return all(c in "0123456789=D" for c in value.upper())
    return True  # "b" / unknown -> don't fail


def iso_validate(
    mti: str,
    fields: dict[str, dict],
    de_values: dict,
    de_list: list[int] | None = None,
    presence: dict | None = None,
) -> list[str]:
    """Validate a decoded message against a dialect (DE table + presence rules).

    Returns a list of human-readable violations (empty == conformant). Three
    checks, all driven by the bound format:

    * **unknown DE** — a bit set in the bitmap (``de_list``) with no entry in
      ``fields``; the dialect does not define it.
    * **length / charset** — each decoded value must fit its field's ``length``
      (exact for ``fixed``, max for ``llvar``/``lllvar``) and its ``type`` class
      (``n``/``an``/``ans``/``z``) when the spec declares one.
    * **presence** — every DE marked ``mandatory`` for this MTI (looked up by
      exact MTI then ``"default"``) must be present and non-empty.
    """
    issues: list[str] = []
    de_values = {str(k): ("" if v is None else str(v)) for k, v in de_values.items()}
    bits = [int(d) for d in (de_list if de_list is not None else de_values.keys())]

    for de in sorted(bits):
        if str(de) not in fields:
            issues.append(f"DE {de}: not defined in format")

    for de, val in de_values.items():
        sp = fields.get(de)
        if not sp:
            continue
        lt = sp.get("len_type", "fixed")
        ln = int(sp.get("length", 0) or 0)
        if lt == "fixed" and ln and len(val) != ln:
            issues.append(f"DE {de}: length {len(val)} != fixed {ln}")
        elif lt in ("llvar", "lllvar", "llllvar", "lllllvar") and ln and len(val) > ln:
            issues.append(f"DE {de}: length {len(val)} exceeds max {ln}")
        if (t := sp.get("type")) and val and not _charset_ok(val, t):
            issues.append(f"DE {de}: value not valid for type '{t}'")

    if presence:
        rule = presence.get(mti) or presence.get("default") or {}
        present = {str(d) for d in bits} | set(de_values)
        for de in rule.get("mandatory", []):
            if str(de) not in present or not de_values.get(str(de)):
                issues.append(f"DE {de}: mandatory for MTI {mti} but missing")

    return issues


def iso_pack(mti: str, values: dict, fields: dict[str, dict]) -> str:
    """Build an ASCII ISO 8583 message from MTI + ``{DE: value}``."""
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
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            out += str(len(v)).zfill(pw) + v
    return out


#: Standard ISO 8583:1987-style ASCII field table. Used as the default DE table
#: when an environment / step does not supply its own ``fields``.
DEFAULT_FIELDS: dict[str, dict] = {
    "2": {"name": "Primary Account Number", "len_type": "llvar", "length": 19},
    "3": {"name": "Processing Code", "len_type": "fixed", "length": 6},
    "4": {"name": "Amount, Transaction", "len_type": "fixed", "length": 12},
    "7": {"name": "Transmission Date/Time", "len_type": "fixed", "length": 10},
    "11": {"name": "STAN", "len_type": "fixed", "length": 6},
    "12": {"name": "Time, Local", "len_type": "fixed", "length": 6},
    "13": {"name": "Date, Local", "len_type": "fixed", "length": 4},
    "14": {"name": "Date, Expiration", "len_type": "fixed", "length": 4},
    "18": {"name": "Merchant Type", "len_type": "fixed", "length": 4},
    "22": {"name": "POS Entry Mode", "len_type": "fixed", "length": 3},
    "25": {"name": "POS Condition Code", "len_type": "fixed", "length": 2},
    "32": {"name": "Acquiring Institution ID", "len_type": "llvar", "length": 11},
    "35": {"name": "Track 2 Data", "len_type": "llvar", "length": 37},
    "37": {"name": "Retrieval Reference Num", "len_type": "fixed", "length": 12},
    "38": {"name": "Authorization ID Resp", "len_type": "fixed", "length": 6},
    "39": {"name": "Response Code", "len_type": "fixed", "length": 2},
    "41": {"name": "Card Acceptor Terminal", "len_type": "fixed", "length": 8},
    "42": {"name": "Card Acceptor ID Code", "len_type": "fixed", "length": 15},
    "49": {"name": "Currency Code, Txn", "len_type": "fixed", "length": 3},
    "52": {"name": "PIN Data", "len_type": "fixed", "length": 16},
    "55": {"name": "ICC Data (EMV)", "len_type": "lllvar", "length": 999},
    "70": {"name": "Network Management Code", "len_type": "fixed", "length": 3},
}
