"""One ISO 8583 codec, one field dictionary (ADR-0011).

Shared by the worker (wire), scenario-service (Message Format builtins, the
Inspector's analyzer, the catalog code-step defaults) and the NATS codec. Before
this package each of them carried its own copy and the copies had drifted.

    pack(mti, values, fields, encoding=None) -> bytes
    unpack(data, fields, encoding=None, *, strict=True) -> dict
    validate_message(mti, fields, de_values, de_list=None, presence=None) -> list[str]
    validate_field(value, spec) -> str | None
    resolve_encoding(encoding) -> dict            # profile name or axis dict -> options
    wire_encoding_from_config(config) -> encoding # folds the legacy framing.encoding key

Tables: ``ISO8583_1987`` (also ``DEFAULT_FIELDS``), ``ISO8583_1993``,
``VISA_BASE_I``. EMV TLV helpers: ``parse_tlv`` / ``build_tlv``.
"""

from __future__ import annotations

from typing import Any

from .codec import (
    ASCII_OPTS,
    AXES,
    AXIS_VALUES,
    BINARY_OPTS,
    EBCDIC,
    FIELD_OVERRIDE_KEYS,
    LEN_PREFIX,
    PROFILES,
    Iso8583DecodeError,
    bcd_decode,
    bcd_encode,
    field_options,
    is_ascii,
    pack,
    resolve_encoding,
    unpack,
)
from .fields import BUILTIN_TABLES, ISO8583_1987, ISO8583_1993, VISA_BASE_I
from .tlv import EMV_TAGS, build_tlv, parse_tlv
from .validate import charset_error, length_error, validate_field, validate_message

#: The table used when nothing binds a dialect (worker codec, Inspector).
DEFAULT_FIELDS = ISO8583_1987

#: Legacy ``framing.encoding`` values (Python text-codec names) and the profile
#: each folds to. Anything else is refused: the key was a text codec, it cannot
#: express a bitmap or length axis, and a typo must never bind as ASCII.
LEGACY_TEXT_ENCODINGS: dict[str, Any] = {
    "ascii": "ascii",
    "us-ascii": "ascii",
    "utf-8": "ascii",
    "utf8": "ascii",
    "latin-1": "ascii",
    "latin1": "ascii",
    "iso-8859-1": "ascii",
    "cp037": {"text": "ebcdic"},
    "ebcdic": {"text": "ebcdic"},
    "ebcdic-cp-us": {"text": "ebcdic"},
}


def wire_encoding_from_config(config: dict | None, *, protocol: str = "iso8583") -> Any:
    """The ISO 8583 wire encoding a simulator / connection / codec config asks for.

    Precedence (ADR-0011): a top-level ``encoding`` (profile name or axis dict,
    which is where a bound Message Format's ``definition.encoding`` is injected)
    wins; otherwise the legacy ``framing.encoding`` text codec is folded through
    :data:`LEGACY_TEXT_ENCODINGS`. When both are present and resolve to different
    profiles the config is refused with a ``ValueError`` naming both keys, never
    silently picked. Non-ISO protocols (``header_echo``) keep ``framing.encoding``
    as a real text codec and are not folded.
    """
    config = config or {}
    explicit = config.get("encoding")
    legacy = (config.get("framing") or {}).get("encoding")
    if protocol != "iso8583":
        return explicit if explicit is not None else "ascii"
    folded: Any = None
    if legacy is not None:
        key = str(legacy).lower()
        if key not in LEGACY_TEXT_ENCODINGS:
            raise ValueError(
                f"framing.encoding {legacy!r} is not a known ISO 8583 text encoding; "
                "use a top-level 'encoding' profile (ascii | binary | axis dict) instead"
            )
        folded = LEGACY_TEXT_ENCODINGS[key]
    if explicit is None:
        return folded if folded is not None else "ascii"
    resolved = resolve_encoding(explicit)  # validates early
    if folded is not None and resolve_encoding(folded) != resolved:
        raise ValueError(
            f"'encoding' ({explicit!r}) and legacy 'framing.encoding' ({legacy!r}) "
            "disagree; keep one (the top-level 'encoding' profile is the one that counts)"
        )
    return explicit


__all__ = [
    "ASCII_OPTS",
    "AXES",
    "AXIS_VALUES",
    "BINARY_OPTS",
    "BUILTIN_TABLES",
    "DEFAULT_FIELDS",
    "EBCDIC",
    "EMV_TAGS",
    "FIELD_OVERRIDE_KEYS",
    "ISO8583_1987",
    "ISO8583_1993",
    "LEGACY_TEXT_ENCODINGS",
    "LEN_PREFIX",
    "PROFILES",
    "VISA_BASE_I",
    "Iso8583DecodeError",
    "bcd_decode",
    "bcd_encode",
    "build_tlv",
    "charset_error",
    "field_options",
    "is_ascii",
    "length_error",
    "pack",
    "parse_tlv",
    "resolve_encoding",
    "unpack",
    "validate_field",
    "validate_message",
    "wire_encoding_from_config",
]
