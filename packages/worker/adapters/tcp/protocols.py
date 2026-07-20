"""Pluggable wire protocols for the persistent TCP transport.

The transport (``adapter.TcpAdapter``) owns the socket, the background reader,
request/response correlation, sign-on, keep-alive and framing. It knows nothing
about message *content* — that is a protocol's job. A protocol turns a step's
``(action, payload)`` into the bytes that go on the wire and a correlation key,
and turns inbound bytes back into a parsed dict with the same correlation key.

This keeps the same long-lived, multiplexed connection usable for very
different hosts:

* ``iso8583``     — ASCII ISO 8583 (switches, acquirers, issuer simulators).
* ``header_echo`` — host-command protocols where the host echoes a leading
                    header that we use to match the reply (HSMs such as Thales
                    payShield, and many simple request/response hosts).

Add a new protocol by subclassing :class:`TcpProtocol` and registering it in
:func:`make_protocol`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import iso8583


@dataclass
class EncodedMessage:
    """A message ready for the transport to frame and send."""

    key: str  # correlation key the reply must resolve to
    body: bytes  # protocol body (transport adds framing/TPDU)
    request: dict = field(default_factory=dict)  # human-readable request repr


class TcpProtocol(ABC):
    """Strategy interface the transport delegates content handling to."""

    name = "abstract"

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def encode(self, action: str, payload: dict) -> EncodedMessage:
        """Build the outbound message + the key its reply will carry."""

    @abstractmethod
    def decode(self, body: bytes) -> dict:
        """Parse an inbound body (TPDU already stripped) into a dict."""

    @abstractmethod
    def correlation_key(self, parsed: dict) -> str | None:
        """Extract the correlation key from a parsed inbound message."""

    @abstractmethod
    def shape_response(self, parsed: dict) -> dict:
        """Flatten a parsed message into assertable ``response`` fields."""

    def health_probe(self) -> tuple[str, dict]:
        """Default ``(action, payload)`` for sign-on / health checks."""
        return ("", {})

    def is_healthy(self, parsed: dict) -> bool:
        """Did a health-probe reply indicate a live, healthy peer?"""
        return bool(parsed)


# --------------------------------------------------------------------------- #
# ISO 8583
# --------------------------------------------------------------------------- #

_ACTION_MTI = {
    "send_0100": "0100",
    "send_0200": "0200",
    "send_0220": "0220",
    "send_0400": "0400",
    "send_0420": "0420",
    "send_0800": "0800",
}
_DEFAULT_FIELD_MAP = {
    "pan": "2",
    "processing_code": "3",
    "amount": "4",
    "stan": "11",
    "pos_entry_mode": "22",
    "rrn": "37",
    "response_code": "39",
    "terminal_id": "41",
    "currency": "49",
}
_DEFAULT_RESPONSE_MTI_MAP = {
    "0100": "0110",
    "0200": "0210",
    "0220": "0230",
    "0400": "0410",
    "0420": "0430",
    "0800": "0810",
}


class Iso8583Protocol(TcpProtocol):
    """ASCII ISO 8583. Correlation by a configurable DE (STAN / DE 11)."""

    name = "iso8583"

    def __init__(self, config: dict):
        super().__init__(config)
        c = config.get("correlation", {})
        self.corr_field = str(c.get("field", "11"))
        self.corr_auto = bool(c.get("auto_generate", True))
        self.match_mti = bool(c.get("match_mti", False))
        self.resp_mti_map = {**_DEFAULT_RESPONSE_MTI_MAP, **c.get("response_mti_map", {})}
        self.fields = config.get("fields") or iso8583.DEFAULT_FIELDS
        self.field_map = {**_DEFAULT_FIELD_MAP, **config.get("field_map", {})}
        self.auto_fields = bool(config.get("auto_fields", True))
        self.encoding = config.get("framing", {}).get("encoding", "ascii")
        self._stan = 0

    def encode(self, action: str, payload: dict) -> EncodedMessage:
        mti = str(payload.get("mti") or _ACTION_MTI.get(action) or "0200")
        # A step may pin its own dialect by snapshotting a Message Format's DE
        # table into ``fields``; otherwise pack with the connection's table.
        fields = self._resolve_fields(payload)
        values = self._build_values(payload, fields)
        corr = values.get(self.corr_field)
        if not corr and self.corr_auto:
            corr = self._next_stan(fields)
            values[self.corr_field] = corr
        if not corr:
            raise ValueError(
                f"No correlation value for DE{self.corr_field} and " f"auto_generate is off."
            )
        if self.auto_fields:
            self._stamp_time_fields(values, fields)
        expected = self.resp_mti_map.get(mti, mti) if self.match_mti else None
        body = iso8583.iso_pack(mti, values, fields).encode(self.encoding)
        return EncodedMessage(self._key(corr, expected), body, {"mti": mti, "values": values})

    def _resolve_fields(self, payload: dict) -> dict:
        """Effective DE table for this message: a per-step ``fields`` override
        (dict or JSON string, e.g. a snapshotted Message Format) if supplied and
        non-empty, otherwise the connection/environment default ``self.fields``."""
        f = payload.get("fields")
        if isinstance(f, str):
            try:
                f = json.loads(f or "{}")
            except json.JSONDecodeError:
                f = None
        return f if isinstance(f, dict) and f else self.fields

    def decode(self, body: bytes) -> dict:
        text = body.decode(self.encoding, errors="replace")
        return iso8583.iso_unpack(text, self.fields)

    def correlation_key(self, parsed: dict) -> str | None:
        corr = (parsed.get("fields", {}).get(self.corr_field) or {}).get("value")
        if corr is None:
            return None
        return self._key(corr, parsed.get("mti") if self.match_mti else None)

    def shape_response(self, parsed: dict) -> dict:
        de = {k: v.get("value") for k, v in parsed.get("fields", {}).items()}
        return {
            "mti": parsed.get("mti"),
            "de_list": parsed.get("de_list", []),
            "fields": de,
            "response_code": de.get("39"),
            "stan": de.get("11"),
            "rrn": de.get("37"),
            "auth_code": de.get("38"),
        }

    def health_probe(self) -> tuple[str, dict]:
        return ("send_0800", {"mti": "0800", "values": {"70": "301"}})

    def is_healthy(self, parsed: dict) -> bool:
        return bool(parsed.get("mti"))

    # -- helpers --
    def _key(self, corr: str, mti: str | None) -> str:
        return f"{corr}:{mti}" if mti else corr

    def _build_values(self, payload: dict, fields: dict | None = None) -> dict[str, str]:
        fields = fields if fields is not None else self.fields
        values: dict[str, str] = {}
        for key, de in self.field_map.items():
            if key in payload and payload[key] not in (None, ""):
                values[de] = self._coerce(de, payload[key], fields)
        explicit = payload.get("values")
        if isinstance(explicit, str):
            explicit = json.loads(explicit or "{}")
        if isinstance(explicit, dict):
            for de, v in explicit.items():
                values[str(de)] = str(v)
        return values

    def _coerce(self, de: str, value: Any, fields: dict | None = None) -> str:
        spec = (fields if fields is not None else self.fields).get(de, {})
        if de == "4":
            return str(int(value)).zfill(int(spec.get("length", 12)))
        if spec.get("len_type", "fixed") == "fixed":
            length = int(spec.get("length", 0))
            s = str(value)
            return s.zfill(length) if length and s.isdigit() else s
        return str(value)

    def _next_stan(self, fields: dict | None = None) -> str:
        fields = fields if fields is not None else self.fields
        self._stan = (self._stan + 1) % 1_000_000
        width = int(fields.get(self.corr_field, {}).get("length", 6))
        return str(self._stan).zfill(width)

    def _stamp_time_fields(self, values: dict[str, str], fields: dict | None = None) -> None:
        """Auto-fill transmission / local date-time DEs, sized to the dialect.

        1987 carries short forms (DE7 MMDDhhmmss, DE12 hhmmss); 1993 widens
        them (DE7/DE12 YYYYMMDDhhmmss). Pick the format from each field's declared
        length so the stamp is valid whatever dialect is bound."""
        fields = fields if fields is not None else self.fields
        now = datetime.now(timezone.utc)
        full = now.strftime("%Y%m%d%H%M%S")  # 14: YYYYMMDDhhmmss
        by_len = {
            14: full,
            12: full[2:],  # YYMMDDhhmmss
            10: now.strftime("%m%d%H%M%S"),  # MMDDhhmmss
            6: now.strftime("%H%M%S"),  # hhmmss
            4: now.strftime("%m%d"),  # MMDD
        }
        for de in ("7", "12", "13"):
            spec = fields.get(de)
            if not spec or de in values:
                continue
            stamp = by_len.get(int(spec.get("length", 0) or 0))
            if stamp:
                values[de] = stamp


# --------------------------------------------------------------------------- #
# Header-echo (HSM and other host-command protocols)
# --------------------------------------------------------------------------- #


class HeaderEchoProtocol(TcpProtocol):
    """Request/response where the host echoes a leading header for matching.

    Models HSMs (e.g. Thales payShield) and similar simple host-command links:
    each message starts with a fixed-width header chosen by the sender, which
    the host returns verbatim in the reply — that header is the correlation key.
    The reply continues with a response command code and a return/error code.

    Config (under ``header_echo`` or the adapter root)::

        {
          "header_bytes": 4,            # width of the echoed header
          "auto_header": true,          # auto-assign a sequential header
          "response_command_bytes": 2,  # width of the reply command code
          "error_field_bytes": 2,       # width of the reply return/error code
          "ok_codes": ["00"],           # error codes considered "healthy"
          "command_map": { "<action>": "<command>" },
          "diagnostic_command": "NC",   # health-check / sign-on command
          "encoding": "ascii"
        }
    """

    name = "header_echo"

    def __init__(self, config: dict):
        super().__init__(config)
        h = config.get("header_echo", config)
        self.header_bytes = int(h.get("header_bytes", 4))
        self.auto_header = bool(h.get("auto_header", True))
        self.resp_cmd_bytes = int(h.get("response_command_bytes", 2))
        self.error_bytes = int(h.get("error_field_bytes", 2))
        self.ok_codes = set(h.get("ok_codes", ["00"]))
        self.command_map = dict(h.get("command_map", {}))
        self.diagnostic_command = h.get("diagnostic_command", "NC")
        self.encoding = h.get("encoding", config.get("framing", {}).get("encoding", "ascii"))
        self._seq = 0

    def encode(self, action: str, payload: dict) -> EncodedMessage:
        header = payload.get("header")
        if not header and self.auto_header:
            header = self._next_header()
        if not header:
            raise ValueError("No header for correlation and auto_header is off.")
        header = str(header).zfill(self.header_bytes)[: self.header_bytes]

        command = payload.get("command") or self.command_map.get(action, "")
        if "message" in payload and payload["message"] is not None:
            tail = str(payload["message"])
        else:
            tail = f"{command}{payload.get('data', '')}"
        body = f"{header}{tail}".encode(self.encoding)
        return EncodedMessage(
            header, body, {"header": header, "command": command, "data": payload.get("data", "")}
        )

    def decode(self, body: bytes) -> dict:
        text = body.decode(self.encoding, errors="replace")
        hb, rcb, eb = self.header_bytes, self.resp_cmd_bytes, self.error_bytes
        header = text[:hb]
        rest = text[hb:]
        command = rest[:rcb]
        error = rest[rcb : rcb + eb]
        data = rest[rcb + eb :]
        return {
            "header": header,
            "command": command,
            "error_code": error,
            "data": data,
            "raw": text,
        }

    def correlation_key(self, parsed: dict) -> str | None:
        return parsed.get("header") or None

    def shape_response(self, parsed: dict) -> dict:
        return {
            "header": parsed.get("header"),
            "command": parsed.get("command"),
            "error_code": parsed.get("error_code"),
            "response_code": parsed.get("error_code"),  # uniform with iso8583
            "data": parsed.get("data"),
        }

    def health_probe(self) -> tuple[str, dict]:
        return ("", {"command": self.diagnostic_command})

    def is_healthy(self, parsed: dict) -> bool:
        return bool(parsed.get("header")) and (
            not self.ok_codes or parsed.get("error_code") in self.ok_codes
        )

    def _next_header(self) -> str:
        self._seq = (self._seq + 1) % (10**self.header_bytes)
        return str(self._seq).zfill(self.header_bytes)


# --------------------------------------------------------------------------- #

_PROTOCOLS = {
    "iso8583": Iso8583Protocol,
    "header_echo": HeaderEchoProtocol,
    "hsm": HeaderEchoProtocol,  # alias
}


def make_protocol(config: dict) -> TcpProtocol:
    name = config.get("protocol", "iso8583")
    cls = _PROTOCOLS.get(name)
    if cls is None:
        raise ValueError(f"Unknown TCP protocol {name!r}. Available: {sorted(_PROTOCOLS)}")
    return cls(config)
