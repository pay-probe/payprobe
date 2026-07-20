"""Playground samples (ADR-0007 follow-up) — ready-made example interactions.

One sample set per *element family* the Playground can address. The family key
is the target's ``protocol`` when it has one (``iso8583``, ``visa``,
``header_echo``, ``payshield``, ``cybersource``, ``nats``) falling back to its
``adapter`` (``http``, ``grpc``, ``tcp``→``iso8583``) — the same fact every
target row in ``GET /playground/targets`` already carries, so a client picks
``samples.wire[protocol or adapter]`` for the selected target. Function
(crypto) samples are keyed by operation name and cover **every** entry in
``worker.engine.crypto_tools.OPERATIONS``.

Samples are *templates for the composer*, not fixtures: they use classic test
values (``4000001234567890``, the ``0123…3210`` TDES test key, the ANSI X9.24
test KSN) and are safe to fire at the bundled simulators. Dependent values
(``pin_block``, ``offset``) were computed with the platform's own
``run_crypto`` so each function sample executes green as-is — the unit tests
enforce that. Keep this module pure (data + one merge function): it is
unit-tested standalone, like ``playground.py``.
"""
from __future__ import annotations

#: classic test card / key material used across the samples (same PAN the
#: playground tests use; TDES/AES test keys; ANSI X9.24 test KSN).
_PAN = "4000001234567890"
_TDES_KEY = "0123456789ABCDEFFEDCBA9876543210"
_AES_KEY = "00112233445566778899AABBCCDDEEFF"
_KSN = "FFFF9876543210E00008"

#: wire samples per element family — ``{name, action, payload, notes}``.
WIRE_SAMPLES: dict[str, list[dict]] = {
    "iso8583": [
        {"name": "Authorization (0100)",
         "action": "send_0100",
         "payload": {"pan": _PAN, "processing_code": "000000",
                     "amount": 10000, "currency": "981",
                     "terminal_id": "TERM0001"},
         "notes": "Friendly keys map to DEs (pan→2, amount→4, …); STAN/DE 11 "
                  "and time fields are stamped automatically. Pin a dialect "
                  "with message_format_id."},
        {"name": "Purchase (0200)",
         "action": "send_0200",
         "payload": {"pan": _PAN, "processing_code": "000000",
                     "amount": 10000, "currency": "981"},
         "notes": "Financial request; the bundled host sim answers DE 39=00."},
        {"name": "Reversal (0420)",
         "action": "send_0420",
         "payload": {"pan": _PAN, "amount": 10000,
                     "rrn": "<RRN from the original response>"},
         "notes": "Replace rrn with DE 37 from the transaction to reverse."},
        {"name": "Network echo (0800)",
         "action": "send_0800",
         "payload": {},
         "notes": "Sign-on/echo heartbeat; expects an 0810 back."},
    ],
    "visa": [
        {"name": "VISA auth (0100)",
         "action": "send_0100",
         "payload": {"pan": _PAN, "processing_code": "000000",
                     "amount": 10000, "currency": "840"},
         "notes": "Base I authorization. The simulator's opt-in CVV2/ARQC "
                  "checks read DE 48/55 when configured."},
        {"name": "Network management (0800)",
         "action": "send_0800",
         "payload": {},
         "notes": "Sign-on; the sim answers 0810 DE 39=00."},
    ],
    "header_echo": [
        {"name": "Echo test",
         "action": "send",
         "payload": {"data": "PING"},
         "notes": "Header-framed echo; the header correlates automatically. "
                  "Use {\"message\": …} to send a literal tail instead."},
    ],
    "payshield": [
        {"name": "Diagnostics (NC)",
         "action": "nc",
         "payload": {},
         "notes": "LMK check + firmware; error_code 00 = healthy."},
        {"name": "Generate CVK (A0)",
         "action": "generate_key",
         "payload": {"key_type": "402", "scheme": "U"},
         "notes": "Returns a key token + KCV — feed the token to "
                  "generate_cvv as cvk."},
        {"name": "Generate CVV (CW)",
         "action": "generate_cvv",
         "payload": {"cvk": "<key token from generate_key>", "pan": _PAN,
                     "expiry": "2512", "service_code": "201"},
         "notes": "The bundled smoke scenario chains exactly this."},
        {"name": "Raw command",
         "action": "raw",
         "payload": {"command": "NC", "data": ""},
         "notes": "Escape hatch: 2-char command + data tail, sent verbatim."},
    ],
    "http": [
        {"name": "Health probe (GET)",
         "action": "/health",
         "payload": {},
         "notes": "An action containing '/' is used as the path; no body ⇒ "
                  "GET. Declared actions on the connection override this."},
        {"name": "JSON POST",
         "action": "/api/payments",
         "payload": {"amount": 10000, "currency": "GEL", "pan": _PAN},
         "notes": "A payload makes it a POST with a JSON body."},
    ],
    "cybersource": [
        {"name": "Card authorization",
         "action": "/pts/v2/payments",
         "payload": {
             "clientReferenceInformation": {"code": "PLAYGROUND-1"},
             "orderInformation": {"amountDetails": {"totalAmount": "100.00",
                                                    "currency": "USD"}},
             "paymentInformation": {"card": {
                 "number": _PAN, "expirationMonth": "12",
                 "expirationYear": "2031", "securityCode": "123"}},
         },
         "notes": "The sim's decision ladder declines configured block_bins "
                  "prefixes, cvn_decline_values and amounts over decline_over. "
                  "Its auth gate accepts any bearer credential — poking a "
                  "running sim by reference passes one automatically; real "
                  "gateways need connection-configured auth."},
        {"name": "Capture",
         "action": "/pts/v2/payments/<id>/captures",
         "payload": {"orderInformation": {"amountDetails": {
             "totalAmount": "100.00", "currency": "USD"}}},
         "notes": "Replace <id> with the authorization's id."},
    ],
    "nats": [
        {"name": "Publish event",
         "action": "publish",
         "payload": {"subject": "pay.events",
                     "body": {"type": "auth.requested", "amount": 10000}},
         "notes": "Fire-and-forget; subject falls back to the connection's "
                  "configured subject. body is encoded with the connection's "
                  "codec (json default)."},
        {"name": "Request / reply",
         "action": "request",
         "payload": {"subject": "pay.auth", "timeout": 5,
                     "body": {"pan": _PAN, "amount": 10000}},
         "notes": "Waits for a responder on the subject (queue groups fan "
                  "out); timeout is seconds."},
        {"name": "JetStream publish",
         "action": "js_publish",
         "payload": {"subject": "pay.cleared",
                     "body": {"rrn": "000000123456"}},
         "notes": "Ack-checked persistent publish — the subject must be "
                  "bound to a stream."},
    ],
    "grpc": [
        {"name": "Discover services",
         "action": "$discover",
         "payload": {},
         "notes": "Reflection discovery — lists services/methods so you can "
                  "call one as Service/Method next."},
    ],
}

#: one runnable sample per crypto operation — parameter shapes match
#: ``OPERATIONS`` in ``worker/engine/crypto_tools.py``; dependent values
#: (pin_block, offset) computed with run_crypto so each fires green as-is.
CRYPTO_SAMPLES: dict[str, dict] = {
    "des": {"key": _TDES_KEY, "data": "0123456789ABCDEF0123456789ABCDEF",
            "cipher_op": "encrypt", "cipher_mode": "ecb"},
    "kcv": {"key": _TDES_KEY},
    "pin_block_encode": {"pin": "1234", "pan": _PAN, "key": _TDES_KEY,
                         "format": "0"},
    "pin_block_decode": {"pin_block": "7886F2179A0694B3", "pan": _PAN,
                         "key": _TDES_KEY, "format": "0"},
    "retail_mac": {"key": _TDES_KEY, "data": "0123456789ABCDEF"},
    "cvv": {"pan": _PAN, "expiry": "2512", "service_code": "201",
            "key": _TDES_KEY},
    "arqc": {"key": _TDES_KEY, "data": "0123456789ABCDEF0123456789ABCDEF"},
    "arpc": {"key": _TDES_KEY, "arqc": "583A832037420D10", "arc": "3030",
             "arpc_method": "1"},
    "emv_icc_mk": {"key": _TDES_KEY, "pan": _PAN, "psn": "00"},
    "emv_session_key": {"key": "D3DBE21D8AAD7EE9CD1FFE825C0136B4",
                        "atc": "0001"},
    "dukpt_ipek": {"bdk": _TDES_KEY, "ksn": _KSN},
    "dukpt_derive_key": {"bdk": _TDES_KEY, "ksn": _KSN, "variant": "pin"},
    "dukpt_pin_block": {"pin": "1234", "pan": _PAN, "bdk": _TDES_KEY,
                        "ksn": _KSN},
    "pvv": {"pan": _PAN, "pin": "1234", "pvki": "1", "key": _TDES_KEY},
    "ibm3624_pin": {"validation_data": _PAN, "key": _TDES_KEY, "pin_len": 4},
    "ibm3624_offset": {"validation_data": _PAN, "key": _TDES_KEY,
                       "pin": "1234"},
    "ibm3624_verify": {"validation_data": _PAN, "key": _TDES_KEY,
                       "offset": "4251", "pin": "1234"},
    "aes": {"key": _AES_KEY, "data": "00112233445566778899AABBCCDDEEFF",
            "cipher_op": "encrypt", "cipher_mode": "ecb"},
    "aes_cmac": {"key": _AES_KEY, "data": "0123456789ABCDEF"},
    "aes_dukpt_initial_key": {"bdk": _AES_KEY,
                              "initial_key_id": "1234567890123456"},
    "aes_dukpt_derive_key": {"bdk": _AES_KEY,
                             "ksn": "123456789012345600000001",
                             "variant": "pin"},
    "aes_dukpt_pin_block": {"pin": "1234", "pan": _PAN, "bdk": _AES_KEY,
                            "ksn": "123456789012345600000001"},
}


#: Non-``tcp`` adapters that fold onto a wire family (they don't name one
#: directly in ``WIRE_SAMPLES``). Mirrors the worker registry's adapter groups:
#: the universal TCP aliases speak ISO 8583; ``hsm``/``hsm_tcp`` are header-echo
#: *transport* links; ``hsm_client`` is the payShield host-command client;
#: ``rest_pay`` is an HTTP flavour.
_ADAPTER_ALIAS: dict[str, str] = {
    "tcp_iso8583": "iso8583",
    "switch": "iso8583",
    "switch_iso8583": "iso8583",
    "acquirer": "iso8583",
    "acquirer_host": "iso8583",
    "hsm": "header_echo",
    "hsm_tcp": "header_echo",
    "hsm_client": "payshield",
    "rest_pay": "http",
}


#: the ``ConnectionDraft`` protocol default. On a non-tcp adapter this value is
#: noise (the field is irrelevant for nats/http/grpc/…), so it must not win —
#: this is what stopped a NATS connection from showing ISO 8583 samples.
_DEFAULT_PROTOCOL = "iso8583"


def sample_family(protocol: str | None = None,
                  adapter: str | None = None) -> str | None:
    """Which ``samples.wire`` family a target draws samples from.

    The ``protocol`` is authoritative when it is *meaningful*: on the universal
    ``tcp`` adapter (or a bare-protocol call — a simulator/participant carries no
    adapter) any known protocol wins, so a ``tcp`` link speaking ``visa`` gets
    the VISA samples. On any *other* adapter the protocol only wins when it has
    been deliberately set to a non-default family (e.g. ``cybersource`` over
    ``http``) — a bare ``"iso8583"`` default is ignored so the adapter's own
    family (``nats``, ``http``, ``grpc``, ``payshield`` …) is used. ``None`` when
    nothing matches — the composer then offers no chips, which is honest."""
    a = (adapter or "").lower()
    p = (protocol or "").lower()
    tcp_like = a in ("", "tcp")
    # Protocol wins when meaningful: tcp/bare → any known protocol; other
    # adapters → only a deliberately non-default protocol.
    if (tcp_like or p != _DEFAULT_PROTOCOL):
        if p in WIRE_SAMPLES:
            return p
        if p in _ADAPTER_ALIAS:
            return _ADAPTER_ALIAS[p]
    # Otherwise the adapter names the family (folding shared shapes).
    if a in WIRE_SAMPLES:
        return a
    if a in _ADAPTER_ALIAS:
        return _ADAPTER_ALIAS[a]
    # A tcp adapter with no/unknown protocol still speaks ISO 8583.
    return "iso8583" if a == "tcp" else None


def samples_catalog() -> dict:
    """The ``samples`` block of ``GET /playground/targets``.

    ``wire`` is keyed by element family (match on the target's ``protocol``,
    falling back to ``adapter``); ``functions.crypto`` carries one runnable
    parameter set per operation, shaped like a wire sample so the composer
    treats both uniformly."""
    return {
        "wire": WIRE_SAMPLES,
        "functions": {"crypto": [
            {"name": op, "action": op, "payload": dict(params),
             "notes": None}
            for op, params in sorted(CRYPTO_SAMPLES.items())
        ]},
    }
