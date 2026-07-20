"""Payload codecs for the NATS adapter family (ADR-0006).

A NATS message body is opaque bytes; payment systems put different things on the
wire. These codecs translate between PayProbe's structured payloads (the dicts a
scenario / flow produces) and the raw bytes a subject carries, so the *same*
adapter and responder work whether a deployment ships JSON events, raw binary, or
ISO 8583 bytes over a subject:

* ``json``    — the default. ``dict``/list ↔ UTF-8 JSON.
* ``bytes``   — raw binary. Encodes from ``{"hex": …}`` / ``{"base64": …}`` /
  ``{"text": …}`` (or a bare ``str``/``bytes``); decodes to a dict exposing all
  three views so a rule or assertion can match whichever is convenient.
* ``iso8583`` — reuse the existing dialect codecs (:mod:`worker.adapters.tcp.iso8583`)
  so a ``{mti, de:{…}}`` payload packs to ASCII ISO 8583 bytes and inbound bytes
  unpack to ``{mti, de, fields}`` — no NATS-only encoding story. The DE table
  comes from the connection's ``fields`` (injected from a Message Format when a
  ``message_format_id`` is bound, exactly like the TCP simulator path).

``encode`` never raises for JSON/bytes; an ISO 8583 encode with a malformed
payload raises a ``ValueError`` the caller turns into a StepResult error.
"""

from __future__ import annotations

import base64
import json
from typing import Any

DEFAULT_CODEC = "json"


def _iso_fields(config: dict | None) -> dict:
    from ..tcp.iso8583 import DEFAULT_FIELDS

    return (config or {}).get("fields") or DEFAULT_FIELDS


def encode(payload: Any, codec: str = DEFAULT_CODEC, config: dict | None = None) -> bytes:
    """Structured payload -> NATS message bytes for ``codec``."""
    codec = (codec or DEFAULT_CODEC).lower()
    if codec == "bytes":
        return _encode_bytes(payload)
    if codec == "iso8583":
        return _encode_iso8583(payload, config)
    # json (default): tolerate an already-encoded str/bytes so a raw body still
    # goes out unchanged.
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload if payload is not None else {}).encode("utf-8")


def decode(raw: bytes, codec: str = DEFAULT_CODEC, config: dict | None = None) -> Any:
    """NATS message bytes -> structured payload for ``codec``. Never raises —
    a body that doesn't fit the codec falls back to a best-effort text view so a
    responder can still trace and match on it."""
    raw = bytes(raw or b"")
    codec = (codec or DEFAULT_CODEC).lower()
    if codec == "bytes":
        return _decode_bytes(raw)
    if codec == "iso8583":
        return _decode_iso8583(raw, config)
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# -- bytes -------------------------------------------------------------------


def _encode_bytes(payload: Any) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, dict):
        if "hex" in payload and payload["hex"] is not None:
            return bytes.fromhex(str(payload["hex"]).replace(" ", ""))
        for k in ("base64", "b64"):
            if payload.get(k) is not None:
                return base64.b64decode(str(payload[k]))
        for k in ("text", "raw"):
            if payload.get(k) is not None:
                return str(payload[k]).encode("utf-8")
    # last resort: JSON-encode so nothing is silently dropped
    return json.dumps(payload).encode("utf-8")


def _decode_bytes(raw: bytes) -> dict:
    return {
        "hex": raw.hex(),
        "base64": base64.b64encode(raw).decode("ascii"),
        "text": raw.decode("utf-8", errors="replace"),
        "len": len(raw),
    }


# -- iso8583 -----------------------------------------------------------------


def _encode_iso8583(payload: Any, config: dict | None) -> bytes:
    from ..tcp.iso8583 import iso_pack

    if not isinstance(payload, dict):
        raise ValueError("iso8583 codec needs a {mti, de:{…}} payload")
    mti = str(payload.get("mti") or payload.get("command") or "")
    if not mti:
        raise ValueError("iso8583 codec payload has no 'mti'")
    de = payload.get("de")
    if not isinstance(de, dict):
        # also accept a flat fields map ({"2": "...", "4": "..."})
        de = {k: v for k, v in payload.items() if str(k).isdigit()}
    return iso_pack(mti, de, _iso_fields(config)).encode("ascii")


def _decode_iso8583(raw: bytes, config: dict | None) -> dict:
    from ..tcp.iso8583 import iso_unpack

    text = raw.decode("ascii", errors="replace")
    try:
        parsed = iso_unpack(text, _iso_fields(config))
    except Exception:  # noqa: BLE001 — malformed frame stays matchable as text
        return {"raw": text, "error": "iso8583 decode failed"}
    # expose a flat {de: value} view alongside the rich fields, matching the
    # flow/trace shape the ISO responders use.
    parsed["de"] = {k: v.get("value") for k, v in (parsed.get("fields") or {}).items()}
    return parsed
