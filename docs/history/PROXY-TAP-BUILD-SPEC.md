# Proxy Tap — Stage 1 Build Spec

Scope: the **tap-mode pass-through relay** from ADR-0002 — the smallest slice
that puts PayProbe in the middle of a live connection and proves it forwards
byte-for-byte. No intercept, no stub, no capture store, no TLS. Just: accept a
client, dial the real upstream, relay both directions untouched, and decode each
frame for live visibility.

Reference: `docs/adr/0002-tcp-proxy-tap-forwarding-mode.md`.

## Outcome

- A `TcpProxy(TcpResponder)` listener that relays a client↔upstream conversation.
- `kind: "proxy"` configs start/stop/save like any other simulator.
- `GET /peers` reports the proxy's **outbound** upstream socket (today `[]`).
- Golden tests prove `tap` mode is exactly pass-through (bytes in == bytes out).

Explicitly **out** of stage 1: capture-to-store, save-as-scenario, intercept/stub
modes, chaos on the relayed leg, TLS, portal UI.

## Files

### 1. `packages/worker/adapters/tcp/proxy.py` (new)

`TcpProxy(TcpResponder)` — reuse the parent's `__init__` (framing, protocol,
decode, `_conn_meta`, stats), and override only connection handling.

```python
class TcpProxy(TcpResponder):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        up = config.get("upstream") or {}
        self.up_host = up.get("host")
        self.up_port = int(up.get("port", 0))
        self.up_connect_timeout = float(up.get("connect_timeout_sec", 10))
        self.mode = str(config.get("mode", "tap")).lower()
        # outbound liveness, mirror of _conn_meta for the upstream leg
        self._upstream_meta: dict[asyncio.StreamWriter, dict] = {}

    async def _handle(self, reader, writer):
        # 1. register inbound peer (reuse parent's _conn_meta bookkeeping)
        # 2. open upstream: asyncio.open_connection(self.up_host, self.up_port)
        #    wrapped in asyncio.wait_for(..., self.up_connect_timeout)
        # 3. run two pumps concurrently:
        #       client -> upstream   (self._pump(reader, up_writer, "c2u"))
        #       upstream -> client   (self._pump(up_reader, writer, "u2c"))
        #    via asyncio.gather(..., return_exceptions=True)
        # 4. finally: close both sockets, pop both meta entries

    async def _pump(self, src_reader, dst_writer, direction):
        while True:
            body = await self._read_frame(src_reader)   # inherited framing
            if body is None:
                break
            self._observe(body, direction)              # decode for visibility
            dst_writer.write(self._frame(body))         # re-frame, forward as-is
            await dst_writer.drain()
```

Key invariants:

- **Byte-exact:** `_read_frame()` strips the length prefix/TPDU; `_frame()` puts
  them back with the *same* framing config, so `_frame(_read_frame(x)) == x` for
  any well-formed frame. The golden test pins this. Tap never calls `_decode →
  _resolve → _encode`; it forwards the original body.
- **`_observe()`** runs `self._decode(body)` inside a try/except, appends to
  `self.received` and bumps `by_mti` (reusing parent counters), but a decode
  failure must **not** stop the relay — tap forwards bytes it can't parse.
- **Teardown:** closing one leg closes the other (a half-open relay is useless).
  Reuse the parent's best-effort `writer.close()` pattern from `_handle`'s
  `finally`.

Override `peers()` is not needed (inbound side inherited). Add `upstream_peers()`
returning rows from `_upstream_meta` in the same shape `peers()` uses, for the
`/peers` outbound leg.

### 2. `packages/orchestrator/api/main.py`

Dispatch and outbound reporting.

- In `_responder_for()` add the branch (before the `else`):

  ```python
  elif kind == "proxy":
      from worker.adapters.tcp.proxy import TcpProxy
      cls = TcpProxy
  ```

  `kind` already reads `config.get("protocol") or config.get("kind")`, so either
  `{"kind": "proxy"}` or `{"protocol": "proxy"}` works. (Confirm `protocol`
  still carries the wire protocol, e.g. `iso8583`, for decode — prefer
  `{"protocol": "iso8583", "kind": "proxy"}`.)

- In `GET /peers`, replace the hard-coded `"outbound": []` by collecting
  `upstream_peers()` from any `SIMULATORS` entry that exposes it:

  ```python
  outbound = []
  for sid, r in SIMULATORS.items():
      up = getattr(r, "upstream_peers", None)
      if callable(up):
          for p in up():
              outbound.append({**p, "direction": "outbound",
                               "source": "proxy", "source_id": sid,
                               "label": getattr(r, "_label", sid),
                               "protocol": r.protocol})
  ```

  Update the `counts` block (`outbound`, total) accordingly.

- `_resolve_simulator_config()` already injects `fields`/`validate` from a bound
  `message_format_id` — no change needed; the proxy decodes/validates with the
  same dialect machinery the responder uses.

### 3. `packages/orchestrator/api/simulator_store.py`

`SimulatorStore` stores arbitrary `config` dicts, so a proxy config persists with
no schema change. Optional polish: in `_info()` surface `mode`/`upstream` for the
portal list (additive, non-breaking).

## Tests

### `packages/worker/tests/test_proxy.py` (new)

Topology per test: a **backend `TcpResponder`** (the "real" upstream) ← a
**`TcpProxy`** ← a **`TcpAdapter`** client. Reuse the `_adapter`/`_run` helpers'
style from `test_responder.py`.

1. `test_tap_relays_request_and_response` — client sends `0200` through the
   proxy; assert the reply matches what the backend responder would have sent
   directly (`mti == 0210`, echoed STAN, `39 == 00`). Proves end-to-end relay.

2. `test_tap_is_byte_exact` — the golden test. Stand up a trivial echo upstream
   (or capture frames at the upstream socket) and assert the bytes the upstream
   receives equal the bytes the client sent, and vice versa — including length
   prefix and any TPDU. Parametrize over framing variants:
   `length_includes_prefix` true/false, `tpdu_bytes` 0 and >0, big/little order.

3. `test_proxy_forwards_undecodable_frame` — send a frame that fails
   `_decode`/validation; assert it is still relayed (tap must not gate the wire
   on parseability) and the relay stays up.

4. `test_upstream_down_closes_client` — point `upstream` at a dead port; assert
   the client connection is closed promptly (no hang) within the connect
   timeout.

5. `test_teardown_closes_both_legs` — client disconnects mid-stream; assert the
   upstream socket is closed too (check `_upstream_meta` empties).

### `packages/orchestrator/tests/test_simulators.py` (extend)

6. `test_peers_reports_proxy_outbound` — start a `kind: "proxy"` simulator with a
   live upstream + one client; `GET /peers` returns one `inbound` and one
   `outbound` row, `counts.outbound == 1`.

7. `test_start_stop_proxy_simulator` — save a proxy config, start it, confirm
   `GET /simulators` lists it, stop it cleanly.

Run: `pytest packages/worker/tests/test_proxy.py packages/orchestrator/tests/test_simulators.py -q`
(async tests use the project's existing pytest-asyncio config in `pytest.ini`).

## Checklist

- [ ] `TcpProxy` with concurrent dual pumps, byte-exact forward, both-legs teardown
- [ ] `upstream_peers()` + `_upstream_meta` bookkeeping
- [ ] `_observe()` decode-for-visibility, failure-tolerant
- [ ] `_responder_for()` proxy branch
- [ ] `/peers` outbound leg populated + counts fixed
- [ ] worker tests (relay, golden byte-exact ×framing variants, undecodable, upstream-down, teardown)
- [ ] orchestrator tests (peers outbound, start/stop)
- [ ] `make lint test` green

## Risks / watch-items for stage 1

- **Frame-boundary fidelity.** The whole correctness claim rests on
  `_frame(_read_frame(x)) == x`. If any framing flag round-trips lossily, tap
  silently corrupts traffic — hence the parametrized golden test is mandatory,
  not optional.
- **Backpressure.** Two independent pumps; if the upstream is slow, `drain()`
  applies backpressure per direction, which is correct. Don't add buffering.
- **No capture yet.** Stage 1 only decodes into the existing `received`/`by_mti`
  counters for live stats; durable capture + redaction is stage 2 (and is where
  the Secrets Vault / `SecretBox` posture from the ADR lands). Keep PAN/key
  handling in mind but don't build it here.
```
