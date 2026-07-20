"""Built-in "EMV / Payment Tools" palette group.

Pre-built, code-backed steps that mirror the converters / combinators / parsers
on tools like emvlab.org, paymentcardtools.com and neapay.com — BER-TLV parsing
& building, hex/ASCII/decimal/BCD conversion, DOL parsing, Track 2, service code,
AIP/TVR decoding, Luhn, ISO8583 bitmap, Track 1/2 generation.

**Chaining:** every tool reads its input from the ``inputs`` map (the step's
Input rows, which may reference an earlier step like
``${step_001.response.message}``) and returns a structured object, so the output
of one tool can feed the next. Each action ships with example inputs so it also
runs standalone.
"""
from __future__ import annotations

from .catalog import ActionSpec, TargetSpec

_TAGS_SRC = (
    '{'
    '"4F":"Application Identifier (AID)","50":"Application Label",'
    '"57":"Track 2 Equivalent Data","5A":"Application PAN",'
    '"5F20":"Cardholder Name","5F24":"Application Expiry Date",'
    '"5F25":"Application Effective Date","5F28":"Issuer Country Code",'
    '"5F2A":"Transaction Currency Code","5F2D":"Language Preference",'
    '"5F30":"Service Code","5F34":"PAN Sequence Number",'
    '"61":"Application Template","6F":"FCI Template","70":"Record Template",'
    '"77":"Response Message Template Format 2","80":"Response Message Template Format 1",'
    '"82":"Application Interchange Profile (AIP)","84":"DF Name",'
    '"86":"Issuer Script Command","87":"Application Priority Indicator",'
    '"88":"Short File Identifier (SFI)","8A":"Authorisation Response Code",'
    '"8C":"CDOL1","8D":"CDOL2","8E":"CVM List","8F":"CA Public Key Index",'
    '"90":"Issuer Public Key Certificate","91":"Issuer Authentication Data",'
    '"92":"Issuer Public Key Remainder","93":"Signed Static Application Data",'
    '"94":"Application File Locator (AFL)","95":"Terminal Verification Results (TVR)",'
    '"97":"Transaction Certificate Data Object List (TDOL)","9A":"Transaction Date",'
    '"9B":"Transaction Status Information (TSI)","9C":"Transaction Type","9D":"DDF Name",'
    '"5F36":"Transaction Currency Exponent",'
    '"9F02":"Amount, Authorised","9F03":"Amount, Other",'
    '"9F06":"Application Identifier (Terminal)","9F07":"Application Usage Control",'
    '"9F08":"Application Version Number","9F09":"Application Version Number (Terminal)",'
    '"9F0D":"Issuer Action Code - Default","9F0E":"Issuer Action Code - Denial",'
    '"9F0F":"Issuer Action Code - Online","9F10":"Issuer Application Data",'
    '"9F11":"Issuer Code Table Index","9F12":"Application Preferred Name",'
    '"9F1A":"Terminal Country Code","9F1E":"IFD Serial Number",'
    '"9F21":"Transaction Time","9F26":"Application Cryptogram (ARQC/TC/AAC)",'
    '"9F27":"Cryptogram Information Data","9F2A":"Kernel Identifier",'
    '"9F33":"Terminal Capabilities","9F34":"CVM Results","9F35":"Terminal Type",'
    '"9F36":"Application Transaction Counter (ATC)","9F37":"Unpredictable Number",'
    '"9F38":"PDOL","9F39":"POS Entry Mode","9F40":"Additional Terminal Capabilities",'
    '"9F41":"Transaction Sequence Counter","9F66":"Terminal Transaction Qualifiers (TTQ)",'
    '"A5":"FCI Proprietary Template","BF0C":"FCI Issuer Discretionary Data"'
    '}'
)


_TLV_PARSE = '''# EMV TLV (BER-TLV) parser. Input `data` comes from the step's Inputs
# (wire it from an earlier step, e.g. ${step_001.response.message}).
data = inputs.get("data", "")

TAGS = ''' + _TAGS_SRC + r'''

def _printable(v):
    return v.decode("ascii") if v and all(32 <= c < 127 for c in v) else None

def _parse(b, i, out):
    while i < len(b):
        if b[i] in (0x00, 0xFF):
            i += 1; continue
        s = i; t = b[i]; i += 1
        if (t & 0x1F) == 0x1F:
            while i < len(b) and (b[i] & 0x80):
                i += 1
            i += 1
        tag = b[s:i].hex().upper()
        ln = b[i]; i += 1
        if ln & 0x80:
            n = ln & 0x7F; ln = int.from_bytes(b[i:i + n], "big"); i += n
        val = b[i:i + ln]; i += ln
        node = {"tag": tag, "name": TAGS.get(tag, "(unknown)"),
                "length": ln, "value": val.hex().upper()}
        if int(tag[:2], 16) & 0x20:
            node["children"] = []; _parse(val, 0, node["children"])
        else:
            node["ascii"] = _printable(val)
        out.append(node)
    return out

tags = _parse(bytes.fromhex("".join(data.split())), 0, []) if data else []
return {"tags": tags, "count": len(tags)}
'''


_TLV_BUILD = r'''# EMV TLV builder (combinator). `items` is a list of {tag, value} (hex).
import json
items = inputs.get("items", [])
if isinstance(items, str):
    items = json.loads(items or "[]")

def _enc_len(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body

out = b""
for it in items:
    t = bytes.fromhex(it["tag"]); v = bytes.fromhex("".join(it.get("value", "").split()))
    out += t + _enc_len(len(v)) + v
return {"tlv": out.hex().upper(), "length": len(out)}
'''


_HEX_ASCII = r'''# Hex <-> ASCII converter.
value = inputs.get("value", "")
mode = inputs.get("mode", "decode")   # decode: hex->ascii | encode: ascii->hex
if mode == "encode":
    return {"input": value, "hex": value.encode("ascii").hex().upper(), "ascii": value}
b = bytes.fromhex("".join(value.split()))
return {"input": value, "hex": b.hex().upper(), "ascii": b.decode("ascii", errors="replace")}
'''


_DOL_PARSE = '''# DOL parser (PDOL / CDOL / TDOL) — tag + length pairs, no values.
data = inputs.get("data", "")

TAGS = ''' + _TAGS_SRC + r'''

def _parse(b):
    out = []; i = 0
    while i < len(b):
        s = i; t = b[i]; i += 1
        if (t & 0x1F) == 0x1F:
            while i < len(b) and (b[i] & 0x80):
                i += 1
            i += 1
        tag = b[s:i].hex().upper()
        ln = b[i] if i < len(b) else 0; i += 1
        out.append({"tag": tag, "name": TAGS.get(tag, "(unknown)"), "length": ln})
    return out

entries = _parse(bytes.fromhex("".join(data.split()))) if data else []
return {"entries": entries, "total_length": sum(e["length"] for e in entries)}
'''


_DOL_BUILD = '''# DOL/CDOL builder — assemble the value stream a card MACs for the
# Application Cryptogram. `dol` is the DOL itself (tag+length pairs, e.g. CDOL1);
# `values` is {tag: hex}. Values are emitted in DOL order, padded/truncated to each
# element length, so the output `data` feeds straight into the ARQC step.
import json
dol = "".join(inputs.get("dol", "").split())
values = inputs.get("values", {})
if isinstance(values, str):
    values = json.loads(values or "{}")
values = {str(k).upper().replace(" ", ""): "".join(str(v).split()).upper()
          for k, v in values.items()}

TAGS = ''' + _TAGS_SRC + r'''

def _entries(b):
    out = []; i = 0
    while i < len(b):
        s = i; t = b[i]; i += 1
        if (t & 0x1F) == 0x1F:
            while i < len(b) and (b[i] & 0x80):
                i += 1
            i += 1
        tag = b[s:i].hex().upper()
        ln = b[i] if i < len(b) else 0; i += 1
        out.append((tag, ln))
    return out

stream = ""; elements = []; missing = []
for tag, ln in _entries(bytes.fromhex(dol)) if dol else []:
    need = ln * 2
    raw = values.get(tag, "")
    val = raw[:need] if len(raw) >= need else raw.ljust(need, "0")
    if not raw:
        missing.append(tag)
    stream += val
    elements.append({"tag": tag, "name": TAGS.get(tag, "(unknown)"),
                     "length": ln, "value": val.upper(), "supplied": bool(raw)})
return {"data": stream.upper(), "length": len(stream) // 2,
        "elements": elements, "missing_tags": missing}
'''


_TRACK2 = r'''# Track 2 (equivalent) data parser.
data = inputs.get("data", "")
s = "".join(data.split()).upper()
sep = "D" if "D" in s else ("=" if "=" in s else None)
pan, rest = (s.split(sep, 1) if sep else (s, ""))
return {"pan": pan, "expiry_yymm": rest[0:4], "service_code": rest[4:7],
        "discretionary_data": rest[7:].rstrip("F")}
'''


_SERVICE_CODE = r'''# Service code (3 digits) decoder.
code = str(inputs.get("code", ""))
d1, d2, d3 = (code + "   ")[0], (code + "   ")[1], (code + "   ")[2]
pos1 = {"1": "International interchange", "2": "International interchange, ICC preferred",
        "5": "National interchange", "6": "National interchange, ICC preferred",
        "7": "Private", "9": "Test"}
pos2 = {"0": "Normal authorisation", "2": "Authorisation by issuer (online)",
        "4": "Authorisation by issuer, except bilateral agreement"}
pos3 = {"0": "No restrictions, PIN required", "1": "No restrictions",
        "2": "Goods and services only", "3": "ATM only, PIN required",
        "4": "Cash only", "5": "Goods and services only, PIN required",
        "6": "No restrictions, prompt for PIN if PED present",
        "7": "Goods and services only, prompt for PIN if PED present"}
return {"interchange": pos1.get(d1, "?"), "authorisation_processing": pos2.get(d2, "?"),
        "allowed_services_pin": pos3.get(d3, "?")}
'''


_AIP = r'''# Application Interchange Profile (AIP, tag 82) decoder — 2 bytes.
data = inputs.get("data", "")
b = bytes.fromhex("".join(data.split())); b1 = b[0]
flags = []
if b1 & 0x40: flags.append("SDA supported")
if b1 & 0x20: flags.append("DDA supported")
if b1 & 0x10: flags.append("Cardholder verification supported")
if b1 & 0x08: flags.append("Terminal risk management to be performed")
if b1 & 0x04: flags.append("Issuer authentication supported")
if b1 & 0x02: flags.append("On-device cardholder verification supported")
if b1 & 0x01: flags.append("CDA supported")
return {"byte1_bits": format(b1, "08b"), "flags": flags}
'''


_TVR = r'''# Terminal Verification Results (TVR, tag 95) decoder — 5 bytes.
data = inputs.get("data", "")
MEANINGS = {
    (0, 0x80): "Offline data authentication was not performed",
    (0, 0x40): "SDA failed", (0, 0x20): "ICC data missing",
    (0, 0x10): "Card appears on terminal exception file",
    (0, 0x08): "DDA failed", (0, 0x04): "CDA failed",
    (1, 0x80): "ICC and terminal have different application versions",
    (1, 0x40): "Expired application", (1, 0x20): "Application not yet effective",
    (1, 0x10): "Requested service not allowed for card product", (1, 0x08): "New card",
    (2, 0x80): "Cardholder verification was not successful",
    (2, 0x40): "Unrecognised CVM", (2, 0x20): "PIN Try Limit exceeded",
    (2, 0x10): "PIN entry required and PIN pad not present or not working",
    (2, 0x08): "PIN entry required, PIN pad present, but PIN was not entered",
    (2, 0x04): "Online PIN entered",
    (3, 0x80): "Transaction exceeds floor limit",
    (3, 0x40): "Lower consecutive offline limit exceeded",
    (3, 0x20): "Upper consecutive offline limit exceeded",
    (3, 0x10): "Transaction selected randomly for online processing",
    (3, 0x08): "Merchant forced transaction online",
    (4, 0x80): "Default TDOL used", (4, 0x40): "Issuer authentication failed",
    (4, 0x20): "Script processing failed before final GENERATE AC",
    (4, 0x10): "Script processing failed after final GENERATE AC",
}
b = bytes.fromhex("".join(data.split()))
flags = [m for (idx, mask), m in MEANINGS.items() if idx < len(b) and b[idx] & mask]
return {"bytes": b.hex().upper(), "set_flags": flags, "ok": len(flags) == 0}
'''


_LUHN = r'''# Card number (PAN) Luhn / mod-10 validate + complete.
pan = str(inputs.get("pan", ""))
def _sum(num, dbl):
    total = 0; alt = dbl
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9: d -= 9
        total += d; alt = not alt
    return total
digits = "".join(c for c in pan if c.isdigit())
valid = len(digits) > 0 and _sum(digits, False) % 10 == 0
check = str((10 - _sum(digits[:-1], True) % 10) % 10) if len(digits) > 1 else None
return {"pan": digits, "valid": valid, "check_digit": digits[-1] if digits else None,
        "correct_pan": (digits[:-1] + check) if check is not None else digits}
'''


_HEX_DEC_BCD = r'''# Hex <-> Decimal <-> BCD converter.
value = str(inputs.get("value", "")).strip()
mode = inputs.get("mode", "dec2hex")   # dec2hex | hex2dec | dec2bcd | bcd2dec
if mode == "dec2hex":
    out = {"hex": format(int(value), "X")}
elif mode == "hex2dec":
    out = {"dec": int(value, 16)}
elif mode == "dec2bcd":
    out = {"bcd_hex": value if len(value) % 2 == 0 else "0" + value,
           "bits": " ".join(format(int(c), "04b") for c in value)}
elif mode == "bcd2dec":
    out = {"dec": value.lstrip("0") or "0"}
else:
    out = {"error": "mode must be dec2hex | hex2dec | dec2bcd | bcd2dec"}
return out
'''


_BITMAP = r'''# ISO8583 bitmap decode / build.
import json
mode = inputs.get("mode", "decode")
bitmap_hex = inputs.get("bitmap_hex", "")
fields = inputs.get("fields", [])
if isinstance(fields, str):
    fields = json.loads(fields or "[]")

def _decode(h):
    h = "".join(h.split()); n = int(h, 16); w = len(h) * 4
    return [i + 1 for i in range(w) if n & (1 << (w - 1 - i))]

def _build(fs):
    fs = set(int(f) for f in fs); w = 128 if any(f > 64 for f in fs) else 64
    if w == 128: fs.add(1)
    n = 0
    for f in fs:
        n |= 1 << (w - f)
    return format(n, "0%dX" % (w // 4))

if mode == "decode":
    return {"fields": _decode(bitmap_hex)}
return {"bitmap": _build(fields)}
'''


_TRACK_GEN = r'''# Build Track 1 & Track 2 from card data.
pan = str(inputs.get("pan", ""))
expiry = str(inputs.get("expiry", ""))        # YYMM
service = str(inputs.get("service", ""))
name = str(inputs.get("name", ""))
disc = str(inputs.get("discretionary", ""))
track2 = "%sD%s%s%s" % (pan, expiry, service, disc)
track1 = "B%s^%s^%s%s%s" % (pan, name, expiry, service, disc)
return {"track1": track1, "track2": track2}
'''


def _i(name, value):
    return {"name": name, "value": value}


def _ienum(name, value, options):
    """An input row the editor renders as a dropdown (fixed set of choices)."""
    return {"name": name, "value": value, "options": options}


def _code_action(name, label, response_fields, code, inputs) -> ActionSpec:
    return ActionSpec(
        name=name, label=label, payload_hint={}, response_fields=response_fields,
        behavior={"kind": "code", "template": {
            "language": "python", "code": code, "inputs": inputs,
        }},
    )


EMV_TOOLS_TARGET = TargetSpec(
    target="emv_tools",
    label="EMV / Payment Tools",
    category="EMV / Payment Tools",
    color="#e3b341",
    icon="layers",
    description="Code-backed EMV/payment converters — BER-TLV parse/build, "
                "hex/ASCII/BCD, DOL, Track 2, AIP/TVR decode, Luhn and more.",
    custom=False,
    actions=[
        _code_action("tlv_parse", "TLV parser (BER-TLV)", ["tags", "count"], _TLV_PARSE,
                     [_i("data", "6F1A840E315041592E5359532E4444463031A5088801025F2D02656E")]),
        _code_action("tlv_build", "TLV builder", ["tlv", "length"], _TLV_BUILD,
                     [_i("items", '[{"tag":"9F02","value":"000000001000"},{"tag":"5F2A","value":"0840"},{"tag":"9A","value":"260618"}]')]),
        _code_action("hex_ascii", "Hex ⇄ ASCII", ["hex", "ascii"], _HEX_ASCII,
                     [_i("value", "315041592E5359532E4444463031"),
                      _ienum("mode", "decode", ["decode", "encode"])]),
        _code_action("dol_parse", "DOL parser (PDOL/CDOL)", ["entries", "total_length"], _DOL_PARSE,
                     [_i("data", "9F66049F02069F03069F1A0295055F2A029A039C019F3704")]),
        _code_action("dol_build", "DOL/CDOL builder → ARQC data",
                     ["data", "length", "elements", "missing_tags"], _DOL_BUILD,
                     [_i("dol", "9F02069F03069F1A0295055F2A029A039C019F370482029F3602"),
                      _i("values", '{"9F02":"000000001000","9F03":"000000000000",'
                         '"9F1A":"0840","95":"0000000000","5F2A":"0840","9A":"260618",'
                         '"9C":"00","9F37":"12345678","82":"7C00","9F36":"001C"}')]),
        _code_action("track2_parse", "Track 2 parser", ["pan", "expiry_yymm", "service_code", "discretionary_data"], _TRACK2,
                     [_i("data", "4761739001010119D24122011758928889")]),
        _code_action("service_code", "Service code decoder", ["interchange", "authorisation_processing", "allowed_services_pin"], _SERVICE_CODE,
                     [_i("code", "201")]),
        _code_action("aip_decode", "AIP decoder", ["byte1_bits", "flags"], _AIP,
                     [_i("data", "7C00")]),
        _code_action("tvr_decode", "TVR decoder", ["bytes", "set_flags", "ok"], _TVR,
                     [_i("data", "0000000000")]),
        _code_action("card_luhn", "Card Luhn validate/generate", ["pan", "valid", "check_digit", "correct_pan"], _LUHN,
                     [_i("pan", "4111111111111111")]),
        _code_action("hex_dec_bcd", "Hex ⇄ Decimal ⇄ BCD", ["hex", "dec", "bcd_hex"], _HEX_DEC_BCD,
                     [_i("value", "255"),
                      _ienum("mode", "dec2hex", ["dec2hex", "hex2dec", "dec2bcd", "bcd2dec"])]),
        _code_action("iso_bitmap", "ISO8583 bitmap decode/build", ["fields", "bitmap"], _BITMAP,
                     [_ienum("mode", "decode", ["decode", "build"]),
                      _i("bitmap_hex", "7234000108000000"), _i("fields", "[2,3,4,7,11]")]),
        _code_action("track_generator", "Track 1/2 generator", ["track1", "track2"], _TRACK_GEN,
                     [_i("pan", "4761739001010119"), _i("expiry", "2412"), _i("service", "201"),
                      _i("name", "DOE/JOHN"), _i("discretionary", "0000000")]),
    ],
)
