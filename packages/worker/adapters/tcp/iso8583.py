"""ISO 8583 codec entry point for the worker: a re-export of the one shared codec.

ADR-0011 moved the codec, the field dictionaries and the dialect validator into
``payprobe_common.iso8583`` so the worker wire path, the scenario-service
Inspector and the catalog code-step defaults can never disagree byte for byte.
This module keeps the names the adapters and tests import:

* :func:`pack` / :func:`unpack` — the bytes codec under a wire ``encoding``
  profile (``"ascii"`` default, ``"binary"``, or an axis dict; see the shared
  package docstring). Use these on the wire.
* :func:`iso_pack` / :func:`iso_unpack` — the legacy ``str`` helpers, ASCII
  profile only. ``iso_unpack`` keeps its historical tolerance of whitespace
  anywhere in the input, which is why it must not be used on wire bytes whose
  text fields may contain spaces.
* :func:`iso_validate` — dialect validation of a decoded message.
* ``DEFAULT_FIELDS`` — the shared ISO 8583:1987 table (``ISO8583_1987``).

The worker still must not import from the scenario-service package; the shared
package is a leaf both services depend on.
"""

from __future__ import annotations

from typing import Any

from payprobe_common.iso8583 import (
    DEFAULT_FIELDS,
    ISO8583_1987,
    ISO8583_1993,
    VISA_BASE_I,
    Iso8583DecodeError,
    mti_encoding,
    pack,
    resolve_encoding,
    unpack,
    validate_message,
    wire_encoding_from_config,
)

iso_validate = validate_message


def iso_unpack(msg: str, fields: dict[str, dict]) -> dict[str, Any]:
    """Parse an ASCII ISO 8583 message (``str``) into ``{mti, de_list, fields, …}``.

    Whitespace anywhere in ``msg`` is dropped first (historical convenience for
    hand-pasted messages). The result is :func:`unpack`'s: an unknown DE stops
    decoding and is reported through ``error`` / ``truncated``; a structurally
    broken message raises :class:`Iso8583DecodeError` (a ``ValueError``).
    """
    text = "".join(msg.split())
    return unpack(text.encode("latin-1"), fields, "ascii")


def iso_pack(mti: str, values: dict, fields: dict[str, dict]) -> str:
    """Build an ASCII ISO 8583 message (``str``) from MTI + ``{DE: value}``."""
    return pack(mti, values, fields, "ascii").decode("latin-1")


__all__ = [
    "DEFAULT_FIELDS",
    "ISO8583_1987",
    "ISO8583_1993",
    "VISA_BASE_I",
    "Iso8583DecodeError",
    "iso_pack",
    "iso_unpack",
    "iso_validate",
    "mti_encoding",
    "pack",
    "resolve_encoding",
    "unpack",
    "validate_message",
    "wire_encoding_from_config",
]
