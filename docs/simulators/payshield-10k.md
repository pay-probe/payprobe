# payShield 10K HSM Simulator

PayProbe can stand in for a Thales payShield 10K Hardware Security Module, so
scenarios and load tests that depend on an HSM can run with no physical
appliance. The simulator listens for host commands on a TCP socket and answers
them with realistic responses, performing real payment cryptography under a
test Local Master Key (LMK).

It is built on the same responder machinery as the ISO 8583 host simulators, so
it plugs into the saved-simulator registry, the live-metrics dashboard, and the
**Simulators** page in the portal without any special handling.

## What it speaks

Each request is a length-prefixed frame carrying `header + command + data`; the
reply is `header + response-code + error-code + data`. The header is whatever
the host chose (it is echoed back for correlation), the command is a two-letter
code, and the response code is that command with its second character
incremented — `A0` → `A1`, `CW` → `CX`, `NC` → `ND`, and so on.

The following commands are implemented:

| Command | Response | Function | Crypto |
|---|---|---|---|
| `NC` | `ND` | Perform diagnostics (LMK check value + firmware) | real |
| `A0` | `A1` | Generate a key (optionally export under a ZMK/TMK) | real KCV |
| `BU` | `BV` | Generate a key check value | real |
| `CW` | `CX` | Generate a CVV / CVC | real |
| `CY` | `CZ` | Verify a CVV / CVC | real |
| `CA` | `CB` | Translate a PIN block from TPK to ZPK (ISO-0) | real |
| `EC` | `ED` | Verify an interchange PIN (Visa PVV method) | real |
| `M6` | `M7` | Generate a retail MAC (ISO 9797-1 alg. 3) | real |
| `M8` | `M9` | Verify a retail MAC | real |
| `CC` | `CD` | Translate a PIN block from one ZPK to another | real |
| `A6` | `A7` | Import a key encrypted under a ZMK | real KCV |
| `K8` | `K9` | Export a key under a KEK/ZMK | real KCV |
| `G0` | `G1` | Translate a PIN block from BDK (3DES DUKPT) to ZPK | real |
| `KQ` | `KR` | ARQC verification and/or ARPC generation | real |
| `NO` | `NP` | HSM status (buffer/ethernet/firmware; PCI-HSM flag) | n/a |

Any unrecognised command returns error code `30`; a command listed in
`disabled_commands` returns error `68`.

> EMV note: the real `KQ` packs several fields (PAN/PSN, ATC, UN, ARQC) as raw
> binary. The simulator accepts them as hex-ASCII so the link stays ASCII-clean;
> the cryptography (ICC master key derivation, session key, ARQC MAC, ARPC) is
> real. The bundled client adapter encodes the same way.

## Crypto fidelity

The simulator is deliberately *hybrid*. Key check values, CVV/CVC, PVV, ISO-0
PIN blocks and retail MACs are computed for real from the clear key that the
test LMK unwraps, so they verify across commands and against independent tools —
a key minted by `A0` checks out under `BU`, a CVV from `CW` verifies under `CY`,
and a PIN translated by `CA` decodes back to the same value.

Key *material* is random, and key tokens are wrapped under a single fixed test
LMK rather than the real per-key-type variant scheme. That means tokens
round-trip *within* the simulator but will **not** interoperate with a physical
payShield (different LMK). This is expected and appropriate for protocol and
integration testing. Do not use the simulator for anything requiring real key
security.

## Running it

### From the portal

On the **Simulators** page, open **New simulator**, choose the **payShield 10K**
preset, and start it. Live throughput, response-code and command breakdowns, and
recent traffic appear in the inline metrics just like any other simulator.

### Standalone

```bash
python -m worker.responder examples/simulators/payshield_10k.json
```

### Configuration

```json
{
  "protocol": "payshield",
  "host": "0.0.0.0",
  "port": 1500,
  "header_bytes": 4,
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "lmk": "0123456789ABCDEFFEDCBA9876543210",
  "firmware": "0007-E000",
  "commands": {}
}
```

`header_bytes` must match the message-header length configured on the host port
you are emulating (the payShield default in the manuals is 4). `lmk` overrides
the test LMK, `firmware` sets the string `NC` reports, and `commands` lets you
script fixed replies for specific commands — handy for forcing an error path:

```json
"commands": { "NC": { "error": "68", "data": "" } }
```

This makes `NC` return error `68` (command disabled), overriding the real
handler.

Additional options:

- `variant_lmk` (bool) — when true, keys are wrapped under a per-key-type
  variant of the LMK, so the simulator enforces *key separation*: a key minted
  as a CVK won't check out when a command treats it as a ZPK. Off by default
  (all key types share one variant, which is simpler for protocol testing).
- `disabled_commands` (list) — command codes that should return error `68`, to
  exercise host-side handling of disabled commands.
- `pci_hsm` (bool) — the PCI-HSM compliance flag the `NO` (HSM status) command
  reports.

## Driving it from a scenario

The package ships a host-command **client** adapter, `HSMAdapter`, registered as
`payshield` (and `hsm_client`). A scenario step names a high-level action and
passes parameters; the adapter builds the command string, sends it, and parses
the reply into `response_payload` with `response_code`, `error_code`, `ok`, the
raw `data`, and an action-specific field (e.g. `cvv`, `key`, `kcv`, `pin_block`,
`arpc`).

Actions: `nc`, `hsm_status`, `generate_key`, `key_check`, `generate_cvv`,
`verify_cvv`, `translate_pin` (TPK→ZPK), `translate_pin_zpk_zpk`, `verify_pin`
(PVV), `dukpt_translate_pin`, `generate_mac`, `verify_mac`, `import_key`,
`export_key`, `verify_arqc`. An unknown action falls back to a raw passthrough
(`command` + `data` in the payload).

### Correlation, pipelining and pooling

The payShield **message header** is a host-chosen correlation id the HSM echoes
back verbatim. The client tags every command with an incrementing header and a
single background reader demultiplexes replies to the right caller by that
header — so many commands can be **in flight at once over one reused
connection** without waiting for each reply in turn. Sequential `await`s behave
exactly as before; concurrent `execute` calls overlap.

For throughput, set `connections` (alias `pool_size`) on the adapter config to
open several sockets; commands round-robin across them, and each socket also
pipelines:

```json
"adapters": {
  "hsm": { "adapter": "payshield", "host": "10.0.2.10", "port": 1500, "connections": 8 }
}
```

If a connection drops, any in-flight command resolves as a failure (never a
hang), and the next command reconnects.

Point an environment's `hsm` target at the adapter:

```json
"adapters": {
  "hsm": { "adapter": "payshield", "host": "127.0.0.1", "port": 1500, "header": "HDR1" }
}
```

Then a step like:

```json
{ "id": "gen_cvv", "target": "hsm", "action": "generate_cvv",
  "payload": { "cvk": "${gen_cvk.response.key}", "pan": "4000000000000002",
               "expiry": "2512", "service_code": "000" },
  "assertions": [ { "field": "cvv", "operator": "present" } ] }
```

See `examples/scenarios/payshield_hsm_smoke.json` (with
`examples/environments/payshield-sim.json`) for a complete NC → generate CVK →
generate CVV → verify CVV run, and the "payShield ·" entries in the starter-flow
palette.

## From the MCP server

The PayProbe MCP server exposes three payShield tools (group **payShield HSM**)
so an agent can drive the simulator without the REST details:

- `start_payshield_simulator(label, port, header_bytes, lmk, firmware,
  variant_lmk, disabled_commands, pci_hsm)` — start a simulator with the
  payShield preset; returns its `id`.
- `hsm_command(host, port, action, params, command, data, header)` — send one
  host command and get the parsed reply. `action` is a high-level verb
  (`generate_cvv`, `verify_pin`, `nc`, …) with fields in `params`, or `"raw"`
  with `command` + `data` for a literal command. Backed by the orchestrator
  endpoint `POST /hsm/command`, which uses the client adapter.
- `run_hsm_example(environment_name, label)` — run the bundled NC → CVK → CVV →
  verify smoke scenario; returns `{run_id, status}`.

A typical agent flow: `start_payshield_simulator` → `hsm_command` calls (or
`run_hsm_example`) → `stop_simulator`.

## How it fits together

The simulator lives in `worker/adapters/hsm/`:

- `lmk.py` — the test LMK, the key-token wrap/unwrap codec, and the optional
  per-key-type variant scheme.
- `commands.py` — the per-command handlers and the command registry.
- `payshield.py` — `PayShieldSimulator`, a `TcpResponder` subclass that decodes
  the host-command wire, dispatches to a handler, and encodes the reply.
- `adapter.py` — `HSMAdapter`, the host-command client used by scenarios.

The orchestrator selects `PayShieldSimulator` whenever a saved simulator's
config has `protocol: "payshield"`; everything else (lifecycle, metrics,
auto-start, portal UI) is shared with the ISO 8583 responders.

## Reference

The wire formats follow the Thales *payShield 10K Core Host Commands* and *Host
Command Examples* manuals. Consult those for the full field layouts of each
command.
