"""Built-in "ISO Messaging" palette group — ISO8583 + ISO20022 helpers.

Code-backed steps (self-contained Python, since the worker runs code nodes in an
isolated subprocess). ISO8583 pack/parse use an **ASCII** representation with a
configurable field table (`FIELDS`) right in the step — edit it per integration,
or load a registered version from the Message Formats manager. ISO20022 build /
parse work on XML via the standard library.
"""
from __future__ import annotations

from .catalog import ActionSpec, TargetSpec

# A generic, configuration-driven ASCII ISO8583 codec, embedded into the steps.
ISO8583_CODEC_SRC = r'''
def _bits_from_hex(h):
    n = int(h, 16); w = len(h) * 4
    return {i + 1 for i in range(w) if n & (1 << (w - 1 - i))}

def _bitmap(des, width):
    n = 0
    for d in des:
        n |= 1 << (width - d)
    return format(n, "0%dX" % (width // 4))

def iso_unpack(msg, FIELDS):
    msg = "".join(msg.split())
    pos = 0
    mti = msg[0:4]; pos = 4
    present = _bits_from_hex(msg[pos:pos + 16]); pos += 16
    if 1 in present:
        present |= {b + 64 for b in _bits_from_hex(msg[pos:pos + 16])}; pos += 16
        present.discard(1)
    fields = {}
    for de in sorted(present):
        sp = FIELDS.get(str(de))
        if not sp:
            fields[str(de)] = {"name": "(unknown)", "value": "", "error": "DE not in spec"}
            break
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            ln = int(sp.get("length", 0)); v = msg[pos:pos + ln]; pos += ln
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            ln = int(msg[pos:pos + pw]); pos += pw; v = msg[pos:pos + ln]; pos += ln
        fields[str(de)] = {"name": sp.get("name", ""), "value": v}
    return {"mti": mti, "de_list": sorted(present), "fields": fields}

def iso_pack(mti, values, FIELDS):
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
        sp = FIELDS[str(d)]; v = values[str(d)]
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            out += v
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            out += str(len(v)).zfill(pw) + v
    return out
'''

# Standard ISO8583:1987 field table (ASCII), as editable source. Names, classes
# and lengths follow the published 1987 data-element directory.
ISO8583_1987_FIELDS_SRC = (
    'FIELDS = {\n'
    '    "2":  {"name": "Primary Account Number (PAN)", "len_type": "llvar",  "length": 19, "type": "n"},\n'
    '    "3":  {"name": "Processing Code",        "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "4":  {"name": "Amount, Transaction",    "len_type": "fixed",  "length": 12, "type": "n"},\n'
    '    "5":  {"name": "Amount, Settlement",     "len_type": "fixed",  "length": 12, "type": "n"},\n'
    '    "6":  {"name": "Amount, Cardholder Billing", "len_type": "fixed", "length": 12, "type": "n"},\n'
    '    "7":  {"name": "Transmission Date & Time", "len_type": "fixed", "length": 10, "type": "n"},\n'
    '    "11": {"name": "System Trace Audit Number","len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "12": {"name": "Time, Local Transaction", "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "13": {"name": "Date, Local Transaction",  "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "14": {"name": "Date, Expiration",        "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "15": {"name": "Date, Settlement",        "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "18": {"name": "Merchant Type (MCC)",     "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "19": {"name": "Acquirer Country Code",   "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "22": {"name": "POS Entry Mode",          "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "25": {"name": "POS Condition Code",      "len_type": "fixed",  "length": 2,  "type": "n"},\n'
    '    "32": {"name": "Acquiring Institution ID","len_type": "llvar",  "length": 11, "type": "n"},\n'
    '    "35": {"name": "Track 2 Data",            "len_type": "llvar",  "length": 37, "type": "z"},\n'
    '    "37": {"name": "Retrieval Reference Number", "len_type": "fixed", "length": 12, "type": "an"},\n'
    '    "38": {"name": "Authorization ID Response", "len_type": "fixed", "length": 6,  "type": "an"},\n'
    '    "39": {"name": "Response Code",           "len_type": "fixed",  "length": 2,  "type": "an"},\n'
    '    "41": {"name": "Card Acceptor Terminal ID","len_type": "fixed",  "length": 8,  "type": "ans"},\n'
    '    "42": {"name": "Card Acceptor ID Code",   "len_type": "fixed",  "length": 15, "type": "ans"},\n'
    '    "43": {"name": "Card Acceptor Name/Location", "len_type": "fixed", "length": 40, "type": "ans"},\n'
    '    "49": {"name": "Currency Code, Transaction", "len_type": "fixed", "length": 3,  "type": "n"},\n'
    '    "52": {"name": "PIN Data",                "len_type": "fixed",  "length": 16, "type": "b"},\n'
    '    "53": {"name": "Security Control Information", "len_type": "fixed", "length": 16, "type": "n"},\n'
    '    "55": {"name": "ICC Data (EMV)",          "len_type": "lllvar", "length": 999,"type": "b"},\n'
    '    "64": {"name": "Message Authentication Code", "len_type": "fixed", "length": 16, "type": "b"},\n'
    '    "70": {"name": "Network Management Code", "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '}\n'
)

# ISO8583:1993-style field table (ASCII). Mirrors the 1987 table with the
# headline 1993 changes: DE 22 becomes the 12-char Point-of-Service Data Code,
# DE 39 becomes the 3-char Action Code, and 1993 additions (DE 56 message reason
# code, DE 95 replacement amounts, network/error DEs) are included.
ISO8583_1993_FIELDS_SRC = (
    'FIELDS = {\n'
    '    "2":  {"name": "Primary Account Number", "len_type": "llvar",  "length": 19, "type": "n"},\n'
    '    "3":  {"name": "Processing Code",        "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "4":  {"name": "Amount, Transaction",    "len_type": "fixed",  "length": 12, "type": "n"},\n'
    '    "7":  {"name": "Transmission Date/Time",  "len_type": "fixed",  "length": 10, "type": "n"},\n'
    '    "11": {"name": "STAN",                    "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "12": {"name": "Date/Time, Local Txn",    "len_type": "fixed",  "length": 12, "type": "n"},\n'
    '    "13": {"name": "Date, Effective",         "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "14": {"name": "Date, Expiration",        "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "18": {"name": "Merchant Type",           "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "22": {"name": "POS Data Code",           "len_type": "fixed",  "length": 12, "type": "an"},\n'
    '    "25": {"name": "POS Condition Code",      "len_type": "fixed",  "length": 2,  "type": "n"},\n'
    '    "32": {"name": "Acquiring Institution ID","len_type": "llvar",  "length": 11, "type": "n"},\n'
    '    "35": {"name": "Track 2 Data",            "len_type": "llvar",  "length": 37, "type": "z"},\n'
    '    "37": {"name": "Retrieval Reference Num", "len_type": "fixed",  "length": 12, "type": "an"},\n'
    '    "38": {"name": "Approval Code",           "len_type": "fixed",  "length": 6,  "type": "an"},\n'
    '    "39": {"name": "Action Code",             "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "41": {"name": "Card Acceptor Terminal",  "len_type": "fixed",  "length": 8,  "type": "ans"},\n'
    '    "42": {"name": "Card Acceptor ID Code",   "len_type": "fixed",  "length": 15, "type": "ans"},\n'
    '    "49": {"name": "Currency Code, Txn",      "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "52": {"name": "PIN Data",                "len_type": "fixed",  "length": 16, "type": "b"},\n'
    '    "53": {"name": "Security Control Info",   "len_type": "llvar",  "length": 48, "type": "b"},\n'
    '    "55": {"name": "ICC Data (EMV)",          "len_type": "lllvar", "length": 999,"type": "b"},\n'
    '    "56": {"name": "Message Reason Code",     "len_type": "llvar",  "length": 4,  "type": "n"},\n'
    '    "70": {"name": "Network Management Code", "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "95": {"name": "Replacement Amounts",     "len_type": "fixed",  "length": 42, "type": "an"},\n'
    '}\n'
)


# VISA Base I — the DE table the bundled VisaSimulator (scheme host) packs and
# parses with: the standard ISO 8583:1987 core plus the VISA-relevant private
# fields its flows touch (DE 43/44/48/54/60/62/63/90/95). Extracted here so the
# simulator can bind it from the registry (message_format_id) instead of carrying
# a private hard-coded copy. Functional, public ISO 8583 layout — DE 62/63 are
# opaque echo fields, not VISA's confidential V.I.P. sub-field structure.
ISO8583_VISA_FIELDS_SRC = (
    'FIELDS = {\n'
    '    "2":  {"name": "Primary Account Number (PAN)", "len_type": "llvar",  "length": 19, "type": "n"},\n'
    '    "3":  {"name": "Processing Code",        "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "4":  {"name": "Amount, Transaction",    "len_type": "fixed",  "length": 12, "type": "n"},\n'
    '    "7":  {"name": "Transmission Date & Time", "len_type": "fixed", "length": 10, "type": "n"},\n'
    '    "11": {"name": "System Trace Audit Number","len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "12": {"name": "Time, Local Transaction", "len_type": "fixed",  "length": 6,  "type": "n"},\n'
    '    "13": {"name": "Date, Local Transaction",  "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "14": {"name": "Date, Expiration",        "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "18": {"name": "Merchant Type (MCC)",     "len_type": "fixed",  "length": 4,  "type": "n"},\n'
    '    "22": {"name": "POS Entry Mode",          "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "25": {"name": "POS Condition Code",      "len_type": "fixed",  "length": 2,  "type": "n"},\n'
    '    "32": {"name": "Acquiring Institution ID","len_type": "llvar",  "length": 11, "type": "n"},\n'
    '    "35": {"name": "Track 2 Data",            "len_type": "llvar",  "length": 37, "type": "z"},\n'
    '    "37": {"name": "Retrieval Reference Number", "len_type": "fixed", "length": 12, "type": "an"},\n'
    '    "38": {"name": "Authorization ID Response", "len_type": "fixed", "length": 6,  "type": "an"},\n'
    '    "39": {"name": "Response Code",           "len_type": "fixed",  "length": 2,  "type": "an"},\n'
    '    "41": {"name": "Card Acceptor Terminal ID","len_type": "fixed",  "length": 8,  "type": "ans"},\n'
    '    "42": {"name": "Card Acceptor ID Code",   "len_type": "fixed",  "length": 15, "type": "ans"},\n'
    '    "43": {"name": "Card Acceptor Name/Location", "len_type": "fixed", "length": 40, "type": "ans"},\n'
    '    "44": {"name": "Additional Response Data", "len_type": "llvar",  "length": 25, "type": "ans"},\n'
    '    "48": {"name": "Additional Data (Private)", "len_type": "lllvar", "length": 999, "type": "ans"},\n'
    '    "49": {"name": "Currency Code, Transaction", "len_type": "fixed", "length": 3,  "type": "n"},\n'
    '    "52": {"name": "PIN Data",                "len_type": "fixed",  "length": 16, "type": "b"},\n'
    '    "54": {"name": "Additional Amounts",      "len_type": "lllvar", "length": 120, "type": "ans"},\n'
    '    "55": {"name": "ICC Data (EMV)",          "len_type": "lllvar", "length": 999,"type": "b"},\n'
    '    "60": {"name": "Reserved (Visa POS Data)", "len_type": "lllvar", "length": 999, "type": "ans"},\n'
    '    "62": {"name": "Custom Payment Service (Visa)", "len_type": "lllvar", "length": 999, "type": "ans"},\n'
    '    "63": {"name": "Network Data (Visa)",     "len_type": "lllvar", "length": 999, "type": "ans"},\n'
    '    "70": {"name": "Network Management Code", "len_type": "fixed",  "length": 3,  "type": "n"},\n'
    '    "90": {"name": "Original Data Elements",  "len_type": "fixed",  "length": 42, "type": "n"},\n'
    '    "95": {"name": "Replacement Amounts",     "len_type": "fixed",  "length": 42, "type": "an"},\n'
    '}\n'
)


# The DE table (FIELDS) now comes from the `fields` Input — pick a registered
# Message Format in the editor to snapshot it here, or edit the JSON directly.
_FIELDS_FROM_INPUT = (
    'import json\n'
    '_f = inputs.get("fields") or {}\n'
    'FIELDS = json.loads(_f) if isinstance(_f, str) else _f\n'
)

_ISO8583_PARSE = (
    '# Parse an ASCII ISO8583 message. `message` + `fields` come from step Inputs.\n'
    '# Wire `message` from an earlier step (e.g. ${iso8583_pack.response.message});\n'
    '# pick a Message Format to fill `fields`, or edit the JSON.\n'
    'message = inputs.get("message", "")\n'
    + _FIELDS_FROM_INPUT + '\n' + ISO8583_CODEC_SRC +
    '\nreturn iso_unpack(message, FIELDS) if message else {"mti": "", "de_list": [], "fields": {}}\n'
)

_ISO8583_PACK = (
    '# Build an ASCII ISO8583 message from MTI + field values (from step Inputs).\n'
    '# Pick a Message Format to fill `fields`, or edit the JSON.\n'
    'mti = inputs.get("mti", "0200")\n'
    'values = inputs.get("values") or {}\n'
    + _FIELDS_FROM_INPUT +
    'if isinstance(values, str):\n'
    '    values = json.loads(values or "{}")\n'
    '\n' + ISO8583_CODEC_SRC +
    '\nreturn {"message": iso_pack(mti, values, FIELDS)}\n'
)


_ISO20022_BUILD = r'''# Build a minimal ISO20022 pacs.008 (FI to FI Customer Credit Transfer).
import json
import xml.etree.ElementTree as ET

data = inputs.get("data") or {}
if isinstance(data, str):
    data = json.loads(data or "{}")
def g(k, default):
    return data.get(k, default)
NS = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
ET.register_namespace("", NS)
def e(parent, tag, text=None):
    el = ET.SubElement(parent, "{%s}%s" % (NS, tag))
    if text is not None:
        el.text = text
    return el

root = ET.Element("{%s}Document" % NS)
ct = e(root, "FIToFICstmrCdtTrf")
grp = e(ct, "GrpHdr"); e(grp, "MsgId", g("msg_id", "MSG-0001")); e(grp, "NbOfTxs", "1")
tx = e(ct, "CdtTrfTxInf")
amt = e(tx, "IntrBkSttlmAmt", g("amount", "0.00")); amt.set("Ccy", g("ccy", "EUR"))
e(e(tx, "Dbtr"), "Nm", g("debtor", ""))
e(e(e(tx, "DbtrAcct"), "Id"), "IBAN", g("iban_dr", ""))
e(e(tx, "Cdtr"), "Nm", g("creditor", ""))
e(e(e(tx, "CdtrAcct"), "Id"), "IBAN", g("iban_cr", ""))
return {"xml": ET.tostring(root, encoding="unicode")}
'''

_ISO20022_PARSE = r'''# Parse / well-formedness check an ISO20022 (or any) XML message.
# `xml` comes from the step Inputs — wire ${iso20022_build.response.xml}.
import xml.etree.ElementTree as ET

xml_text = inputs.get("xml", "")

def _local(t):
    return t.split("}", 1)[-1]

def _walk(el):
    node = {}
    if el.attrib:
        node["@"] = {_local(k): v for k, v in el.attrib.items()}
    children = list(el)
    if children:
        node["children"] = {_local(c.tag): _walk(c) for c in children}
    else:
        node["text"] = (el.text or "").strip()
    return node

try:
    root = ET.fromstring(xml_text)
    ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else None
    return {"well_formed": True, "root": _local(root.tag),
            "namespace": ns, "tree": _walk(root)}
except ET.ParseError as exc:
    return {"well_formed": False, "error": str(exc)}
'''


import json as _json


def _fields_json(src: str) -> str:
    """Execute a ``FIELDS = {...}`` source block and return it as a JSON string."""
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - trusted module constant
    return _json.dumps(ns["FIELDS"])


#: Default DE table (JSON) used when a step ships standalone.
_FIELDS_1987_JSON = _fields_json(ISO8583_1987_FIELDS_SRC)


def _i(name, value):
    return {"name": name, "value": value}


def _ienum(name, value, options):
    """An input row the editor renders as a dropdown (fixed set of choices)."""
    return {"name": name, "value": value, "options": options}


def _iformat(name, value, protocol, format_id="", version=""):
    """An input the editor renders as a Message Format picker. Selecting a format
    snapshots its definition into ``value``; ``formatId``/``formatVersion`` record
    the source so the editor can show (and re-sync) what was snapshotted."""
    return {"name": name, "value": value, "format": protocol,
            "formatId": format_id, "formatVersion": version}


# Common ISO8583 message type indicators (1987 numbering).
_MTIS = ["0100", "0110", "0200", "0210", "0220", "0230",
         "0400", "0410", "0420", "0430", "0800", "0810"]


def _code_action(name, label, response_fields, code, inputs) -> ActionSpec:
    return ActionSpec(
        name=name, label=label, payload_hint={}, response_fields=response_fields,
        behavior={"kind": "code", "template": {
            "language": "python", "code": code, "inputs": inputs,
        }},
    )


_ISO8583_EXAMPLE = (
    "0200723800000A8080001641111111111111110000000000000100000203040506"
    "000001020304020300000000000100TERM0001978"
)
_ISO8583_VALUES = (
    '{"2":"4111111111111111","3":"000000","4":"000000010000",'
    '"11":"000001","41":"TERM0001"}'
)
_ISO20022_DATA = (
    '{"msg_id":"MSG-0001","amount":"100.00","ccy":"EUR","debtor":"Alice",'
    '"creditor":"Bob","iban_dr":"DE89370400440532013000",'
    '"iban_cr":"FR1420041010050500013M02606"}'
)
_ISO20022_XML = (
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf><GrpHdr><MsgId>MSG-0001</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>'
    '<CdtTrfTxInf><IntrBkSttlmAmt Ccy="EUR">100.00</IntrBkSttlmAmt>'
    '<Dbtr><Nm>Alice</Nm></Dbtr><Cdtr><Nm>Bob</Nm></Cdtr></CdtTrfTxInf>'
    '</FIToFICstmrCdtTrf></Document>'
)


ISO_MESSAGING_TARGET = TargetSpec(
    target="iso_messaging",
    label="ISO Messaging",
    category="ISO Messaging",
    color="#58a6ff",
    icon="format",
    description="Code-backed ISO message tools — pack/parse ISO 8583 and build "
                "ISO 20022 messages from a dialect definition.",
    custom=False,
    actions=[
        _code_action("iso8583_parse", "ISO8583 parse", ["mti", "de_list", "fields"],
                     _ISO8583_PARSE,
                     [_i("message", _ISO8583_EXAMPLE),
                      _iformat("fields", _FIELDS_1987_JSON, "iso8583", "iso8583-1987", "1987")]),
        _code_action("iso8583_pack", "ISO8583 pack", ["message"],
                     _ISO8583_PACK,
                     [_ienum("mti", "0200", _MTIS), _i("values", _ISO8583_VALUES),
                      _iformat("fields", _FIELDS_1987_JSON, "iso8583", "iso8583-1987", "1987")]),
        _code_action("iso20022_build", "ISO20022 build (pacs.008)", ["xml"],
                     _ISO20022_BUILD, [_i("data", _ISO20022_DATA)]),
        _code_action("iso20022_parse", "ISO20022 parse/validate",
                     ["well_formed", "root", "namespace", "tree"],
                     _ISO20022_PARSE, [_i("xml", _ISO20022_XML)]),
    ],
)
