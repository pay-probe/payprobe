"""PayShieldSimulator — a Thales payShield 10K Host-Command simulator.

The HSM counterpart of the host/acquirer responder: instead of matching ISO 8583
rules, it speaks the payShield host-command protocol — a length-prefixed frame
carrying ``header + command(2A) + data``, answered with
``header + response(2A) + error(2A) + data``. It dispatches each command to a
handler (:mod:`.commands`) that performs real payment cryptography under a test
LMK (:mod:`.lmk`), so PayProbe can stand in for an HSM during loopback testing
with no physical appliance.

It subclasses :class:`~worker.adapters.tcp.responder.TcpResponder` to inherit all
the framing, lifecycle, live metrics and chaos/fault-injection machinery — only
the decode/encode/dispatch steps are payShield-specific. That also makes it
drop-in compatible with the orchestrator's saved-simulator registry and the
portal Simulators page.

Config (JSON-friendly)::

    {
      "protocol": "payshield",
      "host": "0.0.0.0", "port": 1500,
      "header_bytes": 4,                       # width of the echoed host header
      "framing": { "length_prefix_bytes": 2, "length_byte_order": "big",
                   "encoding": "ascii" },
      "lmk": "0123456789ABCDEFFEDCBA9876543210",   # optional test LMK override
      "firmware": "0007-E000",                      # optional NC firmware string
      "commands": {                                 # optional scripted overrides
        "NC": { "error": "00", "data": "..." }      # force a command's reply
      }
    }

Supported commands: NC (diagnostics), A0 (generate key), BU (key check value),
CW/CY (generate/verify CVV/CVC), CA/CC/G0 (translate PIN blocks), DE (generate
IBM 3624 PIN offset), BA/BE (verify terminal/interchange PIN, IBM method), JA
(generate Visa PVV), EC (verify PIN via PVV), M6/M8 (generate/verify MAC), A6/K8
(import/export key), KQ (verify ARQC / generate ARPC), NO (HSM status). Unknown
commands return error ``30``.
"""

from __future__ import annotations

import logging

from ..tcp.responder import TcpResponder
from . import commands
from .commands import HsmContext
from .lmk import TestLmk

log = logging.getLogger(__name__)


class PayShieldSimulator(TcpResponder):
    PROTOCOL = "payshield"

    def __init__(self, config: dict) -> None:
        config = dict(config)
        config.setdefault("protocol", self.PROTOCOL)
        # payShield framing default: 2-byte big-endian length prefix, ASCII.
        config.setdefault(
            "framing",
            {"length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii"},
        )
        super().__init__(config)
        # The host-port header width (the host chooses the header; we echo it).
        self.header_bytes = int(
            config.get("header_bytes", config.get("header_echo", {}).get("header_bytes", 4))
        )
        self.ctx = HsmContext(
            lmk=TestLmk(config.get("lmk"), variant_lmk=bool(config.get("variant_lmk", False))),
            firmware=config.get("firmware", commands.DEFAULT_FIRMWARE),
            overrides=dict(config.get("commands") or {}),
            disabled_commands={c.upper() for c in (config.get("disabled_commands") or [])},
            pci_hsm=bool(config.get("pci_hsm", False)),
        )

    # -- payShield wire: decode / dispatch / encode --------------------------

    def _decode(self, body: bytes) -> dict:
        text = body.decode(self.encoding, errors="replace")
        hb = self.header_bytes
        return {
            "header": text[:hb],
            "command": text[hb : hb + 2],
            "data": text[hb + 2 :],
            "raw": text,
        }

    def _resolve(self, parsed: dict) -> dict:
        cmd = parsed.get("command", "")
        error, data = commands.dispatch(self.ctx, cmd, parsed.get("data", ""))
        return {
            "command": commands.response_code(cmd),
            "error": error,
            "data": data,
        }

    def _response_code(self, action: dict) -> str:
        # drive the by-response-code metric off the payShield error field.
        return str(action.get("error", "00"))

    def _encode(self, parsed: dict, action: dict) -> bytes:
        header = parsed.get("header", "")
        command = action.get("command", parsed.get("command", ""))
        error = action.get("error", "00")
        data = action.get("data", "")
        return f"{header}{command}{error}{data}".encode(self.encoding)

    @property
    def commands_supported(self) -> list[str]:
        return commands.supported_commands()
