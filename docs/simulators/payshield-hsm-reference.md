# payShield 10K HSM — Configuration & Command Reference

A practical guide to configuring the bundled payShield 10K HSM simulator and the
host-command client, and to every command they support — what each one is *for*
in a real payments system, how to call it, and a worked example.

If you just want to get something running, jump to [Quick start](#quick-start).

---

## 1. The two sides

An HSM integration always has two halves, and PayProbe gives you both:

| Side | What it is | PayProbe piece |
|---|---|---|
| **HSM** (server) | Listens on a TCP port, answers host commands, holds keys under its LMK | `PayShieldSimulator` — a saved simulator with `protocol: "payshield"` |
| **Host** (client) | Your application — sends commands, reads replies | `HSMAdapter` — the `payshield` adapter, used as a scenario target |

You start the **simulator** once (it binds a port). Then **scenarios** connect to
it as a client and drive commands. The two never need a real appliance.

> The simulator performs **real** payment cryptography (CVV, PVV, PIN blocks,
> MAC, KCV, EMV) under a *test* LMK, so values verify across commands. Key
> material is random and key tokens only round-trip within the simulator — they
> won't interoperate with a physical payShield. Use it for protocol and
> integration testing, never for real key security.

---

## 2. Simulator configuration

A simulator is a JSON config. Every field with its meaning:

```jsonc
{
  "protocol": "payshield",        // selects the HSM simulator (required)
  "host": "0.0.0.0",              // bind address (0.0.0.0 = all interfaces)
  "port": 1500,                   // classic payShield host port
  "header_bytes": 4,              // width of the echoed message header (see §4)

  "framing": {                    // TCP framing — must match the host port
    "length_prefix_bytes": 2,     // 2-byte big-endian length prefix (standard)
    "length_byte_order": "big",
    "encoding": "ascii"
  },

  "lmk": "0123456789ABCDEFFEDCBA9876543210",  // double-length test LMK (32 hex)
  "firmware": "0007-E000",        // string the NC command reports

  // --- optional fidelity / behaviour switches ---
  "variant_lmk": false,           // true = enforce key separation (see §7)
  "pci_hsm": false,               // flag the NO (status) command reports
  "disabled_commands": [],        // e.g. ["CW"] → those commands return error 68
  "commands": {                   // scripted overrides for specific commands
    // "NC": { "error": "68", "data": "" }
  }
}
```

**Field notes:**

- **`header_bytes`** must match the *message header length* configured on the host
  port you're emulating. payShield's manuals use 4; if your host sends a
  different width, set it here or command parsing will shift.
- **`lmk`** only needs to be internally consistent — it wraps/unwraps the
  simulator's own key tokens. KCVs and all payment crypto come from the *clear*
  key, so they're correct regardless of this value.
- **`disabled_commands`** lets you exercise your host's handling of a disabled
  command (error `68`) without reconfiguring anything else.
- **`commands`** forces a fixed reply for a command — handy to script an error
  path: `"CY": { "error": "01" }` makes every CVV verification fail.

### Starting it

```bash
# Standalone (CLI)
python -m worker.responder examples/simulators/payshield_10k.json
```

```text
# Portal: Simulators → New simulator → payShield 10K preset → enable → Save → Start
```

```python
# MCP (my-server) — persists + starts in one call, shows on the Simulators page
start_payshield_simulator(label="payShield 10K", port=1500)
```

---

## 3. Client / environment configuration

Scenarios reach the HSM through an **environment** that maps the `hsm` target to
the `payshield` adapter:

```jsonc
{
  "name": "payshield-sim",
  "adapters": {
    "hsm": {
      "adapter": "payshield",     // the HSMAdapter client
      "host": "127.0.0.1",
      "port": 1500,
      "header_bytes": 4,
      "connections": 1,           // sockets to open; >1 for load (see §4)
      "length_prefix_bytes": 2,   // framing (match the simulator)
      "length_byte_order": "big",
      "encoding": "ascii",
      "response_timeout_sec": 5
    }
  }
}
```

A scenario step then targets `hsm` and names an **action**:

```jsonc
{
  "id": "make_cvv",
  "target": "hsm",
  "action": "generate_cvv",
  "payload": { "cvk": "${gen_cvk.response.key}", "pan": "4000000000000002",
               "expiry": "2512", "service_code": "000" },
  "assertions": [ { "field": "cvv", "operator": "present" } ]
}
```

Every reply lands in `response_payload` with these fields you can assert on:

| Field | Meaning |
|---|---|
| `response_code` | 2-char reply code (e.g. `A1`, `CX`) |
| `error_code` | `00` = success; anything else is an error (see §6) |
| `ok` | boolean shortcut for `error_code == "00"` |
| `data` | the raw response data tail |
| *action-specific* | `cvv`, `key`, `kcv`, `pin_block`, `firmware`, `arpc`, … |

---

## 4. Wire format & correlation

Each message on the wire is:

```
[2-byte length][header / correlation id][2-char command][command data]
```

The **header** is a host-chosen value the HSM echoes back unchanged — it's how
the host matches a reply to its request. The client tags each command with an
**incrementing** header, so it can keep **many commands in flight on one
connection** and demultiplex replies by header. Set `connections` > 1 to open a
pool of sockets that commands round-robin across — the throughput path for load
tests.

The **response code** is the command with its second character incremented:
`A0`→`A1`, `CW`→`CX`, `NC`→`ND`, `M6`→`M7`.

---

## 5. Supported commands

Grouped by what they're used for. Each shows the command/response codes, the
client **action** name, its parameters, and a practical example.

### Diagnostics & status

#### `NC` → `ND` — Perform diagnostics  ·  action `nc`
The sign-on / health check. Returns the LMK check value and firmware number —
hosts send it on connect to confirm the HSM is alive and using the expected LMK.

- **Params:** none
- **Returns:** `lmk_check` (16 hex), `firmware`
- **Example:** `hsm_command(action="nc")` → `ND 00 08D7B4FB629D0885 0007-E000`

#### `NO` → `NP` — HSM status  ·  action `hsm_status`
Returns operational status. Mode `01` reports PCI-HSM compliance.

- **Params:** `mode` (`"00"` status, `"01"` PCI-HSM flag)
- **Example:** `hsm_command(action="hsm_status", params={"mode":"01"})`

### Key management

#### `A0` → `A1` — Generate a key  ·  action `generate_key`
Mint a new working key under the LMK. This is how you provision the keys every
other command needs — a CVK before generating CVVs, a ZPK before translating
PINs, a MAC key before MACing, and so on. Returns the key as a token plus its
check value (KCV) so two parties can confirm they hold the same key.

- **Params:** `key_type` (3-digit, default `002`), `scheme` (`U` double-length,
  `T` triple, `Z` single), `mode` (`0`)
- **Returns:** `key` (token), `kcv`
- **Common key types:** see §8
- **Example:** `generate_key {"key_type":"402","scheme":"U"}` → a CVK token + KCV

#### `BU` → `BV` — Generate a key check value  ·  action `key_check`
Compute the KCV of a key already held under the LMK. Used to verify a key was
loaded/exchanged correctly (both ends compare KCVs).

- **Params:** `key_type_code` (2-digit), `key` (token), `length_flag` (`1`)
- **Returns:** `kcv`
- **Example:** `key_check {"key_type_code":"42","key":"${gen_cvk.response.key}"}`

#### `A6` → `A7` — Import a key under a ZMK  ·  action `import_key`
Bring a key in from another party: it arrives encrypted under a shared Zone
Master Key (ZMK), and the HSM re-wraps it under its own LMK. The standard way to
receive a ZPK/CVK from an acquirer or scheme during key exchange.

- **Params:** `key_type` (`001`), `zmk` (token), `key` (key encrypted under ZMK),
  `scheme` (`U`)
- **Returns:** `key` (LMK token), `kcv`

#### `K8` → `K9` — Export a key under a KEK  ·  action `export_key`
The inverse of import: take a key held under the LMK and re-encrypt it under a
shared KEK/ZMK so it can be sent to another party.

- **Params:** `key_type` (`402`), `key` (LMK token), `kek` (token), `scheme` (`X`)
- **Returns:** the key encrypted under the KEK + `kcv`

### Card verification (CVV / CVC)

#### `CW` → `CX` — Generate a CVV/CVC  ·  action `generate_cvv`
Produce the card security value from the PAN, expiry and service code under a
Card Verification Key (CVK). This is the value encoded on the magstripe (CVV1),
printed on the signature panel (CVV2, service code `000`), or put on the chip
(iCVV, service code `999`).

- **Params:** `cvk` (token), `pan`, `expiry` (`YYMM`), `service_code` (`000`)
- **Returns:** `cvv` (3 digits)
- **Example:** `generate_cvv {"cvk":..., "pan":"4000000000000002",
  "expiry":"2512","service_code":"000"}` → `cvv: 483`

#### `CY` → `CZ` — Verify a CVV/CVC  ·  action `verify_cvv`
The authorization-time check: recompute the CVV and compare to the one presented
in the transaction. `error_code 00` = match, `01` = mismatch (declined).

- **Params:** `cvk`, `cvv`, `pan`, `expiry`, `service_code`
- **Example:** wrong CVV → `error_code: 01`

### PIN processing

#### `CA` → `CB` — Translate PIN from TPK to ZPK  ·  action `translate_pin`
A PIN block arrives from a terminal encrypted under a Terminal PIN Key (TPK); to
forward it to the scheme/issuer it must be re-encrypted under a Zone PIN Key
(ZPK) — without the clear PIN ever being exposed. This is the bread-and-butter
acquirer operation at the terminal→network boundary.

- **Params:** `src_key`/`tpk`, `dst_key`/`zpk`, `pin_block`, `pan`,
  `max_pin_len` (`12`), `src_format`/`dst_format` (`01` = ISO-0)
- **Returns:** `pin_length`, `pin_block` (re-encrypted)

#### `CC` → `CD` — Translate PIN from ZPK to ZPK  ·  action `translate_pin_zpk_zpk`
Re-encrypt a PIN block from one ZPK to another (and optionally change PIN block
format) — used at network boundaries between zones, e.g. switch-to-switch.

- **Params:** `src_key`/`src_zpk`, `dst_key`/`dst_zpk`, `pin_block`, `pan`,
  `src_format`, `dst_format`, `max_pin_len`
- **Returns:** `pin_length`, `pin_block`

#### `EC` → `ED` — Verify a PIN (Visa PVV)  ·  action `verify_pin`
Issuer-side PIN verification using the Visa PVV method: decrypt the incoming PIN
block under the ZPK and check the PIN against the stored PIN Verification Value.
`error_code 00` = correct PIN, `01` = wrong PIN.

- **Params:** `zpk`, `pvk`, `pin_block`, `pan`, `pvki` (`1`), `pvv`,
  `format` (`01`)

#### `G0` → `G1` — Translate PIN from BDK (DUKPT) to ZPK  ·  action `dukpt_translate_pin`
Modern terminals encrypt PINs with a unique per-transaction key derived via
DUKPT from a Base Derivation Key (BDK) and a Key Serial Number (KSN). This
command re-derives that key from BDK+KSN and translates the PIN block to a ZPK —
the acquirer operation for DUKPT-enabled terminals.

- **Params:** `bdk`, `zpk`, `ksn`, `pin_block`, `pan`,
  `src_format`/`dst_format` (`01`)
- **Returns:** `pin_length`, `pin_block`

### Message authentication (MAC)

#### `M6` → `M7` — Generate a MAC  ·  action `generate_mac`
Compute a retail MAC (ISO 9797-1 algorithm 3) over a data block — used to
protect the integrity of messages between host and switch.

- **Params:** `key` (MAC key token), `message` (hex data block)
- **Returns:** `data` = the MAC (16 hex)

#### `M8` → `M9` — Verify a MAC  ·  action `verify_mac`
Recompute the MAC over the message and compare to the one supplied.
`error_code 00` = intact, `01` = tampered/mismatch.

- **Params:** `key`, `message`, `mac`

### EMV cryptograms

#### `KQ` → `KR` — Verify ARQC / generate ARPC  ·  action `verify_arqc`
Chip-card authentication: an EMV transaction carries an ARQC (Authorisation
Request Cryptogram) the issuer must validate, and optionally an ARPC
(Authorisation Response Cryptogram) the issuer returns to the card. The HSM
derives the card's session key from the issuer master key and checks the ARQC.

- **Params:** `mode` (`0` verify, `1` verify+ARPC), `mk_ac` (issuer master key),
  `pan`, `psn` (`00`), `atc`, `txn_data` (hex), `arqc`, `arc`
- **Returns:** `arpc` (when mode `1`); `error_code 01` if the ARQC fails

> Note: the real `KQ` packs some fields as binary; the simulator accepts them as
> hex-ASCII so the link stays clean. The cryptography (ICC master key derivation,
> session key, ARQC MAC, ARPC) is real. The client adapter encodes the same way.

### Anything else

An action the client doesn't know falls back to a **raw passthrough** — send the
literal command and data yourself:

```python
hsm_command(action="raw", command="NC", data="")
```

Unknown commands return `error_code 30`.

---

## 6. Error codes

| Code | Meaning |
|---|---|
| `00` | No error / success |
| `01` | Verification failed (CVV / PIN / MAC / ARQC mismatch) |
| `10` | Key parity error |
| `15` | Invalid input data / could not parse the command |
| `30` | Unknown / unsupported command |
| `68` | Command disabled (via `disabled_commands` or a scripted override) |

A step is only **passed** when `error_code == "00"` *and* its assertions hold —
so a negative test (expecting `01`) is best done with the simulator's
`disabled_commands` / scripted `commands` overrides, or asserted out-of-band,
since a non-`00` reply marks the step failed by design.

---

## 7. Key separation (`variant_lmk`)

With `variant_lmk: false` (default) every key type shares one LMK variant, so any
token works anywhere — simplest for protocol testing. With `variant_lmk: true`
the simulator wraps each key type under its own variant, mirroring real payShield
**key separation**: a key minted as a CVK won't check out when a command tries to
use it as a ZPK (it decrypts to a different clear key → parity/verification
error). Turn it on when you want to test that your host uses the right key type
for each operation.

---

## 8. Common key types

| Code | Key | Used by |
|---|---|---|
| `000` | ZMK — Zone Master Key | A6 / K8 (key exchange) |
| `001` | ZPK — Zone PIN Key | CA / CC / EC / G0 |
| `002` | TPK / PVK / TMK | CA (source), EC (PVK) |
| `008` | ZAK / TAK — MAC key | M6 / M8 |
| `109` | MK-AC — EMV issuer master key | KQ |
| `302` | BDK / IPEK — DUKPT base key | G0 |
| `402` | CVK — Card Verification Key | CW / CY |

Key **schemes** (the token's leading tag): `U` = double-length TDES (32 hex),
`T` = triple-length (48 hex), `Z` = single-length DES (16 hex).

---

## Quick start

```python
# 1. Start the HSM simulator (persisted, shows on the Simulators page)
start_payshield_simulator(label="payShield 10K", port=1500)

# 2. Probe it
hsm_command(action="nc")
#   → response_code "ND", error_code "00", firmware "0007-E000"

# 3. Generate a CVK, make a CVV, verify it
g  = hsm_command(action="generate_key", params={"key_type":"402","scheme":"U"})
cvk = g["response"]["key"]
cv = hsm_command(action="generate_cvv",
                 params={"cvk":cvk,"pan":"4000000000000002",
                         "expiry":"2512","service_code":"000"})
hsm_command(action="verify_cvv",
            params={"cvk":cvk,"cvv":cv["response"]["cvv"],
                    "pan":"4000000000000002","expiry":"2512","service_code":"000"})
#   → error_code "00"  ✓

# 4. Or run the whole regression scenario in the portal
#    Project "HSM Tests" → payshield_hsm_full → run against the payshield-sim environment
```

For load and concurrency, point the environment's `hsm` target at the simulator
with `"connections": 8` and drive commands in parallel — each socket pipelines
and the pool round-robins.
