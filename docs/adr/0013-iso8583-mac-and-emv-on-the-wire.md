# ADR-0013: ISO 8583 message authentication (DE 64 / 128) and EMV DE 55 awareness on the wire

**Status:** Proposed
**Date:** 2026-09-25
**Deciders:** PayProbe maintainers (David + reviewers)
**Depends on:** [ADR-0011](0011-one-iso8583-codec-binary-wire.md) (the bytes
codec and one field dictionary), which named both items here as its first
follow-ons.

## Context

ADR-0011 gave PayProbe a byte-accurate ISO 8583 wire: binary bitmaps, BCD,
EBCDIC, raw binary fields, one codec for the worker, the Inspector and the
catalog. Two things real hosts do with those bytes are still missing, and both
were deliberately left out of 0011 so that it stayed falsifiable by byte
comparison alone. Facts below verified against `main` on 2026-09-25.

**1. Nobody computes or checks a MAC.** DE 64 and DE 128 are in the shared
dictionary as `b` fields ("Message Authentication Code", 16 hex chars), the
primitives exist in `packages/worker/engine/crypto_tools.py` (`retail_mac`,
ISO 9797-1 algorithm 3 with method-2 padding, RFC-vector-tested; `aes_cmac`,
RFC 4493 vectors), and the payShield simulator answers `M6` / `M8` (MAC
generate / verify) as host commands. But no adapter puts a MAC on an outbound
message, no responder verifies one on an inbound message, and no simulator can
be configured to require one. A switch that rejects unauthenticated traffic is
unreachable, and a certification pack cannot assert "the MAC was right".

**2. The worker cannot see inside DE 55.** `parse_tlv` moved to
`payprobe_common.iso8583.tlv` under 0011, but the worker never calls it: the
responder's rule matcher (`_match_condition` in `responder.py`) sees DE 55 as
one opaque hex string, so a rule cannot say "decline when 9F27 says AAC" or
"approve chip transactions only". `VisaSimulator.verify_arqc` shows the cost of
that: it compares the *entire* DE 55 value with a cryptogram and needs the
cryptogram input data supplied verbatim in its config (`cfg["data"]`), so it
verifies a fixed test vector, not the transaction it just received. Real
issuer behaviour, deriving the session key from an MDK + PAN + ATC and building
the data from the tags in the message, is one config block away with the
helpers `emv_icc_mk` and `emv_session_key` that already exist.

**3. Simulator keys are plaintext config.** `${key.NAME}` tokens resolve
against the test-data registry's service-gated material endpoint for
*scenarios* (`_attach_test_data` in `orchestrator/api/main.py`), never for
simulator configs. So the VISA simulator's `cvk` / `pvk` / `session_key`, and
any MAC key this ADR adds, would sit inline in the config that
`POST /simulators` receives and `simulator_store.py` writes to disk as plain
JSON. `is_secret_key` recognises `session_key` (suffix `_key`) but not `cvk`,
`pvk`, `mdk`, `bdk`. That is an invariant #8 gap the MAC work would widen if it
ignored it.

### What we can reuse

- The bytes codec: a MAC is a property of the wire bytes, and `pack` /
  `unpack` now hand those bytes over exactly. DE 64 is always the last field
  of a primary-bitmap message and DE 128 the last field overall, so "the bytes
  the MAC covers" is simply the message up to the MAC field.
- `retail_mac`, `aes_cmac`, `arqc`, `arpc`, `emv_icc_mk`, `emv_session_key`
  in `crypto_tools.py`, and `parse_tlv` / `build_tlv` in the shared package.
- The responder's `validate` block (`warn` / `reject`, DE 39 = 30 on reject)
  is the right home for "MAC mismatch": it is a dialect conformance failure.
- The `${key.NAME}` resolver and the material endpoint; `_resolve_simulator_config`
  is the one place a simulator config is prepared, so key resolution slots in
  beside format binding.
- The VISA simulator's `verify_*` blocks as the shape for opt-in crypto checks.

### Forces

- **Key material never inline, never echoed.** MAC and EMV keys must be
  `${key.NAME}` references resolved server-side at start; existing inline keys
  keep working (backward compatibility) but must be masked wherever a config is
  read back or persisted.
- **MAC coverage and encoding must be exact.** Retail MAC over the message
  bytes as they are on the wire (so the same message MACs differently under
  ASCII and binary, which is correct), 8-byte or 4-byte truncation (DE 64 /
  128 are 64-bit fields, so a full 16-byte AES-CMAC never fits; the first
  smoke test proved that by mis-slicing the coverage), and the MAC field's own
  encoding (`b`: hex text under ASCII, raw under binary).
- **The MAC is dialect data.** Which field, which algorithm, how long, is a
  property of the host dialect, like `encoding`. It belongs on the Message
  Format with a per-connection / per-simulator override, not on every step.
- **Verification failure is a decision, not an exception.** A responder must
  answer a bad MAC the way the host would (format error, or silence), never
  crash the connection; an adapter must report it on the step result.
- **payShield exists for a reason.** Some deployments want the MAC computed by
  the HSM (`M6` / `M8`), not in-process. That must stay possible later without
  redesign, but every message round-tripping through a second socket is not
  the default.
- **No parallel EMV stack.** DE 55 awareness is a small tag map on the decoded
  message plus rule conditions, reusing `parse_tlv`; not a new EMV kernel.

## Decision

**MAC.** Add an optional `mac` block to the ISO 8583 Message Format definition
(and, with the same shape, to a connection or simulator config, which wins):

```jsonc
"mac": {
  "field": 64,                      // 64 | 128 (must be the last field present)
  "algorithm": "retail_mac",        // retail_mac (ISO 9797-1 alg 3) | aes_cmac
  "key": "${key.SWITCH_MAK}",       // resolved server-side; inline hex still accepted, always masked
  "length": 8,                      // MAC bytes on the wire: 8 or 4 (the field is 64 bits; CMAC is truncated)
  "on_failure": "warn"              // warn | reject  (responder: DE 39 = 30; adapter: step fails)
}
```

Coverage is every wire byte before the MAC field. Packing writes a zero
placeholder, computes the MAC over the preceding bytes, and splices it in;
unpacking recomputes and compares. A shared, dependency-free module
`payprobe_common/iso8583/mac.py` owns the contract (spec validation, coverage
slicing, splice, compare) and takes the algorithm as a callable; the worker
supplies `retail_mac` / `aes_cmac` from `crypto_tools`. Verification failure is
a validation violation (`DE 64: MAC mismatch`) handled by the existing
`validate` block on the responder, and a `mac_verified: false` field plus, under
`reject`, a failed step on the adapter.

**Keys.** `_resolve_simulator_config` resolves `${key.NAME}` tokens in
simulator configs through the same material endpoint scenarios use. `cvk`,
`pvk`, `mdk`, `bdk`, `mak`, `mac_key` join the secret-name set so SecretBox and
the API masks treat them as secrets; the simulator store masks secret-named
values in what it returns and encrypts them at rest.

**EMV.** When DE 55 is present and parses, the decoded message carries
`emv: {tag: value}` (first occurrence, uppercase hex). Responder rules gain
`when.emv` with the existing condition vocabulary (`{"9F27": {"eq": "80"}}`),
the adapter's shaped response exposes the same map, and the VISA simulator's
`verify_arqc` verifies the cryptogram in tag 9F26 over data built from the
message's own tags (default CVN 10/18 order: 9F02 9F03 9F1A 95 5F2A 9A 9C 9F37
82 9F36 9F10; configurable), with the session key either given or derived from
an `mdk` via PAN (DE 2), PSN (5F34) and ATC (9F36). Optionally it returns an
ARPC (method 1, ARC = DE 39) in the response DE 55 as tag 91.

Nothing here changes a message that has no `mac` block and no DE 55.

## Options Considered

### Option A — `mac` as dialect data, computed in-process, keys by reference (chosen)

**Pros:** one config block enables the whole flow; MAC coverage is computed
from the bytes the codec just produced, so it is right under every profile;
keys flow through the existing service-gated path; verification reuses the
validate block. **Cons:** a second `mac` override site (format vs connection)
to keep coherent, same precedence rule as `encoding`; in-process only in this
ADR.

### Option B — Leave MAC to a `crypto` step in the scenario

Compute `retail_mac` in a step and place the hex in DE 64 by hand.
**Pros:** possible today for sending. **Cons:** the step cannot see the packed
wire bytes (the coverage), so it can only be right for a hand-built ASCII
message; nothing verifies inbound MACs; simulators cannot require one.
Rejected as the mechanism; it remains a valid teaching exercise.

### Option C — HSM-backed MAC through the payShield simulator (`M6` / `M8`)

**Pros:** realistic for deployments where the HSM holds the MAK. **Cons:**
every message round-trips a second socket; the responder gains a network
dependency; the payShield simulator is not always part of a network. Deferred:
the callable seam in `mac.py` is exactly where an HSM-backed algorithm plugs
in later, so this is a follow-on, not a rejection.

### Option D — Per-scheme MAC / EMV classes

A `VisaMacResponder`, a `MastercardMacResponder`. Rejected: the TestPay lesson
(ATLAS §10) and ADR-0009's "provider specifics live in data, never in a
per-provider adapter class". Scheme differences here are field, algorithm,
length and tag order, all data.

## Trade-off Analysis

The decisive factor is where the coverage bytes live. Only the codec knows
exactly which bytes precede DE 64 under a given profile, so the MAC must be
applied where packing happens and checked where unpacking happens; that rules
out Option B and pushes the contract into the shared package next to the codec.
Keeping algorithms in the worker (pycryptodome) and only the byte contract in
`payprobe_common` keeps the shared package dependency-free, which is what let
ADR-0011 land without packaging changes.

Resolving keys at simulator start rather than at request time costs nothing
and closes a real plaintext gap that predates this ADR.

## Consequences

**Easier**
- A host that requires a MAC becomes reachable; a simulator can require one; a
  certification pack can assert on `mac_verified`.
- Rules and assertions can reason about EMV tags; the VISA simulator behaves
  like an issuer host for chip transactions (verify ARQC, return ARPC).
- Simulator key material goes through the same masked, encrypted path as
  scenario keys.

**Harder / risks**
- **Coverage disagreement with a real host** (some hosts MAC a subset of
  fields, or exclude the TPDU). Mitigation: coverage is "all preceding wire
  bytes" in this ADR, stated plainly; a `coverage` option is the obvious
  extension and will be driven by a real capture, not guessed.
- **Reject-by-default would break existing simulators** that receive DE 64
  from clients that never MAC'd. Mitigation: `on_failure` defaults to `warn`,
  and nothing verifies unless a `mac` block exists.
- **Derived EMV keys need correct PAN/PSN/ATC handling**; a wrong derivation
  looks like a card fraud decline. Mitigation: the derivation path is tested
  against the existing ARQC vector in `test_payshield_sim.py` and the
  cryptogram data order is configurable.
- **Masking more names** (`cvk`, `pvk`, …) changes what the vault page and API
  reads show for existing saved simulators. That is the intended direction of
  invariant #8; the values are still usable, only never revealed.

**Revisit**
- HSM-backed MAC (Option C) once a network needs the HSM to hold the MAK.
- Partial-coverage MACs if a real host demands them.

## Rollout / sequencing

Every phase leaves the per-package suites green; nothing needs a flag because
each behaviour is opt-in by configuration.

**Phase 0 — vectors first.** `test_iso8583_mac.py`: retail MAC and AES-CMAC
over known ISO 8583 byte strings (from the ADR-0011 golden fixture) with
expected MACs computed once from `crypto_tools` and frozen; ARQC derivation
against the vector `test_payshield_sim.py` already uses.

**Phase 1 — the MAC contract.** `payprobe_common/iso8583/mac.py`:
`resolve_mac_spec`, `coverage(wire, spec, fields, encoding)`, `apply`,
`verify`; the worker's `iso8583.py` binds the algorithms. `Iso8583Protocol`
and `TcpResponder` apply / verify when a spec is present; responder failures
flow through the validate block; adapter results carry `mac_verified`.

**Phase 2 — keys by reference.** `${key.NAME}` resolution in
`_resolve_simulator_config`; secret-name additions; simulator store masks and
encrypts secret-named values; a test proves a saved simulator config never
returns a key in plaintext.

**Phase 3 — EMV awareness.** `emv` tag map on decoded messages, `when.emv`
rules, shaped-response `emv`, VISA `verify_arqc` on message tags with optional
MDK derivation and ARPC in the response.

**Phase 4 — proof and docs.** Loopback tests: adapter and simulator under
ASCII and binary with a retail MAC on DE 64 and on DE 128, tampered byte
detected, wrong key detected, `warn` vs `reject`; VISA chip flow approve /
decline on cryptogram. Docs: TCP adapter README, VISA simulator doc,
payments-domain-reference (MAC and EMV sections), config reference for the
new secret names; ADR to Accepted.

## Action Items

- [ ] Phase 0 vectors: `packages/worker/tests/test_iso8583_mac.py`.
- [ ] Phase 1: `payprobe_common/iso8583/mac.py`; `worker/adapters/tcp/iso8583.py`
      algorithm binding; `Iso8583Protocol.encode/decode`; `TcpResponder._encode/_decode/_validate`.
- [ ] Phase 2: `_resolve_simulator_config` key tokens; `payprobe_common/crypto.py`
      `_SECRET_EXACT` additions; `simulator_store.py` masking/encryption + test.
- [ ] Phase 3: `emv` map + `when.emv`; `VisaSimulator._arqc_ok` on tags, MDK
      derivation, ARPC tag 91.
- [ ] Phase 4: loopback suite, docs, ADR status.

## Open questions

1. **MAC field encoding under ASCII.** DE 64 is `b` (16 hex chars). Some
   ASCII hosts carry the MAC as 8 raw bytes even in an otherwise text message.
   The per-field `encoding` override from ADR-0011 (`{"binary": "raw"}`)
   already expresses that; confirm against a capture before documenting it as
   the norm.
2. **Where the connection-level `mac` override lives for load runs**: the load
   engine builds many connections from one config; the spec must be resolved
   once, not per worker. Check `load_coordinator` in phase 1.
3. **ARPC method 2 (CSU)** is implemented in `arpc`; whether the VISA
   simulator should default to method 1 or make it configurable is a data
   decision for the pack, not this ADR.

## Notes

**Out of scope, recorded on purpose:** HSM-backed MAC (Option C); partial
MAC coverage; PIN block translation / verification on DE 52 (a separate,
larger topic touching the payShield flows); a full EMV kernel or CDOL parsing
beyond the configurable tag list; scheme certification packs with requirement
IDs (the ADR after this one).
