"""Dialect validation for decoded ISO 8583 messages (ADR-0011).

Works on **logical** values (what :func:`payprobe_common.iso8583.unpack` returns
and what a scenario author types): digits for ``n``, text for the text classes,
hex characters for ``b``. Lengths in a field spec are in those same units, so
this module never needs to know the wire profile; a BCD-packed PAN is still 16
digits here.

Two entry points, both driven by the bound field table:

* :func:`validate_field` — one value against one spec; returns an error string
  (the Inspector's per-row message) or ``None``.
* :func:`validate_message` — a whole decoded message against the table plus an
  optional per-MTI presence matrix; returns the responder's violation list.
"""

from __future__ import annotations

VARIABLE_LEN_TYPES = ("llvar", "lllvar", "llllvar", "lllllvar")

#: ISO 8583 data-element classes and what they admit.
_TEXT_CLASSES = ("ans", "anp", "p", "s")


def charset_error(value: str, type_code: str) -> str | None:
    """Why ``value`` violates its data-element class, or ``None`` if it fits.

    ``n`` numeric, ``a`` alphabetic, ``an`` alphanumeric, ``ans`` / ``anp`` /
    ``p`` / ``s`` printable ASCII, ``z`` track 2/3 data (digits plus the ``=`` /
    ``D`` separators), ``b`` binary as whole hex bytes. Unknown classes pass.
    """
    typ = (type_code or "").lower()
    if not value:
        return None
    if typ == "n":
        return None if value.isdigit() else "must be numeric"
    if typ == "a":
        return None if value.isalpha() else "must be alphabetic"
    if typ == "an":
        return None if value.isalnum() else "must be alphanumeric"
    if typ in _TEXT_CLASSES:
        if any(not (0x20 <= ord(c) <= 0x7E) for c in value):
            return "must be printable ASCII (ans)"
        return None
    if typ == "z":
        if any(c not in "0123456789=Dd" for c in value):
            return "must be track 2/3 data (digits, '=', 'D')"
        return None
    if typ == "b":
        try:
            int(value, 16)
        except ValueError:
            return "must be hexadecimal"
        if len(value) % 2:
            return "binary field must have an even number of hex digits"
        return None
    return None


def length_error(value: str, spec: dict) -> str | None:
    """Fixed fields must match ``length`` exactly, variable ones must not exceed it."""
    lt = spec.get("len_type", "fixed")
    length = int(spec.get("length", 0) or 0)
    if lt == "fixed" and length and len(value) != length:
        return f"expected {length} chars, got {len(value)}"
    if lt in VARIABLE_LEN_TYPES and length and len(value) > length:
        return f"exceeds max length {length} (got {len(value)})"
    return None


def validate_field(value: str, spec: dict) -> str | None:
    """Return an error string if ``value`` violates the DE spec, else ``None``.
    Length first (fixed = exact, variable = max), then the class in ``type``."""
    value = str(value)
    err = length_error(value, spec)
    if err:
        return err
    return charset_error(value, spec.get("type") or "")


def validate_message(
    mti: str,
    fields: dict[str, dict],
    de_values: dict,
    de_list: list[int] | None = None,
    presence: dict | None = None,
) -> list[str]:
    """Validate a decoded message against a dialect (DE table + presence rules).

    Returns human-readable violations (empty == conformant). Three checks:

    * **unknown DE** — a bit set in the bitmap (``de_list``) with no entry in
      ``fields``; the dialect does not define it.
    * **length / charset** — each value must fit its field's ``length`` (exact
      for ``fixed``, max for the variable types) and its ``type`` class.
    * **presence** — every DE marked ``mandatory`` for this MTI (exact MTI, then
      ``"default"``) must be present and non-empty.
    """
    issues: list[str] = []
    de_values = {str(k): ("" if v is None else str(v)) for k, v in de_values.items()}
    bits = [int(d) for d in (de_list if de_list is not None else de_values.keys())]

    for de in sorted(bits):
        if str(de) not in fields:
            issues.append(f"DE {de}: not defined in format")

    for de, val in de_values.items():
        spec = fields.get(de)
        if not spec:
            continue
        lt = spec.get("len_type", "fixed")
        ln = int(spec.get("length", 0) or 0)
        if lt == "fixed" and ln and len(val) != ln:
            issues.append(f"DE {de}: length {len(val)} != fixed {ln}")
        elif lt in VARIABLE_LEN_TYPES and ln and len(val) > ln:
            issues.append(f"DE {de}: length {len(val)} exceeds max {ln}")
        typ = spec.get("type")
        if typ and val and charset_error(val, typ):
            issues.append(f"DE {de}: value not valid for type '{typ}'")

    if presence:
        rule = presence.get(mti) or presence.get("default") or {}
        present = {str(d) for d in bits} | set(de_values)
        for de in rule.get("mandatory", []):
            if str(de) not in present or not de_values.get(str(de)):
                issues.append(f"DE {de}: mandatory for MTI {mti} but missing")

    return issues


__all__ = [
    "VARIABLE_LEN_TYPES",
    "charset_error",
    "length_error",
    "validate_field",
    "validate_message",
]
