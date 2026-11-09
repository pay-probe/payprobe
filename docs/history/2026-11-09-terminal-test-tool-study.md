# Terminal test-tool study → link-level framing options, terminal records, echo assertions (2026-11-09)

**Scope:** `packages/payprobe_common/link_framing.py` (new),
`packages/worker/adapters/tcp` (`framing.py`, `adapter.py`, `responder.py`,
`chaos.py`, `proxy.py`), `packages/worker/engine` (`generators.py`,
`runner.py`), scenario-service (`connection_store.py`, `test_data_store.py`,
`models/scenario.py`), the portal framing editor / connection model / test-data
page, the new `examples/terminal-link/` pack. No ISO 8583 field table, Message
Format builtin or saved record was changed.

**Trigger:** a working configuration from a commercial terminal test tool (one
"test case": a POS magnetic-stripe purchase against a processing host) was
handed over to be reproduced on PayProbe, with two asks: make the tool's
*network parameters* configurable on the Connection in a generic way, and take
whatever ideas in the tool PayProbe lacked. The tool, its vendor and the
customer are deliberately not named anywhere in the repo; every name below is
structural.

## What the tool keeps in a profile (and where each thing lives here)

| Tool concept | What it is | PayProbe home |
|---|---|---|
| *Service* | host:port, `CLIENT` / `SERVER` / multi-connection server, a **link-level string** `<len-enc>:<counts-prefix>:<counts-header>:<byte-order>:<width>:<header-bytes>`, an optional scripted crypto layer | **Connection** — `mode`, `framing.*` (now also `preset` / `link_level`), `extends` |
| *Protocol* | a field table per dialect: per-field length type, length encoding (binary / ASCII-dec / ASCII-hex / BCD), padding side+char, TLV sub-structures with their own tag/length encodings | **Message Format** `definition.fields` + `definition.encoding` (+ per-field `encoding` overrides) |
| *System tags* + *data types* + *data objects* | typed records (card, terminal, network, transaction parameters); a message field bound to a tag is filled from the bound object | **Card pool** records → `${card.*}` (existed); **Terminal pool** records → `${terminal.*}` (new) |
| *Scenario* | `BuildAndSend` + `RecvAndParse` with `strictValidation`, mandatory/optional field flags, `${<req>:F02}` echo references, 10 s timeout | **Scenario** step `values` + `assertions`; `expected` may now reference `${request.*}` |
| *Plugins* on a field | `MMDDhhmmss`, `YYMMDDhhmmss`, `INCREMENT_NUMBER6`, `RANDOM_NUMBER6`, `RANDOM_PAN`, track / PIN-block / ARQC / CVV2 / MAC builders | `${now.fmt}`, `${seq.*}`, `${rand.*}`, auto-stamped DE 7/12/13, auto STAN, `crypto_tools` |
| *Message templates* (IN) | match an inbound message, reply from a template echoing `${in:F11}` | **Responder rules** (`echo` / `set` / `generate`) and participant flows |
| *Dictionaries* | named value lists per field (response codes with descriptions) | not built — see owed |
| *Loading profile* | card set + **device set** (N virtual terminals, each with its own socket, `portSeed`, reconnect mode, a `RangedValueSupplier` for terminal ids) + staggered scenarios (`Pos_077ms` … `Pos_923ms`) + fire-and-forget variants | load runs + pools; one-socket-per-terminal and staggered start not built — see owed |

The one case handed over decodes to: outbound TCP client, link level
`CHARDEC:false:false:BIG:4:` (4 ASCII-decimal digits counting only the body),
a 1987 dialect with ASCII fields, **raw binary bitmap**, ASCII LL/LLL lengths,
raw-binary DE 52/64, DE 55 with a BCD length, DE 48/63 as ASCII TLV
(3-char tag, 3-digit length) and a **three-digit DE 39** (`000` = approved);
request `0200` with PAN, processing code, amount, DE 7/11/12 (the tool's
profile had them stored and stale — its own notes say to switch them to the
date/counter plugins before replaying), DE 22 `020`, DE 24 `200`, DE 25 `00`,
track 2, DE 41/42 from the bound terminal object, DE 48 `036=0`, DE 49; reply
`0210` must echo DE 2/41/49 and carry `000`. All of that was already
expressible — the framing since `0cb62420` (ASCII prefix) and
2026-11-06 (`chardec` alias) — so the reproduction is configuration, shipped
as `examples/terminal-link/`.

## What was measured

| Observation | Measured cause |
|---|---|
| The tool's link-level string carries two flags and a trailing "pad" PayProbe had no single key for | Disassembled the tool's framing class: `hdrType:includeHdr:includeOffset:byteOrder:lenLength:padOffset` — `includeHdr` = length counts its own prefix, `includeOffset` = length counts the fixed bytes after it, `padOffset` = dotted hex sent after the prefix. Its presets: four ASCII-decimal widths (0/2/4/5), binary-2 inclusive, binary-2 plain, binary-2 + `00.00` not counted, binary-4 counting prefix + 14 zero bytes, an ASCII-4 + mandatory ETX trailer, and an STX…ETX LRC envelope. The read loop with width 0 reads "whatever arrives" (no framing). |
| `length_encoding: "hex"` refused | Not implemented (the tool has `CHARHEX`). |
| No trailer, no delimiter envelope | `FrameSpec` only knew prefix ‖ TPDU ‖ header ‖ body. |
| Terminal pools are `list[str]`; DE 41/42/49 typed into every scenario | `${pool.terminal}` yields an id only; no bound terminal record. |
| "Response DE 2 must equal request DE 2" not expressible on the same step | `assertions[].expected` was never passed through `resolve_value`; only the payload was. |
| The pack's first run desynced at DE 48 | The merchant id was 14 chars in a fixed 15-char DE 42; the codec writes fixed fields as given (no padding), the tool pads per field (`ProtocolFieldPadding` side+char). Not fixed — see owed; the pack uses full-width ids and says so. |
| The host stand-in's reply desynced at DE 38 | `responder` `generate: random_rrn` wrote **6** digits into the 12-char DE 37 whenever the request carried no RRN (pre-existing; any 1987 dialect). Fixed: sized to the bound table. |

Baseline before any change (clone of `a631a7f`, Python 3.13, `mcp<2`):
worker `648 passed, 11 skipped, 1 failed` (`test_socket_registry::
test_drop_unregisters_then_reconnect_reregisters` — fails alone on the untouched
tree, unrelated to framing), scenario-service `348 passed`.

## What changed

- **One framing vocabulary, shared** — `payprobe_common/link_framing.py`:
  `LENGTH_ENCODINGS` (+ `hex`), vendor aliases (`chardec`, `charhex`, `bch`,
  `bcd…`), `FRAMING_MODES` (`length_prefix` | `delimited`), generic
  `FRAMING_PRESETS` (`binary2`, `binary2_inclusive`, `binary2_pad2`,
  `binary4_pad14_inclusive`, `bcd2`, `ascii2/4/5/6`, `ascii4_trailer`, `hex4`,
  `stx_etx_lrc`), `parse_link_level()` for the compact string,
  `expand_framing()` (link_level < preset < explicit keys; labels kept). The
  worker `FrameSpec` and the scenario-service `ConnectionDraft` both import it
  — the store's hand-mirrored tuples are gone (`test_framing_length_encodings_
  match_the_worker` now pins identity).
- **`FrameSpec`**: `hex` length prefix; `trailer_hex` / `trailer_bytes`
  (appended on send, checked and stripped on receive, never counted);
  `mode: delimited` with `start_hex` / `end_hex` / `checksum: lrc|none`
  (refuses a body containing the end byte); `read_frame(reader)` — the one
  read loop, now used by the adapter and the responder (and therefore the
  proxy and every simulator). A width of 0 is still refused, with the message
  pointing at `delimited`.
- **Chaos** on delimited frames: `bad_length` drops ETX + LRC (the same stall),
  the other modes keep the envelope valid.
- **Connection store**: expands `preset` / `link_level` into explicit keys on
  save (so a portal that only knows the explicit keys round-trips them),
  validates mode-specific keys, hex, widths.
- **Terminal records**: `TerminalPoolDraft.terminals: list[str | dict]`
  (a record needs an `id`); `${terminal.id|merchant|currency|location|type|
  pinblock_format|<any key>|seq}` bound per transaction like `${card.*}`;
  `${pool.terminal}` still yields ids. Key material stays in the vault —
  records carry key *names*.
- **Echo assertions**: `assertions[].expected` is resolved like the payload,
  with the step's own resolved payload as `${request.*}`; an unresolvable
  reference is a clear step `error`. `request` and `terminal` are reserved
  reference prefixes in the scenario validator.
- **Responder**: `generate: random_rrn` sized to the bound table.
- **Portal** (owed a host build — see below): `LengthEncoding` gains `bcd` /
  `hex`; `FramingConfig` gains `preset`, `linkLevel`, `mode`, `headerHex`,
  `trailerHex`, `startHex`, `endHex`, `checksum`, all round-tripped by
  `framingToApi` / `framingFromApi`; the framing editor shows envelope mode,
  link-level string, header/trailer and the delimited fields, previews BCD /
  hex prefixes; adapter catalog rows for every new key; test-data page accepts
  terminal records as JSON lines (same convention as cards).
- **`examples/terminal-link/`**: connection (`preset: ascii4`, format bound on
  the connection), Message Format (the dialect above), terminal + card pools,
  the purchase scenario with echo assertions, a host stand-in responder, a
  README mapping tool concepts to PayProbe pieces and the first things to
  check against a real host. `worker/tests/test_terminal_link_example.py`
  runs it end to end (adapter ↔ responder over ascii4, pools bound, echo
  assertions resolved) and pins that no vendor / customer name appears.

Tests added: `worker/tests/test_link_framing.py` (19), terminal + echo cases in
`test_generators.py` / `test_engine.py`, pack tests (4), store tests in
`test_connections.py` / `test_test_data.py`. Four existing tests that used
`"hex"` as their example of an *unknown* encoding now use `"octal"`.

## Owed (in order)

1. **Portal click-through** of the framing editor (mode switch, link-level
   paste, header/trailer), the Connections page summary and the test-data
   terminal records. Prettier is clean and `ng build --configuration
   development` compiled in the sandbox (the production build stopped only at
   Google-Fonts inlining, which the sandbox network refuses); run the
   production build on the host.
2. **Fixed-width padding rule.** The tool pads every fixed field per its
   table (numeric: left `0`; text: right space; DE 14 right `1`…); our codec
   writes the value as given, so a short DE 42 silently desyncs the frame.
   Options: a per-field `encoding.pad_char` / `pad_side` honoured by
   `encode_value` (ADR-0011 territory, flag-gated), or at least a refusal at
   pack time (`check()` already names it on a dry run).
3. **Load "device set"**: N virtual terminals, each its own socket from a
   terminal record (the tool's `isTcpGateMode` + `portSeed` + ranged ids),
   reconnect-per-device, staggered start offsets across workers
   (`Pos_000ms … Pos_923ms`), a fire-and-forget (no-response) load variant.
   Needs a spec — it touches `LoadBus` / `load_worker` pooling and ADR-0001
   fleet hosting.
4. **Field dictionaries**: named value lists per DE (response codes with
   descriptions, POS entry modes, function codes) surfaced in the inspector
   and the step editor instead of only in the format's `name` text.
5. **ASCII TLV sub-fields** on DE 48 / DE 63 (3-char tag + LLL length) as a
   Message Format sub-structure, the way DE 55 BER-TLV is modelled, so a step
   can set `48.036 = 0` instead of the pre-built string.
6. **Connection-level crypto layer** (the tool's optional send/recv transform
   on the whole frame) — only if a host ever needs it; none in the profile
   did.
