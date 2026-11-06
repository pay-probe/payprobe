# TCP adapter: message header, ASCII length prefix, health-check modes, wire journal (2026-11-06)

**Scope:** `packages/worker/adapters/tcp` (`framing.py`, `adapter.py`,
`responder.py`, `protocols.py`), the connection store's framing validation,
`POST /connections/test` and the new `POST /connections/frame-preview`
(MCP `preview_connection_frame`). No ISO 8583 field table or Message Format
changed; no saved connection was touched.
**Trigger:** testing a POS host whose vendor client frames with
"standard header GENERIC4, header type CHARDEC, bytes length 4, byte order
Big" — a 4-character ASCII decimal length equal to the payload length (`"0016"`
+ 16 bytes). The saved `pos_host` connection did not produce that framing,
several keys looked ignored, and *Test connection* reported only "connected,
but health check failed" after a 30 s wait.

## What was measured (before any change)

| Observation | Measured cause |
|---|---|
| `mhdr` / top-level `header` had no effect | `TcpAdapter` never read either key (its framing read `length_prefix_bytes`, `length_encoding`, `length_byte_order`, `length_includes_prefix`, `length_includes_header`, `tpdu_bytes`, `tpdu_outbound_hex`, `encoding`; the only `header` it knew is the legacy sign-on payload key). Separately, the portal's connection form rebuilds the stored config from its own fields on save (`connection.models.ts:toAdapterConfig`), so any key it does not model (`mhdr`, `health_check`, `bcd`, …) is dropped by a portal save. |
| `length_prefix_bytes: 2` with `length_encoding` unset ≠ the 4-digit prefix | Correct behaviour: that is a 2-byte binary MLI. The 4-digit ASCII prefix already existed (`length_prefix_bytes: 4, length_encoding: ascii`, since `0cb62420`) but the vendor name `chardec` was refused. |
| `sign_on.enabled: false` still produced an exchange | `health_check()` always sent the protocol's default probe (`0800`, DE 70 `301`) whatever `sign_on` said; `sign_on` only governs connect time. A host that wants its own sign-on first never answers it → 30 s timeout → "health check failed" with no reason. |
| `length_encoding: chardec` → "must be one of binary, ascii, bcd" | Worker error text; the scenario-service store was stricter still (`binary`/`ascii` only — it predates ADR-0011's `bcd`). |
| Host closes ~10 ms after the first frame, "0 bytes read on a total of 2 expected" | Nothing recorded the outbound bytes or the close timing outside DEBUG logs; `raw_log` only exists for executed steps, not for the probe. |

## What changed

- **One framing model, both ends:** `framing.FrameSpec` (prefix, TPDU, fixed
  message header, inbound widths) is built from the config by the adapter and
  the responder/proxy alike. New keys: `framing.header` (ASCII) /
  `framing.header_hex` / `framing.header_bytes`, top-level `mhdr` alias;
  `length_includes_header` now covers TPDU + header. `length_encoding` accepts
  `chardec` (→ `ascii`); the store accepts `bcd` and folds `chardec` on save.
  A top-level `header` on an ISO connection is logged as ignored instead of
  vanishing. No prefix (`length_prefix_bytes: 0`) stays refused by design.
- **Health-check modes** (`health_check.mode`): `probe` (default, unchanged),
  `connect` (send nothing; healthy when the socket survives `settle_ms`),
  `message` (send the configured message). The default was deliberately not
  flipped: every existing connection and the diagnostics "dialect mismatch"
  hint rely on the probe; `connect`/`message` are opt-in per connection, and
  the probe's failure text now names them.
- **Wire journal:** every open / tx / rx / close per socket with the first 64
  bytes as hex and ms timing (`adapter.wire_events()`, logger
  `worker.adapters.tcp.wire`, `PAYPROBE_TCP_WIRE_LOG=1` promotes it to INFO).
  A peer close records "N ms after the last send, M of K expected bytes
  arrived" and that timing travels with the step / health error.
  `POST /connections/test` returns `health` + `wire`; `raw_log` carries RX too.
- **Dry run:** `TcpAdapter.preview_frame()` / `POST /connections/frame-preview`
  / MCP `preview_connection_frame` — the exact frame, split into blocks, with
  no socket.

## Evidence

- Worker suite 649 → 680 passed (9 skipped both times; `test_payshield_e2e`
  run alone, 1 passed both times). Orchestrator 422 passed; scenario-service
  345 passed; mcp-server 171 passed (each per package — the four
  `test_registry` failures seen only in one combined session are the known
  "no current event loop" artefact of combined collection, present before this
  change). `ruff check` / `black --check` clean on the worker.
- Exact-hex tests per configuration in `worker/tests/test_tcp_framing.py`
  (`test_frame_spec_exact_hex_per_configuration`,
  `test_client_frame_0016_plus_16_payload_bytes_is_20_bytes`); health modes,
  journal timing and the dry run in `worker/tests/test_tcp_adapter.py`;
  endpoint behaviour in `orchestrator/tests/test_connection_test_endpoint.py`.

## Owed

- Portal: the connection form does not model `framing.header` /
  `health_check` (and drops them on save) — a host `npm run build` is needed
  for any portal change, so this is left for a follow-up; until then save such
  connections through the API / MCP `save_connection`.
- The real-host run against the host port with
  `{"length_prefix_bytes": 4, "length_encoding": "ascii"}` + the host's own
  sign-on as `health_check.mode: message` — the dry run and journal exist to
  make that a five-minute comparison against the vendor client's bytes.
