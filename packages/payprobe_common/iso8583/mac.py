"""ISO 8583 message authentication on DE 64 / DE 128 (ADR-0013).

The contract only: which field carries the MAC, which bytes it covers, how it
is written and compared. The algorithms themselves (ISO 9797-1 retail MAC,
AES-CMAC) live in the worker's ``crypto_tools`` and are passed in as a callable
``algorithm(key_hex, data) -> bytes`` returning the full MAC, so this package
stays dependency-free and an HSM-backed algorithm can plug in later.

Coverage is every wire byte before the MAC field. DE 64 is always the last
field of a primary-bitmap message and DE 128 the last field overall, so under
any wire profile the message is ``coverage || mac_field``. The MAC field is a
``b`` field of 16 hex characters (8 bytes); a truncated MAC (``length`` 4)
occupies the left bytes, the rest are zero.

Spec (a Message Format ``definition.mac`` block, or the same keys on a
connection / simulator config, which wins)::

    {"field": 64, "algorithm": "retail_mac", "key": "<hex or ${key.NAME}>",
     "length": 8, "on_failure": "warn"}

``length`` is 8 or 4 for both algorithms: the field is 64 bits, so an AES-CMAC is
the truncated CMAC-64 (RFC 4493 §2.4 permits truncation), never the full tag.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .codec import pack, unpack

#: algorithm -> allowed MAC lengths on the wire (bytes); first is the default
#: DE 64 / 128 are 64-bit fields, so every algorithm is truncated to 8 (or 4) bytes
ALGORITHMS: dict[str, tuple[int, ...]] = {"retail_mac": (8, 4), "aes_cmac": (8, 4)}
FIELDS = (64, 128)
ON_FAILURE = ("warn", "reject")
#: the MAC field as the shared dictionaries define it (16 hex chars, class b)
MAC_FIELD_HEX_CHARS = 16

MacAlgorithm = Callable[[str, bytes], bytes]


class MacSpecError(ValueError):
    """The ``mac`` block is not usable (unknown field / algorithm / length …)."""


def resolve_mac_spec(spec: Any) -> dict | None:
    """Normalise a ``mac`` block; ``None`` when absent. Refuses nonsense early so
    a typo never silently means "no MAC"."""
    if spec in (None, {}, False):
        return None
    if not isinstance(spec, dict):
        raise MacSpecError(f"mac must be a block, got {spec!r}")
    try:
        field = int(spec.get("field", 64))
    except (TypeError, ValueError):
        raise MacSpecError(f"mac.field must be 64 or 128, got {spec.get('field')!r}") from None
    if field not in FIELDS:
        raise MacSpecError(f"mac.field must be 64 or 128, got {field}")
    algorithm = str(spec.get("algorithm", "retail_mac")).lower()
    if algorithm not in ALGORITHMS:
        raise MacSpecError(
            f"mac.algorithm must be one of {', '.join(ALGORITHMS)}, got {algorithm!r}"
        )
    length = int(spec.get("length", ALGORITHMS[algorithm][0]))
    if length not in ALGORITHMS[algorithm]:
        raise MacSpecError(
            f"mac.length for {algorithm} must be one of {ALGORITHMS[algorithm]}, got {length}"
        )
    on_failure = str(spec.get("on_failure", "warn")).lower()
    if on_failure not in ON_FAILURE:
        raise MacSpecError(f"mac.on_failure must be warn or reject, got {on_failure!r}")
    key = spec.get("key")
    if not key or not isinstance(key, str):
        raise MacSpecError("mac.key is required (hex material or a ${key.NAME} reference)")
    return {
        "field": field,
        "algorithm": algorithm,
        "key": key,
        "length": length,
        "on_failure": on_failure,
    }


def key_is_unresolved(spec: dict) -> bool:
    """True when the key is still a ``${key.NAME}`` reference (the orchestrator
    resolves those before a simulator starts; a worker seeing one must refuse)."""
    return str(spec.get("key", "")).startswith("${")


def _mac_hex(spec: dict, algorithm: MacAlgorithm, coverage: bytes) -> str:
    full = algorithm(spec["key"], coverage)
    mac = full[: spec["length"]].ljust(MAC_FIELD_HEX_CHARS // 2, b"\x00")
    return mac.hex().upper()


def _mac_field_wire_length(fields: dict[str, dict], spec: dict, encoding: Any) -> int:
    """How many wire bytes the MAC field occupies under ``encoding``: pack a
    message that is only the MAC field and subtract the header."""
    key = str(spec["field"])
    if key not in fields:
        raise MacSpecError(f"mac.field {key} is not defined in the field table")
    only_mac = pack("0000", {key: "0" * MAC_FIELD_HEX_CHARS}, fields, encoding)
    header = pack("0000", {}, fields, encoding)
    # a DE > 64 forces a secondary bitmap onto the header as well
    if spec["field"] > 64:
        header = pack(
            "0000", {"65": ""}, {**fields, "65": {"len_type": "fixed", "length": 0}}, encoding
        )
    return len(only_mac) - len(header)


def pack_with_mac(
    mti: str,
    values: dict,
    fields: dict[str, dict],
    encoding: Any,
    spec: dict,
    algorithm: MacAlgorithm,
) -> bytes:
    """Pack ``values`` and stamp the MAC into ``spec['field']``.

    The MAC field must be the highest DE present (it is last on the wire); a
    higher DE in ``values`` is refused because the coverage would be ambiguous.
    """
    key = str(spec["field"])
    values = {str(k): v for k, v in values.items()}
    higher = [d for d in values if int(d) > spec["field"]]
    if higher:
        raise MacSpecError(
            f"mac.field {key} must be the last field on the wire; DE {', '.join(higher)} come after it"
        )
    values[key] = "0" * MAC_FIELD_HEX_CHARS
    placeholder_wire = pack(mti, values, fields, encoding)
    mac_len = _mac_field_wire_length(fields, spec, encoding)
    coverage = placeholder_wire[:-mac_len]
    values[key] = _mac_hex(spec, algorithm, coverage)
    return pack(mti, values, fields, encoding)


def verify_mac(
    wire: bytes,
    parsed: dict,
    fields: dict[str, dict],
    encoding: Any,
    spec: dict,
    algorithm: MacAlgorithm,
) -> dict:
    """Check the MAC a decoded message carries. Returns
    ``{"present": bool, "ok": bool | None, "field": int, "error": str | None}``;
    ``ok`` is ``None`` when no MAC field is present (absence is reported, the
    caller decides whether that is a violation)."""
    key = str(spec["field"])
    out: dict[str, Any] = {"present": False, "ok": None, "field": spec["field"], "error": None}
    entry = (parsed.get("fields") or {}).get(key)
    if not entry or "value" not in entry or entry.get("error"):
        out["error"] = f"DE {key}: no MAC present"
        return out
    out["present"] = True
    de_list = parsed.get("de_list") or []
    if de_list and max(de_list) != spec["field"]:
        out["ok"] = False
        out["error"] = f"DE {key}: MAC field is not the last field (DE {max(de_list)} follows)"
        return out
    mac_len = _mac_field_wire_length(fields, spec, encoding)
    expected = _mac_hex(spec, algorithm, wire[:-mac_len])
    actual = str(entry["value"]).upper()
    n = spec["length"] * 2
    ok = actual[:n] == expected[:n]
    out["ok"] = ok
    if not ok:
        out["error"] = f"DE {key}: MAC mismatch"
    return out


def unpack_and_verify(
    wire: bytes,
    fields: dict[str, dict],
    encoding: Any,
    spec: dict | None,
    algorithm: MacAlgorithm | None,
    **unpack_kwargs: Any,
) -> dict:
    """``unpack`` plus, when a spec is given, a ``mac`` verdict on the result."""
    parsed = unpack(wire, fields, encoding, **unpack_kwargs)
    if spec and algorithm:
        parsed["mac"] = verify_mac(wire, parsed, fields, encoding, spec, algorithm)
    return parsed


__all__ = [
    "ALGORITHMS",
    "FIELDS",
    "MAC_FIELD_HEX_CHARS",
    "ON_FAILURE",
    "MacAlgorithm",
    "MacSpecError",
    "key_is_unresolved",
    "pack_with_mac",
    "resolve_mac_spec",
    "unpack_and_verify",
    "verify_mac",
]
