"""EMV BER-TLV (DE 55 and other constructed fields) — moved here by ADR-0011
so the worker can read EMV tags from a real binary DE 55 in a later step.
Values in and out are uppercase hex strings; the tree is a list of
``{"tag", "name", "length", "value" | "children"}`` nodes."""

from __future__ import annotations

from typing import Any

EMV_TAGS: dict[str, str] = {
    "4F": "Application Identifier (AID)",
    "50": "Application Label",
    "57": "Track 2 Equivalent Data",
    "5A": "Application PAN",
    "82": "Application Interchange Profile (AIP)",
    "84": "Dedicated File Name",
    "8A": "Authorisation Response Code",
    "95": "Terminal Verification Results (TVR)",
    "9A": "Transaction Date",
    "9C": "Transaction Type",
    "5F2A": "Transaction Currency Code",
    "5F34": "PAN Sequence Number",
    "9F02": "Amount, Authorised",
    "9F03": "Amount, Other",
    "9F10": "Issuer Application Data (IAD)",
    "9F1A": "Terminal Country Code",
    "9F26": "Application Cryptogram (ARQC/TC/AAC)",
    "9F27": "Cryptogram Information Data",
    "9F33": "Terminal Capabilities",
    "9F34": "CVM Results",
    "9F36": "Application Transaction Counter (ATC)",
    "9F37": "Unpredictable Number",
    "9F1E": "IFD Serial Number",
    "9F09": "Application Version Number",
    "9F35": "Terminal Type",
    "9F53": "Transaction Category Code",
    "9F6E": "Form Factor Indicator",
}


def parse_tlv(hexstr: str) -> list[dict]:
    """Parse a BER-TLV byte string (hex) into a nested tag/length/value tree."""
    data = bytes.fromhex(hexstr)
    return _tlv(data, 0, len(data))


def _encode_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return bytes([0x80 | len(body)]) + body


def build_tlv(nodes: list[dict]) -> str:
    """Inverse of :func:`parse_tlv`: encode tag/value (or tag/children) nodes
    into a BER-TLV hex string. A node with ``children`` is constructed; a node
    with ``value`` (hex) is primitive."""
    out = bytearray()
    for node in nodes:
        tag = bytes.fromhex(node["tag"])
        children = node.get("children")
        if children:
            value = bytes.fromhex(build_tlv(children))
        else:
            value = bytes.fromhex((node.get("value") or "").replace(" ", ""))
        out += tag
        out += _encode_len(len(value))
        out += value
    return out.hex().upper()


def _tlv(data: bytes, i: int, end: int) -> list[dict]:
    out: list[dict] = []
    while i < end:
        first = data[i]
        tag_bytes = [first]
        i += 1
        if first & 0x1F == 0x1F:  # multi-byte tag
            while i < end:
                tag_bytes.append(data[i])
                more = data[i] & 0x80
                i += 1
                if not more:
                    break
        tag = bytes(tag_bytes).hex().upper()
        constructed = bool(first & 0x20)
        if i >= end:
            break
        length_byte = data[i]
        i += 1
        if length_byte & 0x80:
            n = length_byte & 0x7F
            length = int.from_bytes(data[i : i + n], "big")
            i += n
        else:
            length = length_byte
        value = data[i : i + length]
        i += length
        node: dict[str, Any] = {"tag": tag, "name": EMV_TAGS.get(tag), "length": length}
        if constructed:
            node["children"] = _tlv(value, 0, len(value))
        else:
            node["value"] = value.hex().upper()
        out.append(node)
    return out


__all__ = ["EMV_TAGS", "build_tlv", "parse_tlv"]
