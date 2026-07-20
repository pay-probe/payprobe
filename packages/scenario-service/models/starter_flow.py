"""Starter Flows — one-click, pre-wired chains the editor inserts on the canvas.

A starter flow is an ordered list of catalog steps (``target`` + ``action``)
with optional config / input overrides that may reference earlier steps in the
same flow via ``${ref.…}`` (rewritten to real node ids on insert). Built-in
flows ship in code (``BUILTIN_FLOWS``); user-created or -overridden flows are
persisted by ``api.flow_store.StarterFlowStore`` — same builtin-plus-custom
pattern as the Message Format registry.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FlowStep(BaseModel):
    ref: str
    target: str
    action: str
    #: overrides for top-level node config (crypto/http/init fields)
    config: dict[str, Any] = Field(default_factory=dict)
    #: overrides for a code node's named Input rows (e.g. an ISO `message`)
    inputs: dict[str, str] = Field(default_factory=dict)


class StarterFlowDraft(BaseModel):
    label: str
    description: str = ""
    steps: list[FlowStep] = Field(default_factory=list)


class StarterFlow(StarterFlowDraft):
    id: str
    builtin: bool = False
    updated_at: str | None = None


BUILTIN_FLOWS: list[StarterFlow] = [
    StarterFlow(
        id="emv_arqc_arpc",
        builtin=True,
        label="EMV cryptogram (ARQC → ARPC)",
        description=(
            "Issuer flow: build CDOL data, derive ICC master key + AC session "
            "key, generate the ARQC, then generate and verify the ARPC."
        ),
        steps=[
            FlowStep(ref="dol", target="emv_tools", action="dol_build"),
            FlowStep(ref="udk", target="emv_crypto", action="derive_icc_mk",
                     config={"key": "0123456789ABCDEFFEDCBA9876543210",
                             "pan": "4111111111111111", "psn": "00"}),
            FlowStep(ref="sk", target="emv_crypto", action="derive_session_key",
                     config={"key": "${udk.response.udk}", "atc": "001C"}),
            FlowStep(ref="arqc", target="emv_crypto", action="generate_arqc",
                     config={"key": "${sk.response.session_key}",
                             "data": "${dol.response.data}"}),
            FlowStep(ref="arpc", target="emv_crypto", action="generate_arpc",
                     config={"key": "${sk.response.session_key}",
                             "arqc": "${arqc.response.arqc}",
                             "arc": "3030", "arpc_method": "1"}),
            FlowStep(ref="vfy", target="emv_crypto", action="verify_arpc",
                     config={"key": "${sk.response.session_key}",
                             "arqc": "${arqc.response.arqc}", "arc": "3030",
                             "arpc_method": "1",
                             "expected": "${arpc.response.arpc}"}),
        ],
    ),
    StarterFlow(
        id="iso8583_roundtrip",
        builtin=True,
        label="ISO 8583 purchase (0200 → parse)",
        description=(
            "Pack a 0200 financial request from field values, then parse the "
            "wire message back — both using the selected Message Format."
        ),
        steps=[
            FlowStep(ref="pack", target="iso_messaging", action="iso8583_pack"),
            FlowStep(ref="parse", target="iso_messaging", action="iso8583_parse",
                     inputs={"message": "${pack.response.message}"}),
        ],
    ),
    StarterFlow(
        id="pin_block_roundtrip",
        builtin=True,
        label="PIN block (encode → decode)",
        description=(
            "Encode an ISO format-0 PIN block under a key, then decode it back "
            "to the clear PIN with the same key and PAN."
        ),
        steps=[
            FlowStep(ref="enc", target="emv_crypto", action="pin_block",
                     config={"key": "0123456789ABCDEFFEDCBA9876543210",
                             "pin": "1234", "pan": "4111111111111111"}),
            FlowStep(ref="dec", target="emv_crypto", action="pin_block_decode",
                     config={"key": "0123456789ABCDEFFEDCBA9876543210",
                             "pin_block": "${enc.response.pin_block}",
                             "pan": "4111111111111111"}),
        ],
    ),
]
