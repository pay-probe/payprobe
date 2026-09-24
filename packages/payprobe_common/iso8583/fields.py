"""ISO 8583 field dictionaries as data (ADR-0011).

One table per dialect, shared by the worker wire codec, the scenario-service
Message Format builtins, the Inspector and the catalog code-step defaults. Before
ADR-0011 each of those carried its own hand-maintained copy and they had drifted
(22 vs 29 vs 50 data elements, ``type`` missing on the worker copy). Edit here only.

Shape (the Message Format registry stores the same thing)::

    {"<de>": {"name": str, "len_type": "fixed|llvar|lllvar|llllvar|lllllvar",
              "length": int, "type": "n|a|an|ans|z|b",
              "encoding"?: {per-field override, see codec.field_options}}}

``length`` is in logical units whatever the wire encoding: digits for ``n``,
characters for text classes, **hex characters** for ``b`` (so DE 52 = 16 means
8 wire bytes under a raw-binary profile).
"""

from __future__ import annotations

#: ISO 8583:1987 data-element directory (ASCII teaching form; the wire
#: encoding is a property of the bound format, not of this table). Used as
#: ``DEFAULT_FIELDS`` by the worker codec and the Inspector, and as the
#: ``iso8583-1987`` builtin Message Format.
ISO8583_1987: dict[str, dict] = {
    "2": {"name": "Primary Account Number (PAN)", "len_type": "llvar", "length": 19, "type": "n"},
    "3": {"name": "Processing Code", "len_type": "fixed", "length": 6, "type": "n"},
    "4": {"name": "Amount, Transaction", "len_type": "fixed", "length": 12, "type": "n"},
    "5": {"name": "Amount, Settlement", "len_type": "fixed", "length": 12, "type": "n"},
    "6": {"name": "Amount, Cardholder Billing", "len_type": "fixed", "length": 12, "type": "n"},
    "7": {
        "name": "Transmission Date & Time (MMDDhhmmss)",
        "len_type": "fixed",
        "length": 10,
        "type": "n",
    },
    "9": {"name": "Conversion Rate, Settlement", "len_type": "fixed", "length": 8, "type": "n"},
    "10": {
        "name": "Conversion Rate, Cardholder Billing",
        "len_type": "fixed",
        "length": 8,
        "type": "n",
    },
    "11": {
        "name": "System Trace Audit Number (STAN)",
        "len_type": "fixed",
        "length": 6,
        "type": "n",
    },
    "12": {
        "name": "Time, Local Transaction (hhmmss)",
        "len_type": "fixed",
        "length": 6,
        "type": "n",
    },
    "13": {"name": "Date, Local Transaction (MMDD)", "len_type": "fixed", "length": 4, "type": "n"},
    "14": {"name": "Date, Expiration (YYMM)", "len_type": "fixed", "length": 4, "type": "n"},
    "15": {"name": "Date, Settlement (MMDD)", "len_type": "fixed", "length": 4, "type": "n"},
    "18": {"name": "Merchant Type (MCC)", "len_type": "fixed", "length": 4, "type": "n"},
    "19": {
        "name": "Acquiring Institution Country Code",
        "len_type": "fixed",
        "length": 3,
        "type": "n",
    },
    "22": {"name": "POS Entry Mode", "len_type": "fixed", "length": 3, "type": "n"},
    "23": {"name": "Card Sequence Number", "len_type": "fixed", "length": 3, "type": "n"},
    "25": {"name": "POS Condition Code", "len_type": "fixed", "length": 2, "type": "n"},
    "32": {"name": "Acquiring Institution ID Code", "len_type": "llvar", "length": 11, "type": "n"},
    "33": {
        "name": "Forwarding Institution ID Code",
        "len_type": "llvar",
        "length": 11,
        "type": "n",
    },
    "35": {"name": "Track 2 Data", "len_type": "llvar", "length": 37, "type": "z"},
    "36": {"name": "Track 3 Data", "len_type": "lllvar", "length": 104, "type": "z"},
    "37": {"name": "Retrieval Reference Number", "len_type": "fixed", "length": 12, "type": "an"},
    "38": {"name": "Authorization ID Response", "len_type": "fixed", "length": 6, "type": "an"},
    "39": {"name": "Response Code", "len_type": "fixed", "length": 2, "type": "an"},
    "41": {"name": "Card Acceptor Terminal ID", "len_type": "fixed", "length": 8, "type": "ans"},
    "42": {"name": "Card Acceptor ID Code", "len_type": "fixed", "length": 15, "type": "ans"},
    "43": {"name": "Card Acceptor Name/Location", "len_type": "fixed", "length": 40, "type": "ans"},
    "44": {"name": "Additional Response Data", "len_type": "llvar", "length": 25, "type": "an"},
    "45": {"name": "Track 1 Data", "len_type": "llvar", "length": 76, "type": "an"},
    "48": {"name": "Additional Data — Private", "len_type": "lllvar", "length": 999, "type": "ans"},
    "49": {"name": "Currency Code, Transaction", "len_type": "fixed", "length": 3, "type": "n"},
    "50": {"name": "Currency Code, Settlement", "len_type": "fixed", "length": 3, "type": "n"},
    "51": {
        "name": "Currency Code, Cardholder Billing",
        "len_type": "fixed",
        "length": 3,
        "type": "n",
    },
    "52": {"name": "PIN Data", "len_type": "fixed", "length": 16, "type": "b"},
    "53": {
        "name": "Security Related Control Information",
        "len_type": "fixed",
        "length": 16,
        "type": "n",
    },
    "54": {"name": "Additional Amounts", "len_type": "lllvar", "length": 120, "type": "an"},
    "55": {"name": "ICC Data (EMV)", "len_type": "lllvar", "length": 999, "type": "b"},
    "60": {"name": "Reserved Private", "len_type": "lllvar", "length": 999, "type": "ans"},
    "61": {"name": "Reserved Private", "len_type": "lllvar", "length": 999, "type": "ans"},
    "62": {"name": "Reserved Private", "len_type": "lllvar", "length": 999, "type": "ans"},
    "63": {"name": "Reserved Private", "len_type": "lllvar", "length": 999, "type": "ans"},
    "64": {
        "name": "Message Authentication Code (MAC)",
        "len_type": "fixed",
        "length": 16,
        "type": "b",
    },
    "70": {
        "name": "Network Management Information Code",
        "len_type": "fixed",
        "length": 3,
        "type": "n",
    },
    "90": {"name": "Original Data Elements", "len_type": "fixed", "length": 42, "type": "n"},
    "95": {"name": "Replacement Amounts", "len_type": "fixed", "length": 42, "type": "an"},
    "100": {
        "name": "Receiving Institution ID Code",
        "len_type": "llvar",
        "length": 11,
        "type": "n",
    },
    "102": {"name": "Account Identification 1", "len_type": "llvar", "length": 28, "type": "ans"},
    "103": {"name": "Account Identification 2", "len_type": "llvar", "length": 28, "type": "ans"},
    "128": {
        "name": "Message Authentication Code (MAC)",
        "len_type": "fixed",
        "length": 16,
        "type": "b",
    },
}


#: ISO 8583:1993 dialect: DE 22 becomes the 12-char POS Data Code, DE 39
#: the 3-char Action Code, DE 12 the 12-digit local date/time; 1993 additions
#: (DE 56 message reason code, DE 95 replacement amounts) included.
ISO8583_1993: dict[str, dict] = {
    "2": {"name": "Primary Account Number", "len_type": "llvar", "length": 19, "type": "n"},
    "3": {"name": "Processing Code", "len_type": "fixed", "length": 6, "type": "n"},
    "4": {"name": "Amount, Transaction", "len_type": "fixed", "length": 12, "type": "n"},
    "7": {"name": "Transmission Date/Time", "len_type": "fixed", "length": 10, "type": "n"},
    "11": {"name": "STAN", "len_type": "fixed", "length": 6, "type": "n"},
    "12": {"name": "Date/Time, Local Txn", "len_type": "fixed", "length": 12, "type": "n"},
    "13": {"name": "Date, Effective", "len_type": "fixed", "length": 4, "type": "n"},
    "14": {"name": "Date, Expiration", "len_type": "fixed", "length": 4, "type": "n"},
    "18": {"name": "Merchant Type", "len_type": "fixed", "length": 4, "type": "n"},
    "22": {"name": "POS Data Code", "len_type": "fixed", "length": 12, "type": "an"},
    "25": {"name": "POS Condition Code", "len_type": "fixed", "length": 2, "type": "n"},
    "32": {"name": "Acquiring Institution ID", "len_type": "llvar", "length": 11, "type": "n"},
    "35": {"name": "Track 2 Data", "len_type": "llvar", "length": 37, "type": "z"},
    "37": {"name": "Retrieval Reference Num", "len_type": "fixed", "length": 12, "type": "an"},
    "38": {"name": "Approval Code", "len_type": "fixed", "length": 6, "type": "an"},
    "39": {"name": "Action Code", "len_type": "fixed", "length": 3, "type": "n"},
    "41": {"name": "Card Acceptor Terminal", "len_type": "fixed", "length": 8, "type": "ans"},
    "42": {"name": "Card Acceptor ID Code", "len_type": "fixed", "length": 15, "type": "ans"},
    "49": {"name": "Currency Code, Txn", "len_type": "fixed", "length": 3, "type": "n"},
    "52": {"name": "PIN Data", "len_type": "fixed", "length": 16, "type": "b"},
    "53": {"name": "Security Control Info", "len_type": "llvar", "length": 48, "type": "b"},
    "55": {"name": "ICC Data (EMV)", "len_type": "lllvar", "length": 999, "type": "b"},
    "56": {"name": "Message Reason Code", "len_type": "llvar", "length": 4, "type": "n"},
    "70": {"name": "Network Management Code", "len_type": "fixed", "length": 3, "type": "n"},
    "95": {"name": "Replacement Amounts", "len_type": "fixed", "length": 42, "type": "an"},
}


#: VISA Base I-style table the bundled ``VisaSimulator`` packs and parses
#: with: the 1987 core plus the VISA-relevant private fields its flows touch
#: (DE 43/44/48/54/60/62/63/90/95). Functional public layout; DE 62/63 are
#: opaque echo fields, not VISA's confidential sub-field structure.
VISA_BASE_I: dict[str, dict] = {
    "2": {"name": "Primary Account Number (PAN)", "len_type": "llvar", "length": 19, "type": "n"},
    "3": {"name": "Processing Code", "len_type": "fixed", "length": 6, "type": "n"},
    "4": {"name": "Amount, Transaction", "len_type": "fixed", "length": 12, "type": "n"},
    "7": {"name": "Transmission Date & Time", "len_type": "fixed", "length": 10, "type": "n"},
    "11": {"name": "System Trace Audit Number", "len_type": "fixed", "length": 6, "type": "n"},
    "12": {"name": "Time, Local Transaction", "len_type": "fixed", "length": 6, "type": "n"},
    "13": {"name": "Date, Local Transaction", "len_type": "fixed", "length": 4, "type": "n"},
    "14": {"name": "Date, Expiration", "len_type": "fixed", "length": 4, "type": "n"},
    "18": {"name": "Merchant Type (MCC)", "len_type": "fixed", "length": 4, "type": "n"},
    "22": {"name": "POS Entry Mode", "len_type": "fixed", "length": 3, "type": "n"},
    "25": {"name": "POS Condition Code", "len_type": "fixed", "length": 2, "type": "n"},
    "32": {"name": "Acquiring Institution ID", "len_type": "llvar", "length": 11, "type": "n"},
    "35": {"name": "Track 2 Data", "len_type": "llvar", "length": 37, "type": "z"},
    "37": {"name": "Retrieval Reference Number", "len_type": "fixed", "length": 12, "type": "an"},
    "38": {"name": "Authorization ID Response", "len_type": "fixed", "length": 6, "type": "an"},
    "39": {"name": "Response Code", "len_type": "fixed", "length": 2, "type": "an"},
    "41": {"name": "Card Acceptor Terminal ID", "len_type": "fixed", "length": 8, "type": "ans"},
    "42": {"name": "Card Acceptor ID Code", "len_type": "fixed", "length": 15, "type": "ans"},
    "43": {"name": "Card Acceptor Name/Location", "len_type": "fixed", "length": 40, "type": "ans"},
    "44": {"name": "Additional Response Data", "len_type": "llvar", "length": 25, "type": "ans"},
    "48": {"name": "Additional Data (Private)", "len_type": "lllvar", "length": 999, "type": "ans"},
    "49": {"name": "Currency Code, Transaction", "len_type": "fixed", "length": 3, "type": "n"},
    "52": {"name": "PIN Data", "len_type": "fixed", "length": 16, "type": "b"},
    "54": {"name": "Additional Amounts", "len_type": "lllvar", "length": 120, "type": "ans"},
    "55": {"name": "ICC Data (EMV)", "len_type": "lllvar", "length": 999, "type": "b"},
    "60": {"name": "Reserved (Visa POS Data)", "len_type": "lllvar", "length": 999, "type": "ans"},
    "62": {
        "name": "Custom Payment Service (Visa)",
        "len_type": "lllvar",
        "length": 999,
        "type": "ans",
    },
    "63": {"name": "Network Data (Visa)", "len_type": "lllvar", "length": 999, "type": "ans"},
    "70": {"name": "Network Management Code", "len_type": "fixed", "length": 3, "type": "n"},
    "90": {"name": "Original Data Elements", "len_type": "fixed", "length": 42, "type": "n"},
    "95": {"name": "Replacement Amounts", "len_type": "fixed", "length": 42, "type": "an"},
}


#: Dialect id (as the Message Format registry names it) -> table.
BUILTIN_TABLES: dict[str, dict[str, dict]] = {
    "iso8583-1987": ISO8583_1987,
    "iso8583-1993": ISO8583_1993,
    "visa-base1": VISA_BASE_I,
}

__all__ = ["BUILTIN_TABLES", "ISO8583_1987", "ISO8583_1993", "VISA_BASE_I"]
