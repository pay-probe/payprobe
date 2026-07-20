# PayProbe — Standards Gap Analysis

How PayProbe's current capabilities map onto the industry standards that make a
payments/infrastructure testing tool *dependable*. Grounded in the codebase as of
2026-06-20, not aspiration.

There is no single standard that certifies a tool like PayProbe. Dependability
comes from three layers: (1) faithfully implementing the **message/protocol
standards** you test against, (2) mapping your **certification packs** to real
**scheme conformance specs**, and (3) running your own **test process** to a
recognised software-testing standard. This document scores each.

Legend: ✅ covered · 🟡 partial · ❌ missing

---

## 1. Message & protocol standards (what you test against)

### ISO 8583 — card transaction messaging
**Status: 🟡 partial (functional but ASCII-only)**

What exists: a configuration-driven codec (`iso_catalog.py`,
`iso8583_analyzer.py`) with MTI + primary/secondary bitmap, `fixed/llvar/lllvar`
length types, editable per-integration field tables, and shipped 1987 + 1993
field tables. The Inspector analyzes a wire message into MTI/bitmap/per-DE rows,
validates each field against its spec, and parses DE 55 EMV BER-TLV. A
validating builder packs `{DE: value}` maps.

Gaps to real-world conformance:

- **ASCII representation only.** Production switches commonly use **binary
  bitmaps**, **BCD/EBCDIC** numeric field encoding, and **binary length
  prefixes**. Today the codec assumes ASCII throughout — it will not interoperate
  with a binary-encoded host without new codec paths. *(highest-impact gap)*
- **No published-edition field dictionary.** Field tables are hand-maintained
  "1987-style" / "1993-style" rather than asserted against the published ISO
  8583:1987 / :1993 / :2003 data-element catalogs.
- **No DE-level format validation** beyond type/length (e.g. `n`, `an`, `ans`,
  `b` class rules; padding/justification conventions).

### ISO 20022 — modern financial messaging
**Status: 🟡 partial (XML build/parse, no schema validation)**

What exists: build/parse of ISO 20022 XML via stdlib, carrying
`message_type / namespace / root / template` in the format registry.

Gaps:

- **No XSD validation** against published message-definition schemas (e.g.
  `pacs.008`, `pacs.002`, `pain.001`, `camt.05x`). You can build a message; you
  can't yet prove it's schema-valid for a stated message set.
- **No message-set / version pinning** (the catalog year matters — e.g.
  `pacs.008.001.08` vs `.10`).

### EMV (EMVCo) — chip/contactless
**Status: 🟡 partial (rich tooling, no conformance mapping)**

What exists: BER-TLV parse/build, DOL parsing, Track 1/2, service code, AIP/TVR/TSI
decoding, Luhn, and a broad EMV tag dictionary (`emv_catalog.py`). EMV crypto
flow (`emv_crypto_catalog.py`): UDK derivation → session key → ARQC/ARPC
generate/verify on real DES/3DES.

Gaps:

- **No EMVCo L2/L3 conformance mapping.** The tooling models the flows but isn't
  tied to EMVCo test cases or golden test-card data sets.
- **DES/3DES only.** EMV and the schemes are moving to **AES** session keys;
  there's no AES cryptogram path.

---

## 2. Cryptography & key-management standards

**Status: 🟡 partial**

What exists: PKCS#11 HSM adapter (`hsm`), DES/3DES, PIN-block handling, EMV
ARQC/ARPC.

Gaps that matter for a payments tool claiming dependability:

- **ISO 9564** (PIN block formats) — PIN block exists, but format coverage
  (0/1/2/3/4, where format 4 is AES) isn't asserted against the standard.
- **ANSI X9.24 / DUKPT** — no derived-unique-key-per-transaction path, which is
  table-stakes for terminal/acquirer testing.
- **ASC X9 TR-31 / ISO 20038 key blocks** — no key-block wrap/unwrap; PCI PIN
  now effectively requires TR-31.
- **AES** — absent (see EMV above). DES is deprecated for new work.

---

## 3. Scheme conformance / certification frameworks (the "depend on it" part)

**Status: 🟡 partial (the machinery exists; the content is generic)**

What exists — and this is genuinely strong machinery: test-case **packs**
(`pack.py`) bundle cases that each map a *certification requirement* → a runnable
scenario; installing a pack imports it as a project; the orchestrator's
**certification report** scores a run (compliance %) and emits an HTML badge.
Packs already carry a `scheme` field (`visa | mastercard | generic | <processor>`).

The gap is content, not capability:

- **Built-in packs are generic mock cases**, not the actual scheme test suites.
  To be "dependable" in the certification sense, packs need to map to published
  cases from **Visa (VTS / ADVT / V.I.P.)**, **Mastercard (MTIP / M-TIP)**,
  **Amex / Discover**, or your processor's host-certification script.
- **No traceability export** (requirement-ID → case → result) in the form an
  acquirer/scheme reviewer expects.

This is the highest-leverage area: you already have the report/badge/coverage
engine — populating real scheme requirement IDs turns it from a demo into a
certification artifact.

---

## 4. PCI standards (how the tool handles sensitive data)

**Status: ✅ good posture, 🟡 not formalised**

What exists: connection configs hold only host/port/protocol/framing — **no
credentials**; keys/PINs/passwords live in a scoped, masked variables/secrets
mechanism resolved at run time; the connection-test endpoint never echoes config.
Test PANs are well-known non-live values (e.g. `4111…`).

Gaps:

- **No documented PCI DSS / PCI PIN / P2PE alignment** stating the tool never
  stores PAN/SAD, masks display, and uses only test cards. The behaviour is
  right; the attestation is missing.
- **No automated check** preventing a real PAN from entering a scenario/log.

---

## 5. The tool's own test process & quality (credibility of PayProbe itself)

**Status: 🟡 strong practice, not mapped to a standard**

What exists: one-command CI gate (`make test`, non-zero on failure),
every bundled scenario run end-to-end with enforced clear pass/fail, baseline
diffing against a pinned snapshot, three-phase gating, flaky-quarantine
convention, and execution traces (wire + engine log).

Gaps:

- **ISO/IEC/IEEE 29119** (software testing process/documentation/techniques) is
  the closest thing to "a standard for a testing tool." PayProbe's packs, traces,
  and reports already resemble its artifacts but aren't mapped to its
  vocabulary (test design, test case, test procedure, coverage items).
- **Output formats:** JUnit XML ✅ (CI-ready). No **TAP**; no machine-readable
  coverage of *which requirements* a run exercised (distinct from code coverage).

---

## Scorecard

| Standard / framework | Area | Status |
|---|---|---|
| ISO 8583 (1987/1993) | card messaging | 🟡 ASCII-only codec, no binary/BCD/EBCDIC |
| ISO 20022 | modern messaging | 🟡 XML build/parse, no XSD validation |
| EMV (EMVCo) | chip/contactless | 🟡 tooling rich, no L2/L3 conformance |
| ISO 9564 | PIN block | 🟡 partial format coverage |
| ANSI X9.24 (DUKPT) | key mgmt | ❌ missing |
| TR-31 / ISO 20038 | key blocks | ❌ missing |
| AES cryptograms | crypto | ❌ DES/3DES only |
| Visa/Mastercard conformance | certification | 🟡 engine yes, real cases no |
| PCI DSS / PIN / P2PE | data handling | 🟡 good posture, not attested |
| ISO/IEC/IEEE 29119 | test process | 🟡 strong practice, unmapped |
| JUnit XML / TAP | CI output | ✅ JUnit; ❌ TAP |

---

## Recommended priorities

Ordered by leverage (impact per unit effort), given what already exists:

1. **Binary ISO 8583 codec path** — biggest interoperability unlock. Add
   binary-bitmap, BCD/EBCDIC field encoding, and binary length prefixes alongside
   the current ASCII path, selectable per message format. Without this, real
   switches are out of reach.
2. **Real scheme packs + traceability export** — you already have the
   pack/certification/badge engine; populate one real suite (e.g. a Mastercard
   M-TIP subset) with requirement IDs and add a requirement→case→result export.
   This is what makes the certification report *mean* something.
3. **ISO 20022 XSD validation** — wire published schemas into the build/parse
   steps so a generated message can be asserted schema-valid for its message set.
4. **DUKPT + TR-31 + AES** — close the crypto gap; required for credible
   terminal/acquirer and modern PIN testing.
5. **ISO 29119 alignment doc + PCI test-data attestation** — low effort, high
   credibility: a short conformance statement mapping existing artifacts to 29119
   vocabulary, plus a stated no-live-PAN policy with an automated guard.
