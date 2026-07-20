"""Built-in "EMV Crypto (HSM)" palette group — the issuer/acquirer cryptogram flow.

These steps are backed by the ``crypto`` node kind (real DES/3DES in the worker,
not sandboxed code), so dropping one materialises a pre-configured crypto node.
Chain them to model the full application-cryptogram path an issuer host runs:

    Derive ICC Master Key (UDK)  →  Derive AC Session Key  →
    Generate / Verify ARQC       →  Generate / Verify ARPC

Each step ships with sensible defaults; wire the key/data fields to earlier
steps with ``${step_id.response.field}`` (e.g. the session key into ARQC).
"""
from __future__ import annotations

from .catalog import ActionSpec, TargetSpec


def _crypto_action(name, label, operation, response_fields, defaults=None) -> ActionSpec:
    template = {"operation": operation}
    template.update(defaults or {})
    return ActionSpec(
        name=name, label=label, payload_hint={}, response_fields=response_fields,
        behavior={"kind": "crypto", "template": template},
    )


EMV_CRYPTO_TARGET = TargetSpec(
    target="emv_crypto",
    label="EMV Crypto (HSM)",
    category="EMV Crypto (HSM)",
    color="#d2a8ff",
    icon="key",
    description="EMV cryptogram flow — derive ICC/session keys, generate and "
                "verify ARQC/ARPC, and encode/decode PIN blocks.",
    custom=False,
    actions=[
        _crypto_action(
            "derive_icc_mk", "Derive ICC Master Key (UDK)", "emv_icc_mk",
            ["udk"], {"key": "", "pan": "", "psn": "00"},
        ),
        _crypto_action(
            "derive_session_key", "Derive AC Session Key", "emv_session_key",
            ["session_key"], {"key": "", "atc": ""},
        ),
        _crypto_action(
            "generate_arqc", "Generate ARQC", "arqc",
            ["arqc"], {"key": "", "data": ""},
        ),
        _crypto_action(
            "verify_arqc", "Verify ARQC", "arqc",
            ["arqc", "match", "expected"], {"key": "", "data": "", "expected": ""},
        ),
        _crypto_action(
            "generate_arpc", "Generate ARPC", "arpc",
            ["arpc", "method"], {"key": "", "arqc": "", "arc": "3030", "arpc_method": "1"},
        ),
        _crypto_action(
            "verify_arpc", "Verify ARPC", "arpc",
            ["arpc", "method", "match", "expected"],
            {"key": "", "arqc": "", "arc": "3030", "arpc_method": "1", "expected": ""},
        ),
        # HSM staples that round out the pack.
        _crypto_action(
            "pin_block", "PIN Block — encode (ISO-0)", "pin_block_encode",
            ["pin_block", "clear_block"], {"key": "", "pin": "", "pan": ""},
        ),
        _crypto_action(
            "pin_block_decode", "PIN Block — decode (ISO-0)", "pin_block_decode",
            ["pin", "clear_block"], {"key": "", "pin_block": "", "pan": ""},
        ),
        _crypto_action(
            "key_check_value", "Key Check Value (KCV)", "kcv",
            ["kcv", "full"], {"key": ""},
        ),
    ],
)
