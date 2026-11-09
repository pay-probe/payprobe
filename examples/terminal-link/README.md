# Example pack — POS terminal link

A complete, generic reconstruction of the kind of test case a commercial
terminal test tool runs against a processing host: *one POS terminal sends a
magnetic-stripe purchase and expects an approval that echoes what it sent.*
Everything a tool like that keeps in its profile has a PayProbe home:

| Test-tool concept | PayProbe piece | File |
|---|---|---|
| **Service** / connection — host:port, client or server, link-level framing string | **Connection** (`mode`, `framing.preset` / `framing.link_level`) | `connection.json` |
| **Protocol** — the field table and encodings of the dialect | **Message Format** (`definition.fields`, `definition.encoding`, `presence`) | `message-format.json` |
| **Device object** — terminal id, merchant id, currency, keys, location | **Terminal pool** record → `${terminal.*}` | `terminal-pool.json` |
| **Card object** — PAN, expiry, track 2 | **Card pool** record → `${card.*}` | `card-pool.json` |
| **Scenario** — build-and-send, receive-and-validate (mandatory / optional fields, `${request:F02}` echo checks) | **Scenario** step with `values`, `assertions` (`${request.values.2}`) | `scenario.json` |
| **Message templates** — the host side's canned replies | **Responder / saved simulator** rules | `host-simulator.json` |

## The wire

The terminal is the **client**: the connection is `mode: outbound` and the
worker dials the host. Each message is framed with a **4-character
ASCII-decimal length that counts only the body** — the framing preset `ascii4`.
Terminal tools print the same thing as a compact link-level string,
`CHARDEC:false:false:BIG:4:` (`<length encoding>:<counts prefix>:<counts
header>:<byte order>:<width>:<header bytes>`); PayProbe accepts that string
verbatim in `framing.link_level` and expands it to the explicit keys on save.
Other presets: `binary2` (the ISO norm), `binary2_inclusive`, `binary2_pad2`,
`binary4_pad14_inclusive`, `bcd2`, `ascii2` / `ascii5` / `ascii6`, `hex4`,
`ascii4_trailer` (mandatory ETX after the body) and `stx_etx_lrc` (no length:
STX … ETX LRC). `pos_terminal_link_stx_etx` in `connection.json` shows the
same dialect over that serial-style envelope.

The dialect (`message-format.json`) is ISO 8583:1987 with ASCII fields, a **raw
binary bitmap**, ASCII LL/LLL lengths, raw-binary PIN and MAC fields, a
packed-BCD length on DE 55 and a **three-digit response code** (`000` =
approved). DE 48 / DE 63 are ASCII tag-length-value: 3-character tag, 3-digit
length, value — `0360010` is tag `036`, length `001`, value `0`.

## Install

Through the API (or the matching MCP tools `create_format`, `save_connection`,
`save_terminal_pool`, `save_card_pool`, `create_scenario`,
`create_saved_simulator` / `start_saved_simulator`):

```bash
S=http://localhost:8000      # scenario-service
O=http://localhost:8100      # orchestrator
FID=pos-terminal-link-ascii-fields-binary-bitmap-1
curl -X PUT  $S/formats/$FID -H 'content-type: application/json' -d @message-format.json
curl -X PUT  $S/connections/pos_terminal_link -H 'content-type: application/json' \
     -d "$(jq '.connections.pos_terminal_link' connection.json)"
curl -X PUT  $S/test-data/terminal-pools/pos_lanes -H 'content-type: application/json' -d @terminal-pool.json
curl -X PUT  $S/test-data/card-pools/pos_magstripe_cards -H 'content-type: application/json' -d @card-pool.json
curl -X POST $S/scenarios -H 'content-type: application/json' -d @scenario.json
# optional host stand-in (then point pos_terminal_link at 127.0.0.1:52000)
curl -X POST $O/simulator-configs -H 'content-type: application/json' \
     -d "{\"label\": \"POS host stand-in\", \"config\": $(jq 'del(._comment)' host-simulator.json)}"
```

The dialect is bound **on the connection** (`message_format_id`), because the
reader decodes every reply on the shared socket with the bound table and
encoding — a dialect with non-standard lengths (the 3-digit DE 39 here) does
not work when bound only on a step. The step names the same format so dry runs
check presence rules against it.

Replace the placeholder terminal ids, merchant ids and PANs with the ones your
host and issuer test environment know; fixed-width fields are sent as given,
not padded (DE 41 = 8 characters, DE 42 = 15).

## Running against a real host — what to check first

* **Clock.** DE 7 is stamped in UTC by the worker; DE 12 in the scenario uses
  the terminal's local clock (`${now.fmt(%y%m%d%H%M%S,4)}` — adjust the offset).
  Stale time stamps are the most common reason a host declines a replayed case.
* **Uniqueness.** DE 11 (STAN) is generated per message by the connection's
  correlation rule; a tool-exported case with a *stored* STAN looks like a
  duplicate to the host.
* **Terminal registration.** DE 41 / DE 42 come from the bound terminal
  record; the host only answers for terminals it knows.
* **Framing.** If the host hangs up right after the first frame, the length
  prefix is wrong: compare `framing.preset` / `framing.link_level` with the
  host's connection sheet, and read the wire journal on *Test connection*.
