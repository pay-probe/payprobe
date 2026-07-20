# ADR-0002: Transparent TCP proxy / tap (forwarding mode for TcpResponder)

**Status:** Implemented (stages 1–2; TLS deferred) — status verified 2026-07-07
**Date:** 2026-06-26
**Deciders:** PayProbe maintainers (David + reviewers)

> **Implementation state (2026-07-07):** all three modes are live in
> `worker/adapters/tcp/proxy.py` — `tap` (byte-exact relay), `intercept`
> (rule/chaos mutation in flight), `stub` (answer matched requests locally,
> forward the rest) — with the bounded, redacted capture in
> `worker/adapters/tcp/capture.py` (PAN masking, logging rules, optional raw
> hex), orchestrator endpoints `GET/DELETE /simulators/{sid}/capture` and
> `POST …/capture/save-as-scenario`, both `/peers` legs, and a portal capture
> panel. 30 tests across `test_proxy.py`/`test_capture.py`. One deliberate
> deviation from the text below: capture is an in-memory ring buffer, not the
> file-backed `ProxyCaptureStore` — save-as-scenario is the durable path.
> Only **TLS** on either leg remains deferred.

## Context

Today PayProbe can *originate* traffic (the test engine / load subsystem drive a
`TcpAdapter` at a host) and *terminate* traffic (a `TcpResponder` listens and
answers in place of a real host). There is no third posture: sitting **in the
middle** of a live connection between a real client and a real upstream,
relaying frames both ways while observing — and optionally modifying — what flows
through.

That middle posture unlocks things neither of the existing modes can do:

- **Passive tap** — capture a real client↔host conversation (both directions)
  for inspection, without standing up a simulator or writing a scenario.
- **Record & replay** — turn observed (prod-like) traffic into a scenario or
  starter flow, so live conversations become regression tests.
- **Active intercept (MITM)** — mutate fields in flight, inject response codes,
  or apply latency/drop/corruption to a *real* exchange for resilience testing.
- **Hybrid stub** — forward most traffic to the real upstream but answer selected
  messages locally when the upstream is down or a path needs stubbing.

### What we can reuse

The `TcpResponder` (`packages/worker/adapters/tcp/responder.py`) already owns
almost every primitive a proxy needs:

- Framing that mirrors the adapter: `_read_frame()` and `_frame()` handle the
  length prefix, TPDU, and header-inclusion flags.
- Protocol decode: `_decode()` turns a frame into a structured `{mti, de, ...}`
  view for ISO 8583 (or `header_echo`).
- Dialect validation: `_validate()` / `iso8583.iso_validate()` against a bound
  Message Format (`warn`/`reject`/`off`), wired by
  `orchestrator.api.main._resolve_simulator_config`.
- Fault injection: `ChaosEngine` (`adapters/tcp/chaos.py`) — latency, drop,
  malformed, partial — already merged per-reply in `_handle()`.
- Live-session metadata: `peers()` + `_conn_meta`, surfaced by `GET /peers`.
- Lifecycle + registry: `start()/stop()/serve_forever()`, the file-backed
  `SimulatorStore`, and dispatch via `orchestrator.api.main._responder_for()`
  (keyed on `protocol`/`kind`).

The outbound leg is the genuinely new part. `TcpAdapter`
(`adapters/tcp/adapter.py`) is built for the *persistent, multiplexed,
correlated* exchange use-case (one socket reused for a whole run, a background
reader correlating responses by key). A transparent proxy wants the opposite: a
**1:1 socket pair** per inbound connection, with frames relayed in order and no
correlation logic of its own.

### Forces

- Reuse the responder's framing/decode/validate/chaos rather than fork a second
  TCP stack.
- A passive tap must forward bytes **byte-for-byte**; decode is for observation
  only and must never reshape the forwarded frame.
- Capture can be high-volume; it must be bounded and must respect the Secrets
  Vault posture (PANs/keys appear in the clear on the wire).
- Keep the single-host, dependency-free dev/test path (in-memory, no Redis).
- Don't entangle this with the participant-flow/topology fleet work (ADR-0001);
  a proxy is a single listener with a single upstream, not a hosted network.

## Decision

Add a **forwarding mode** to the TCP responder family as a subclass
`TcpProxy(TcpResponder)`, selected by `kind: "proxy"` in
`orchestrator.api.main._responder_for()`. It reuses the responder's framing,
decode, validation, chaos, and `peers()`/`stats()` surface, and overrides only
the per-frame handling: instead of resolving a reply locally, it relays the frame
to a per-connection **upstream** socket and relays the upstream's frames back to
the client.

Three operating modes on one mechanism, set by `mode`:

- `tap` (default) — relay both directions untouched; capture decoded views of
  each frame into a bounded capture store.
- `intercept` — same relay, but run the existing rules/chaos against frames in
  flight to mutate, inject, delay, drop, or corrupt them.
- `stub` — try the rules first; forward to upstream only when no rule matches
  (or when the upstream is unreachable).

Capture is written to a new, bounded **`ProxyCaptureStore`** (file-backed, same
shape conventions as `SimulatorStore`) and exposed read-only over the
orchestrator API; a follow-up "save as scenario" action converts a captured
session into a scenario/starter flow. `GET /peers` grows a real `outbound` list
populated from the proxy's upstream sockets (today it is hard-coded `[]`).

Fall back to the existing terminate-only behaviour when no `upstream` is
configured, so nothing about current simulators changes.

## Config shape

```jsonc
{
  "kind": "proxy",
  "host": "0.0.0.0", "port": 7000,          // listen side (reuses responder keys)
  "protocol": "iso8583",
  "framing": { /* same keys as adapter/responder */ },
  "message_format_id": "visa-base1",         // optional: decode + validate inbound

  "upstream": {                              // NEW — the real host to relay to
    "host": "10.0.1.50", "port": 7000,
    "connect_timeout_sec": 10,
    "reconnect": true                        // re-dial on upstream drop
  },

  "mode": "tap",                             // tap | intercept | stub

  "capture": {                               // NEW — bounded recording
    "enabled": true,
    "max_frames": 5000,                      // ring buffer cap per session
    "store_raw": false,                      // keep raw bytes (hex) or decoded only
    "redact": true                           // apply Secrets Vault masking to capture
  },

  // intercept/stub reuse the existing responder vocabulary unchanged:
  "rules":  [ /* when/respond — mutate or answer matched frames */ ],
  "chaos":  { /* latency/drop/malformed/partial on the relayed leg */ },
  "validate": { "mode": "warn" }
}
```

## Mechanism

Per accepted inbound connection (`_handle` override):

1. Open the upstream socket (`asyncio.open_connection`) using `upstream.*`.
   Register the pair in `_conn_meta` (client peer) and a new `_upstream_meta`
   (upstream peer) so both legs are linkable in `/peers`.
2. Run two relay pumps concurrently — client→upstream and upstream→client — each
   reading whole frames with the existing `_read_frame()` and re-framing with
   `_frame()`:
   - `tap`: decode for capture, forward the original frame untouched.
   - `intercept`: decode, run `_validate()` + `_resolve()`/`ChaosEngine`; if a
     rule/chaos applies, emit the mutated frame (or drop/delay/corrupt), else
     forward untouched.
   - `stub`: if a rule matches, answer the client directly (no upstream hop);
     otherwise forward.
3. On either side closing (or `IncompleteReadError`), tear down both sockets and
   pop both meta entries — mirroring the responder's existing `finally` cleanup.
4. Every observed frame (subject to `capture.max_frames`) is appended to the
   session's capture buffer with direction, timestamp, decoded view, and — only
   if `store_raw` — the hex bytes, after redaction.

Reuse note: the upstream leg is deliberately **not** `TcpAdapter`. That adapter's
correlation/keepalive/sign-on machinery assumes PayProbe owns the message
semantics; a transparent proxy must preserve the client's own ordering and
correlation, so it uses raw paired sockets and the shared framing helpers only.

## Options Considered

### Option A — Subclass `TcpProxy(TcpResponder)`, dispatched by `kind: "proxy"` (chosen)

| Dimension | Assessment |
|---|---|
| Reuse | High — framing, decode, validate, chaos, peers, store, lifecycle all inherited. |
| Blast radius | Low — new subclass + one `elif` in `_responder_for`; terminate path untouched. |
| Consistency | Matches how `payshield`/`visa`/`cybersource` simulators already plug in. |
| Risk | Tap must guarantee byte-exact forwarding despite sharing decode helpers. |

### Option B — A `forward`/`upstream` action inside the existing rules engine

Let a rule say `{"respond": {"forward": true}}` so the current `TcpResponder`
forwards matched messages. Smallest code change, but conflates "answer locally"
with "be a wire" — the per-frame loop in `_handle()` is reply-shaped (decode →
resolve → encode → write) and has no place to hold a long-lived upstream socket
or relay the reverse direction. Rejected: wrong seam for a bidirectional pump.

### Option C — A standalone proxy service / new package

Cleanest separation, but forks the framing and protocol codecs (or imports them
across a new package boundary) and duplicates the simulator lifecycle, store,
metrics, and `/peers` plumbing. Too much new surface for a capability that is
90% reuse. Rejected for v1; revisit only if proxy needs the load-fleet
horizontal scale-out (then it follows ADR-0001's worker-role pattern).

## Consequences

**Positive**

- A new posture with little new code: tap/record/intercept on top of proven
  framing, validation, chaos, and session plumbing.
- Record & replay makes real conversations into regression scenarios — unique to
  this mode.
- `GET /peers` finally reports the `outbound` leg it currently stubs as `[]`.

**Negative / risks**

- The proxy sees plaintext PANs/keys. `capture.redact` (Secrets Vault masking)
  must be **on by default**, and `store_raw` gated behind an explicit opt-in;
  capture-at-rest should use `SecretBox` like other secret stores.
- A bug in the relay pump can corrupt a live exchange in a way a terminate-only
  simulator never could. `tap` mode must be provably pass-through (golden-frame
  tests: bytes in == bytes out).
- TLS is **out of scope for v1** — transparent MITM of TLS needs cert
  handling/re-encryption. v1 is cleartext links only; document this loudly.
- Capture volume: enforce `max_frames` ring buffer and a global cap; never grow
  unbounded.

## Rollout / sequencing

1. `TcpProxy(TcpResponder)` with `mode: "tap"` + raw paired-socket relay; golden
   pass-through tests.
2. `ProxyCaptureStore` (bounded, redacted) + read-only capture API + `/peers`
   outbound leg.
3. "Save capture as scenario/starter flow."
4. `intercept` mode (reuse rules + `ChaosEngine`) and `stub` fallback.
5. TLS termination/re-encryption (separate ADR).

## Open questions

- Where does capture live durably — reuse the run/trace store, or a dedicated
  `ProxyCaptureStore`? (Leaning dedicated, to keep run history clean.)
- Should `intercept` mutations be recorded as a before/after diff in the capture
  for auditability? (Probably yes — cheap and high-value.)
- Portal: does the Live Sessions page (`/peers`) host the two-leg inspector, or
  is proxy capture its own page next to Simulators?
```
