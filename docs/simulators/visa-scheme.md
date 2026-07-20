# VISA Scheme Simulator (Base I-style)

PayProbe can stand in for the VISA scheme network (VisaNet), so scenarios and
load tests that authorize, reverse or clear against VISA can run with no real
scheme connection. The simulator listens on a TCP socket, speaks ISO 8583, and
answers each message with VISA's response-code conventions and stand-in (STIP)
behaviour — optionally performing real card cryptography (CVV2 / PVV / ARQC).

It is built on the same responder machinery as the other host simulators
(`TcpResponder`), so it plugs into the saved-simulator registry, the live-metrics
dashboard, and the **Simulators** page (preset **VISA scheme**) without any
special handling.

## Fidelity — read this first

This is a **functional Base I** simulator built from the *public* ISO 8583:1987
message structure that VISA Base I derives from, plus the card cryptography
already in PayProbe's HSM toolkit.

It is **not** a byte-exact reproduction of VISA's confidential *V.I.P. System
Field-Level* / *Base I Technical* / *SMS (Base II) Clearing* specifications. The
exact internal layout of VISA private fields (e.g. the sub-field structure of
DE 62 Custom Payment Service and DE 63 Network Data) and the BASE I/II edit
tables are member-only documents. Until those are supplied, DE 62/63 are treated
as opaque echo fields.

When you have the relevant spec sections, the field tables can be encoded
exactly as a follow-on phase (the "spec-exact" path); everything else here is
built from public knowledge and PayProbe's existing code.

## Message flows

VISA Base I is ISO 8583, so the wire and framing are unchanged from the generic
ISO responder. Only the *reply decision* is VISA-specific.

| Request | Response | Flow | Default behaviour |
|---|---|---|---|
| `0100` | `0110` | Authorization request | Stand-in approve, or decline per rules |
| `0200` | `0210` | Financial (auth + capture) request | Stand-in approve, or decline per rules |
| `0400` | `0410` | Reversal request | Acknowledged (approved) |
| `0420` | `0430` | Reversal advice | Acknowledged (approved) |
| `0800` | `0810` | Network management (sign-on/off, echo) | Approved, DE 70 echoed |
| `0220` | `0230` | Capture / advice | Acknowledged |
| `0520` | `0530` | Clearing advice | Acknowledged (functional only) |

Full Base II / SMS batch *record* formats are out of scope; clearing advices are
acknowledged at the message level.

## Authorization decision

For `0100` / `0200`, the simulator declines in this order (first match wins),
otherwise approves under stand-in:

1. **Amount limit** — DE 4 ≥ `decline_over` → DE 39 `61` (exceeds amount limit)
2. **Blocked BIN** — DE 2 starts with any `block_bins` prefix → `block_response` (default `05`)
3. **Expired card** — `expiry_check` on and DE 14 (YYMM) in the past → `54`
4. **CVV2** — `verify_cvv2` present and the presented value mismatches → its `decline` (default `N7`)
5. **PVV** — `verify_pvv` present and the PIN/PVV mismatches → its `decline` (default `55`)
6. **ARQC** — `verify_arqc` present and the cryptogram mismatches → its `decline` (default `05`)

If `stand_in` is `false` and no explicit rule pins a reply, the simulator answers
`91` (issuer or switch inoperative) instead of approving.

Explicit `rules` (same `when`/`respond` shape as the generic responder) are
evaluated **first** and win, so you can pin specific cards/BINs to specific
response codes and let everything else follow scheme behaviour.

## Configuration

```json
{
  "protocol": "visa",
  "host": "0.0.0.0",
  "port": 7010,
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "visa": {
    "stand_in": true,
    "approve_code": "00",
    "decline_over": "000000100000",
    "block_bins": ["400000"],
    "block_response": "05",
    "expiry_check": true,

    "verify_cvv2": { "field": "48", "cvk": "<32H CVK>", "service_code": "000", "decline": "N7" },
    "verify_pvv":  { "pin_field": "52", "pvv_field": "44", "pvk": "<32H PVK>", "pvki": "1", "decline": "55" },
    "verify_arqc": { "field": "55", "session_key": "<32H SK>", "data": "<txn hex>", "decline": "05" }
  },
  "rules": [],
  "chaos": {}
}
```

The crypto blocks are **opt-in**: a check only runs when its block and keys are
present (and the relevant DEs are populated on the request). Missing data skips
the check rather than failing the authorization, so the default config needs no
keys.

Standard responder extras still apply: `chaos` for fault injection (latency,
drops, malformed/partial frames) and live metrics on the dashboard.

## Running it

From the portal: **Simulators → New → VISA scheme** preset, then **Save & start**.

From the CLI:

```bash
python -m worker.responder examples/simulators/visa_baseI.json
```

The bundled `examples/simulators/visa_baseI.json` stands up a stand-in acquirer
on port 7010 that approves most auths, declines amounts ≥ 1,000.00, blocks BIN
`400000`, rejects expired cards, and refers BIN `411111` to the issuer (`01`).
