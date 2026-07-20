"""Message format registry models — versioned ISO8583 / ISO20022 specs.

A *format* is a named, versioned definition you can maintain per integration or
dialect. ISO8583 formats carry a field table (DE -> {name, len_type, length,
type}); ISO20022 formats carry the message type / namespace / template. Pack &
parse steps use a chosen format (snapshot its definition into the step), so the
same flow can target different integrations just by switching the format.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .iso_catalog import (
    ISO8583_1987_FIELDS_SRC,
    ISO8583_1993_FIELDS_SRC,
    ISO8583_VISA_FIELDS_SRC,
)

Protocol = Literal["iso8583", "iso20022"]


class MessageFormatDraft(BaseModel):
    protocol: Protocol
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="1", max_length=40)
    #: Optional organisational group within a protocol (e.g. an acquirer or
    #: scheme name). Lets users file custom formats under their own headings.
    group: str = Field(default="", max_length=80)
    description: str = ""
    #: iso8583 -> {"fields": {de: {name, len_type, length, type}},
    #:             "presence"?: {mti|"default": {"mandatory": [de...],
    #:                                            "conditional": [de...]}}}
    #:   The optional ``presence`` matrix is the dialect's conformance layer:
    #:   which DEs are required for which MTI. A bound simulator uses it to
    #:   validate inbound messages (see the worker responder ``validate`` block).
    #: iso20022 -> {"message_type", "namespace", "root", "template"?}
    definition: dict[str, Any] = Field(default_factory=dict)


class MessageFormat(MessageFormatDraft):
    id: str
    builtin: bool = False


def _exec_fields(src: str) -> dict:
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - trusted constant
    return ns["FIELDS"]


#: Per-MTI presence matrix for the standard ISO 8583 request flows. Deliberately
#: conservative (the widely-required core data elements) so a bound simulator
#: flags genuinely malformed requests without false-positives on optional DEs.
#: Clone a builtin and tighten this for an integration-specific dialect.
_ISO8583_PRESENCE: dict = {
    "0100": {"mandatory": ["2", "3", "4", "7", "11", "41", "49"]},  # auth request
    "0200": {"mandatory": ["2", "3", "4", "7", "11", "41", "49"]},  # financial request
    "0400": {"mandatory": ["7", "11", "37"]},                        # reversal request
    "0800": {"mandatory": ["7", "11", "70"]},                        # network mgmt
    "default": {"mandatory": ["11"]},
}

#: Presence matrix for the VISA Base I dialect (1987 MTIs), matching the flows
#: the bundled VisaSimulator answers: auth (0100/0200), reversal & advice
#: (0400/0420), network management (0800). Conservative core DEs only.
_ISO8583_VISA_PRESENCE: dict = {
    "0100": {"mandatory": ["2", "3", "4", "7", "11", "41", "49"]},  # auth request
    "0200": {"mandatory": ["2", "3", "4", "7", "11", "41", "49"]},  # financial request
    "0400": {"mandatory": ["7", "11", "37", "90"]},                  # reversal advice
    "0420": {"mandatory": ["7", "11", "37"]},                        # advice
    "0800": {"mandatory": ["7", "11", "70"]},                        # network management
    "default": {"mandatory": ["11"]},
}


#: Request MTIs a dialect offers. The 1987 generation numbers messages 0xxx; the
#: 1993 generation bumps the version digit to 1xxx — so switching
#: dialect version must also switch the MTI menu. Stored on each format's
#: ``definition.mti`` so the step editor repopulates the MTI dropdown on change.
_ISO8583_MTIS_1987 = ["0100", "0200", "0220", "0400", "0420", "0500", "0800"]
_ISO8583_MTIS_VISA = ["0100", "0200", "0220", "0400", "0420", "0800"]
_ISO8583_MTIS_1993 = ["1100", "1200", "1220", "1420", "1500", "1804"]


#: Read-only seeds shipped with the product (cloneable into custom versions).
BUILTIN_FORMATS: list[MessageFormat] = [
    MessageFormat(
        id="iso8583-1987",
        protocol="iso8583",
        name="ISO8583 1987 (ASCII)",
        version="1987",
        group="Standard",
        description="Standard ISO8583:1987 ASCII field table. Clone it to build "
        "an integration-specific version.",
        definition={
            "encoding": "ascii",
            "fields": _exec_fields(ISO8583_1987_FIELDS_SRC),
            "presence": _ISO8583_PRESENCE,
            "mti": _ISO8583_MTIS_1987,
        },
        builtin=True,
    ),
    MessageFormat(
        id="iso8583-1993",
        protocol="iso8583",
        name="ISO8583 1993 (ASCII)",
        version="1993",
        group="Standard",
        description="ISO8583:1993 ASCII field table — 3-digit action code (DE 39), "
        "12-char POS data code (DE 22) and 1993 additions. Clone it to build an "
        "integration-specific version.",
        definition={
            "encoding": "ascii",
            "fields": _exec_fields(ISO8583_1993_FIELDS_SRC),
            "presence": _ISO8583_PRESENCE,
            "mti": _ISO8583_MTIS_1993,
        },
        builtin=True,
    ),
    MessageFormat(
        id="visa-base1",
        protocol="iso8583",
        name="VISA Base I (ISO8583:1987)",
        version="base1",
        group="Schemes",
        description="DE table for the bundled VISA Base I-style scheme simulator: "
        "the ISO 8583:1987 core plus the VISA-relevant private fields its flows "
        "touch (DE 43/44/48/54/60/62/63/90/95). Functional public layout — DE "
        "62/63 are opaque echo fields. Clone it to pin an integration-specific set.",
        definition={
            "encoding": "ascii",
            "fields": _exec_fields(ISO8583_VISA_FIELDS_SRC),
            "presence": _ISO8583_VISA_PRESENCE,
            "mti": _ISO8583_MTIS_VISA,
        },
        builtin=True,
    ),
    MessageFormat(
        id="iso20022-pacs008",
        protocol="iso20022",
        name="ISO20022 pacs.008 (FIToFICstmrCdtTrf)",
        version="001.08",
        group="Standard",
        description="FI to FI Customer Credit Transfer. Clone to pin a different "
        "version or message type.",
        definition={
            "message_type": "pacs.008.001.08",
            "namespace": "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
            "root": "Document",
        },
        builtin=True,
    ),
]
