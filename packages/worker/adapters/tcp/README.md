# TCP Adapter (`TcpAdapter`)

A **universal, persistent TCP adapter** for request/response hosts. Built for
the long-running "exchange" use-case: one TCP connection is opened at
`connect()` and reused for the whole run. Many requests can be in flight at
once over the single socket — a background reader correlates each response back
to its request using a key supplied by the active **protocol**.

The transport is **protocol-agnostic**. It owns the socket, reader loop,
correlation, framing, sign-on, keep-alive and timeouts; what goes on the wire
is a pluggable strategy (`tcp/protocols.py`). The same class therefore backs
very different hosts — only `protocol` and a few protocol-specific keys change:

| `protocol` | For | Correlation |
|---|---|---|
| `iso8583` (default) | Switch, acquirer, issuer simulator | STAN / DE 11 (configurable) |
| `header_echo` | HSM (Thales payShield, …) and other host-command links | Leading header echoed by the host |

Registry keys backed by this adapter: `tcp`, `tcp_iso8583`, `switch`,
`switch_iso8583`, `acquirer`, `acquirer_host`, `hsm`, `hsm_tcp`. (In mock mode,
or with a per-adapter `"mock": true`, the registry routes these to the mock
adapter instead.)

## How it works

- **Persistent socket** opened once, reused for every step.
- **Background reader** frames inbound bytes, hands the body to the protocol to
  parse, reads the correlation key, and resolves the matching pending request.
  Unsolicited / unmatched messages go to an internal events list.
- **Protocol** builds the outbound body + correlation key and parses replies.
- **Sign-on** on connect and **keep-alive** on an interval, both optional;
  default to the protocol's health probe (ISO 8583 ⇒ 0800 echo; header_echo ⇒
  the configured diagnostic command).
- `health_check()` runs the protocol's health probe and never raises.

## Config

```jsonc
{
  "host": "10.0.1.50",          // required
  "port": 7000,                  // required
  "protocol": "iso8583",         // "iso8583" | "header_echo"

  "framing": {                   // applies to every protocol
    "length_prefix_bytes": 2,    // width of the length prefix (>= 1)
    "length_byte_order": "big",  // "big" | "little"
    "length_includes_prefix": false, // does the length count its own prefix bytes?
    "length_includes_header": true,  // does the length count the TPDU header?
    "tpdu_bytes": 0,             // inbound TPDU header width to strip
    "tpdu_outbound_hex": "",     // static TPDU prepended on send (hex)
    "encoding": "ascii"          // default body text encoding
  },

  "sign_on":   { "enabled": true, "action": "...", "payload": { } },
  "keepalive": { "enabled": true, "interval_sec": 30, "action": "...", "payload": { } },
  "response_timeout_sec": 30,
  "connect_timeout_sec": 10
}
```

All `framing` keys are optional; defaults match the most common deployment
(2-byte big-endian length prefix, no TPDU). `sign_on`/`keepalive` `action` and
`payload` are optional — omit them to use the protocol's default probe.

### `iso8583` protocol

```jsonc
{
  "protocol": "iso8583",
  "correlation": {
    "field": "11",               // DE used to match response <-> request
    "auto_generate": true,       // assign the field automatically if absent
    "match_mti": false,          // also require the response MTI to match
    "response_mti_map": { "0200": "0210", "0400": "0410", "0800": "0810" }
  },
  "fields": { "<de>": { "name": "...", "len_type": "fixed|llvar|lllvar", "length": 0 } },
  "field_map": { "amount": "4", "pan": "2" },  // convenience key -> DE
  "auto_fields": true            // stamp DE 7/12/13 time fields when in the table
}
```

Actions: `send_message` (universal — `mti` + `values`), `send_0100/0200/0220`,
`send_0400/0420`, `send_0800`. Convenience payload keys (`amount`, `pan`,
`pos_entry_mode`, `currency`, `rrn`, …) are mapped to DEs and width-formatted;
an explicit `values` map always wins. Response exposes `mti`, `response_code`
(DE 39), `stan` (DE 11), `rrn` (DE 37), `auth_code` (DE 38), and `fields` (DE →
value).

### `header_echo` protocol (HSM and host-command links)

Each message starts with a fixed-width header the sender chooses; the host
returns it verbatim, and that header is the correlation key.

```jsonc
{
  "protocol": "header_echo",
  "header_echo": {
    "header_bytes": 4,            // width of the echoed header
    "auto_header": true,          // auto-assign a sequential header
    "response_command_bytes": 2,  // width of the reply command code
    "error_field_bytes": 2,       // width of the reply return/error code
    "ok_codes": ["00"],           // error codes considered healthy
    "command_map": { "<action>": "<command>" },
    "diagnostic_command": "NC",   // health-check / sign-on command
    "encoding": "ascii"
  }
}
```

Actions: `send_command` (`command` + `data`, optional `header`) and
`diagnostics`. Response exposes `header`, `command`, `error_code` /
`response_code`, and `data`.

> **Transport success vs business result.** `success=True` means a correlated
> reply arrived in time. A declined ISO response code (DE 39 = `05`) or a
> non-zero HSM error code is **not** a transport failure — assert on
> `response_code` to gate pass/fail.

## Several instances / different connections

An environment can declare **any number of named instances** under `adapters` —
two switches, a primary and backup HSM, etc. Each instance gets its own
connection, config and connection-budget entry. The instance name (the step's
`target`) is decoupled from the implementation via the `adapter` key (alias
`type`):

```jsonc
{
  "mode": "real",
  "adapter_defaults": {            // underlays every instance of an impl
    "tcp": { "framing": { "length_prefix_bytes": 2 }, "response_timeout_sec": 20,
             "keepalive": { "enabled": true, "interval_sec": 30 } }
  },
  "connection_budget": { "adapters": { "switch_visa": 200, "switch_mc": 200 } },
  "adapters": {
    "switch_visa": { "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.1.50", "port": 7001,
                     "correlation": { "match_mti": true } },
    "switch_mc":   { "adapter": "tcp", "protocol": "iso8583",
                     "host": "10.0.1.50", "port": 7002 },
    "hsm_primary": { "adapter": "tcp", "protocol": "header_echo",
                     "host": "10.0.2.10", "port": 1500,
                     "header_echo": { "header_bytes": 4 } },
    "hsm_backup":  { "extends": "hsm_primary", "host": "10.0.2.11" }
  }
}
```

Config is shared two ways, both deep-merged with the instance taking precedence:

- **`adapter_defaults`** — keyed by implementation name (`tcp`), merged under
  every instance of that impl. Good for framing / timeouts / keepalive common
  to all your TCP links.
- **`extends`** — an instance inherits another instance's full config, then
  overrides a few keys (here `hsm_backup` reuses `hsm_primary` and only changes
  the host). Circular chains are rejected.

Per-instance connection limits come from `connection_budget.adapters[<name>]`,
falling back to a `pool_size` config key, then 100.

Reference an instance from a scenario step by setting the step's `target` to the
instance name (e.g. `switch_visa`). In the editor, expose instances as custom
catalog targets so they appear in the palette. A full sample lives at
`examples/environments/multi-instance.json`.

## Extending

Add a protocol by subclassing `TcpProtocol` in `protocols.py` (implement
`encode` / `decode` / `correlation_key` / `shape_response`, optionally
`health_probe` / `is_healthy`) and registering it in `make_protocol`.
