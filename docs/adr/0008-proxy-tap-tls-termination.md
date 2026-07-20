# ADR-0008: Proxy tap TLS termination (the deferred half of ADR-0002)

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** PayProbe maintainers (David + reviewers)
**Supersedes / extends:** the "TLS deferred" note in
[ADR-0002](0002-tcp-proxy-tap-forwarding-mode.md)

## Context

[ADR-0002](0002-tcp-proxy-tap-forwarding-mode.md) built the transparent TCP
proxy (`worker/adapters/tcp/proxy.py`) — a man-in-the-middle between a real
client and a real upstream, with three modes (`tap` / `intercept` / `stub`),
bounded redacted capture, and save-as-scenario. It shipped for **cleartext**
links only. Its rollout list ends with:

> 5. TLS termination/re-encryption (separate ADR).

This is that ADR. It closes the one remaining gap: today a payment link
protected by TLS cannot be tapped, intercepted, or stubbed, because both relay
legs read and write raw bytes. Real acquirer↔switch and switch↔issuer links
are frequently TLS (or mutual-TLS) in the environments PayProbe wants to sit
in front of, so "cleartext only" excludes a large share of the traffic the
proxy was built to observe.

### What the proxy already gives us

The proxy opens the two legs with the stdlib primitives that already accept a
TLS context, so TLS is an *argument*, not a rewrite:

- **Inbound (client → proxy):** the parent `TcpResponder.start()` calls
  `asyncio.start_server(self._handle, host, port)`
  (`responder.py:159`). `start_server` accepts `ssl=<SSLContext>` — passing a
  server context makes the proxy terminate TLS from the client.
- **Outbound (proxy → upstream):** `_handle` opens each upstream with
  `asyncio.open_connection(up_host, up_port)` (`proxy.py:186`).
  `open_connection` accepts `ssl=<SSLContext>` + `server_hostname` — passing a
  client context makes the proxy re-originate TLS to the real host.

Everything between the two legs — framing, decode, validation, chaos, capture,
`/peers` — operates on the already-decrypted frame and does **not** change.
TLS is purely a property of how each leg's bytes are read and written.

### Forces

- **Terminate-and-re-originate, not passthrough.** To tap or mutate frames the
  proxy must see plaintext, so it decrypts the client leg and encrypts the
  upstream leg independently. A byte-passthrough TLS relay (SNI routing, no
  decrypt) would satisfy neither `tap` capture nor `intercept`.
- **The two legs are independent.** Client-side TLS and upstream-side TLS are
  configured, enabled, and can fail separately — one may be TLS while the other
  is cleartext (terminate TLS, forward plaintext to a local sim, or vice
  versa).
- **Private keys are secrets.** The server leg needs a cert + private key; the
  client leg may need a client cert for mTLS and a CA bundle to verify the
  upstream. These are exactly the material the Secrets Vault + `SecretBox`
  exist for. They must never sit in a connection doc in plaintext or echo back
  through an API (invariant #8).
- **Interception of TLS is a capability with teeth.** Decrypting someone's
  live payment link is legitimate only against systems you own or are
  authorized to test. The feature must be honest about that and off by default.
- **Keep the dependency-free dev/test path.** `ssl` is stdlib; no new package.
  TLS stays opt-in so the cleartext loopback tests are untouched.

## Decision

Add an optional **`tls`** block to the proxy config with two independent
sub-blocks, `listen` (server leg, faces the client) and `upstream` (client
leg, faces the real host). Each is a thin description of an `ssl.SSLContext`
the proxy builds once at `start()` and passes to the existing
`start_server` / `open_connection` calls. No new class, no new socket stack —
one context per leg threaded into the two calls that already accept it.

Certificate/key **material is referenced, never inlined**: the config carries
`${secret.NAME}` / key-registry references that resolve server-side at launch
(the same `_attach_test_data` / material-endpoint path `${key.NAME}` already
uses), so private keys transit only the service-gated material endpoint and are
`SecretBox`-encrypted at rest. Capture is unaffected — it already records the
decoded plaintext frame, which is exactly what the proxy now sees after
termination.

When `tls` is absent, behaviour is byte-for-byte what ADR-0002 shipped.

## Config shape

Extends the ADR-0002 proxy config; every `tls` key is optional.

```jsonc
{
  "kind": "proxy",
  "host": "0.0.0.0", "port": 7000,
  "protocol": "iso8583",
  "upstream": { "host": "10.0.1.50", "port": 7000 },
  "mode": "tap",

  "tls": {
    "listen": {                       // server leg — proxy terminates client TLS
      "enabled": true,
      "cert": "${secret.PROXY_TLS_CERT}",     // PEM chain (ref, never inline)
      "key":  "${secret.PROXY_TLS_KEY}",      // PEM private key (SecretBox at rest)
      "require_client_cert": false,           // true ⇒ mutual TLS from the client
      "client_ca": "${secret.CLIENT_CA}",     // CA bundle to verify client certs
      "min_version": "TLSv1_2"                 // floor; default TLSv1_2
    },
    "upstream": {                     // client leg — proxy re-originates TLS to host
      "enabled": true,
      "verify": true,                          // verify the upstream cert (default true)
      "ca": "${secret.UPSTREAM_CA}",           // CA bundle (omit ⇒ system trust)
      "server_name": "switch.internal",        // SNI / hostname to verify (default: upstream.host)
      "client_cert": "${secret.PROXY_MTLS_CERT}",  // present a client cert (mTLS to upstream)
      "client_key":  "${secret.PROXY_MTLS_KEY}",
      "min_version": "TLSv1_2"
    }
  }
}
```

Notes:

- `tls.listen` and `tls.upstream` are independent — enable either, both, or
  neither. Terminate-TLS-and-forward-cleartext (to a local sim) and
  originate-TLS-from-a-cleartext-client are both valid and useful.
- `verify: false` on the upstream is the classic MITM-your-own-lab knob; it is
  **opt-in** and surfaced in `/peers` / capture metadata so a run's provenance
  records that verification was off (a report should never look clean when it
  skipped cert validation).

## Mechanism

1. **At `start()`**, if `tls.listen.enabled`, resolve `cert`/`key` (+ optional
   `client_ca`) via the material path, build a
   `ssl.SSLContext(PROTOCOL_TLS_SERVER)`, set `minimum_version`, load the
   cert chain, and set `verify_mode = CERT_REQUIRED` when
   `require_client_cert`. Pass it as `start_server(..., ssl=ctx)`.
2. **Per inbound connection**, if `tls.upstream.enabled`, build a
   `PROTOCOL_TLS_CLIENT` context (honoring `verify`, `ca`, `client_cert`),
   and open the upstream with
   `open_connection(host, port, ssl=ctx, server_hostname=server_name)`.
   Otherwise open cleartext exactly as today.
3. **Relay unchanged.** The two pumps see decrypted frames; framing, decode,
   validate, chaos, capture, and the `/peers` legs behave identically to the
   cleartext path — TLS lives entirely at the socket boundary.
4. **Failures are first-class results, not crashes.** A handshake failure
   (bad cert, verify failure, version floor) closes that connection with a
   structured reason recorded in the session metadata / capture, mirroring the
   proxy's existing `IncompleteReadError` teardown. A misconfigured context at
   `start()` (missing key material) fails the launch loudly, like any other
   unresolved reference.

Reuse note: TLS does **not** touch `TcpAdapter`. The adapter already dials
cleartext ISO links; giving *it* TLS is a separate, smaller change (it also
calls `open_connection`) and is out of scope here — this ADR is the proxy's
two legs only.

## Options considered

### Option A — `ssl.SSLContext` per leg, threaded into the existing calls (chosen)

| Dimension | Assessment |
|---|---|
| Reuse | Maximal — the two calls already take `ssl=`; relay/capture untouched. |
| Blast radius | Low — new `tls` config block + context build at `start()`; no new class. |
| Capability | Full terminate + re-originate, independent legs, mTLS both directions. |
| Risk | Key-material handling must go through the vault path; verify-off must be visible. |

### Option B — SNI-routing passthrough (no decryption)

Relay TLS bytes without terminating, routing by SNI. Trivial and preserves
end-to-end encryption — but the proxy never sees plaintext, so **`tap` capture
is empty and `intercept` is impossible**. It defeats the reason the proxy
exists. Rejected: it is a load balancer, not a tap.

### Option C — a dedicated TLS-terminating sidecar (stunnel / nginx stream)

Put a TLS terminator in front of the cleartext proxy. Zero PayProbe code, but
splits one listener into two processes, moves cert config out of the platform
(no vault integration, no `/peers` provenance, no per-run capture of which
cert/verify posture ran), and can't do per-connection upstream re-origination
with the proxy's own SNI/mTLS choices. Rejected for the product path; it stays
a valid *operator* workaround to document, not the built feature.

## Consequences

**Positive**

- Closes ADR-0002's last deferred item: the proxy works against the TLS and
  mTLS links that dominate real acquiring environments.
- No new dependency, no new class — TLS is a socket-boundary property; framing,
  decode, chaos, capture, and save-as-scenario are unchanged.
- Cert/key material flows through the existing Secrets Vault + `SecretBox`
  path, so invariant #8 holds for the newest secret type by construction.

**Negative / risks**

- **Decrypting a live payment link is powerful.** Ship it off by default,
  gate it behind the same fail-closed auth as everything else, and record the
  TLS posture (terminated? verify on?) in capture/`/peers` so a run can never
  look clean while it silently skipped verification. Docs must state the
  authorized-systems-only expectation plainly.
- Handshake edge cases (renegotiation, session resumption, ALPN, SNI
  mismatch) are new failure surfaces; each must degrade to a recorded,
  structured close rather than a relay crash.
- The proxy holds private keys in memory while running — the same trust model
  as the payShield test LMK, and acceptable for a testing tool, but worth
  stating.
- mTLS-to-upstream requires the operator to hold a client cert the real host
  trusts; that is an environment prerequisite, not something PayProbe can
  synthesize.

## Rollout / sequencing

1. `tls.upstream` (client leg): re-originate TLS to a real host, `verify`
   on/off, optional CA — the most common "tap a TLS link to my sandbox switch"
   case. Golden test against a throwaway TLS echo upstream.
2. `tls.listen` (server leg): terminate client TLS with a vault-referenced
   cert/key; capture shows decrypted frames.
3. mutual TLS both directions (`require_client_cert`, upstream `client_cert`).
4. Provenance surfacing: TLS posture in `/peers`, capture metadata, and the
   run/resilience report (so "verify was off" is visible in the verdict).

## Open questions

- Do we generate an on-the-fly CA + leaf for the `listen` leg (mitmproxy-style,
  for clients that pin to a test CA), or require an operator-supplied cert? V1
  leans operator-supplied; a dev-only self-signed helper could come later.
- Should `verify: false` require an explicit `"i_own_this_endpoint": true`
  acknowledgement in the config to make the intent unmistakable in the stored
  doc?
- Is upstream TLS a `tls.upstream` block on the proxy, or a property of the
  saved **Connection** the upstream points at (so any adapter reusing that
  connection inherits it)? Leaning proxy-local for v1 to avoid widening the
  connection schema before there is a second consumer.
