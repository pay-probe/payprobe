"""Built-in "ISO Messaging" palette group — ISO8583 + ISO20022 helpers.

Code-backed steps (self-contained Python, since the worker runs code nodes in an
isolated subprocess). ISO8583 pack/parse use an **ASCII** representation with a
configurable field table (`FIELDS`) right in the step — edit it per integration,
or load a registered version from the Message Formats manager. The codec source
and the default table come from ``payprobe_common.iso8583`` (ADR-0011). ISO20022 build /
parse work on XML via the standard library.
"""
from __future__ import annotations

from payprobe_common.iso8583 import ISO8583_1987
from payprobe_common.iso8583.portable import codec_source

from .catalog import ActionSpec, TargetSpec

# The ISO 8583 codec pasted into the steps. Code steps run in ``python -I`` with
# no PYTHONPATH (worker/engine/code_runner.py), so they cannot import the shared
# codec; the paste is therefore *generated* from ``payprobe_common.iso8583.portable``
# and ``test_iso_catalog_codec_parity`` keeps it byte-identical to the shared codec
# under the ASCII profile (ADR-0011). Binary profiles use the ``tcp`` step.
ISO8583_CODEC_SRC = codec_source()

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

#: Default DE table (JSON) used when a step ships standalone — the shared 1987 dictionary.
_FIELDS_1987_JSON = _json.dumps(ISO8583_1987)


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
