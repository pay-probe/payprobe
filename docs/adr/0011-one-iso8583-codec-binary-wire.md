# ADR-0011: One ISO 8583 codec, one field dictionary, binary on the live wire

**Status:** Proposed — phases 0 to 4 built 2026-09-24 on branch
`feature/adr-0011-iso8583-codec` (see Implementation status); phase 5 (the
`PAYPROBE_ISO8583_FORMAT_ENCODING` flip after a real-environment run, plus the
doc flips) owed. Accepted only after that flip.
**Date:** 2026-09-24
**Deciders:** PayProbe maintainers (David + reviewers)
**Extends:** `docs/history/standards-gap-analysis.md` recommendation #1
("Binary ISO 8583 codec path"); research-frontier ambition 1 in
`.claude/skills/payprobe-research-frontier/SKILL.md`

## Context

PayProbe speaks ISO 8583 in three places and each has its own codec. All
facts below were verified against the working tree on 2026-09-24.

| Codec | Where | Encodings | Consumers |
|---|---|---|---|
| Worker wire codec | `packages/worker/adapters/tcp/iso8583.py` (`iso_pack` / `iso_unpack` / `iso_validate`, `DEFAULT_FIELDS`) | ASCII only, by its own docstring ("Self-contained ASCII ISO 8583 codec") | `TcpResponder._decode/_encode` (`responder.py`), `Iso8583Protocol` in `protocols.py` (used by `TcpAdapter`), the proxy tap (`proxy.py`), the NATS `iso8583` codec (`adapters/nats/codecs.py`), `VisaSimulator` via `VISA_FIELDS` |
| Scenario-service step codec | `packages/scenario-service/models/iso_catalog.py` (`iso_pack` / `iso_unpack`, `ISO8583_CODEC_SRC`) | ASCII only | Pasted **as source text** into the catalog's `iso8583_pack` / `iso8583_parse` code-step templates (`_ISO8583_PACK`, `_ISO8583_PARSE`), so every scenario built from the catalog carries its own copy and runs it in the code sandbox |
| Analyzer codec | `packages/scenario-service/models/iso8583_analyzer.py` (`iso_pack_bytes`, `_dec_field_bytes`, `resolve_encoding`) | ASCII **and** binary: five axes (`bitmap: hex|binary`, `numeric: ascii|bcd`, `text: ascii|ebcdic`, `binary: hex|raw`, `length: ascii|bcd`), EBCDIC via cp037 | `POST /iso8583/analyze` and `/iso8583/build` (Inspector, MCP `iso8583_analyze` / `iso8583_build`), offline messages only |

So the platform can already *describe* a binary message (Inspector) but cannot
*send or receive* one. That is the highest-impact gap in the standards
analysis: production switches use binary bitmaps, packed-BCD numerics,
EBCDIC text and binary or BCD length prefixes, and none of that reaches a
socket today.

Four smaller defects sit next to the gap and cannot be fixed cleanly while the
codecs are separate:

1. **The field dictionaries have already drifted (measured).** Loading both
   tables side by side on 2026-09-24:

   | Table | DEs | Has `type` class |
   |---|---|---|
   | worker `DEFAULT_FIELDS` | 22 | none |
   | scenario-service `ISO8583_1987_FIELDS_SRC` | 29 | all |

   DE 5, 6, 15, 19, 43, 53 and 64 exist only in the scenario-service table.
   Every one of the 22 shared entries differs (the worker copy has no `type`),
   which means `iso_validate`'s charset check is silently a no-op for any
   simulator that is not bound to a registered format. `VISA_FIELDS` layers a
   third hand-copied table on top. This is exactly the per-service-copy drift
   invariant #3 was written to stop for the assistant tool layer.

2. **Two `encoding` knobs that can disagree silently.** `Iso8583Protocol`
   (`protocols.py`, `self.encoding = config["framing"]["encoding"]`) and
   `TcpResponder` (`responder.py`, same key) read `framing.encoding` as a
   *Python text codec name* and call `body.decode(self.encoding)`. The
   MessageFormat model carries its own `definition.encoding` (every builtin
   says `"ascii"`, and `Iso8583BuildRequest.encoding` already accepts the
   analyzer's profile or dict). The orchestrator's simulator format resolver
   (`_resolve_simulator_format` in `orchestrator/api/main.py`) injects the
   bound format's `fields` and `presence` into the simulator config and
   **drops its `encoding`**. A format declared binary therefore binds as ASCII
   with no warning.

3. **Validation and chaos assume ASCII bytes.** `iso_validate` compares
   `len(str)` against the spec `length`, which is wrong once a numeric field
   is packed BCD (half the bytes) or a `b` field is raw (the spec length is in
   hex characters, the wire carries half). `ChaosEngine.malform` in
   `chaos.py` mode `bad_mti` overwrites the first four body bytes with
   `b"9999"`; under a binary profile the MTI is two BCD bytes, so the fault
   also clobbers the first two bitmap bytes and stops meaning "bad MTI".
   `iso_unpack` `break`s at the first DE the spec does not define and returns
   what it has, so a truncated parse looks like a short message.

4. **Framing knows two length encodings.** `framing.py` has
   `LENGTH_ENCODINGS = ("binary", "ascii")` (plus TPDU prefix support). The
   third common form, a packed-BCD length (`0x00 0x43` for 43), is missing.
   Small, same file, but it belongs in the same change because a BCD-length
   host is typically also a BCD-body host.

**Why now.** The next candidate ADRs (scheme certification packs with
requirement traceability, MAC on DE 64/128, EMV DE 55 awareness in the worker)
all certify less, or make no sense, over an ASCII-only wire. This ADR is the
prerequisite, and it is falsifiable by byte comparison.

### What we can reuse

- **The analyzer's byte codec is the binary implementation.** `iso_pack_bytes`,
  `_dec_bitmap`, `_dec_field_bytes`, `_enc_len_bytes` and `resolve_encoding`
  already implement all five axes with tests in
  `packages/scenario-service/tests/test_iso8583_analyzer.py`. They move; they
  are not rewritten.
- **Framing is done.** Binary and ASCII length prefixes, byte order,
  `length_includes_header`, TPDU in/out are in `framing.py` and shared by
  adapter, responder, proxy and chaos. Only the BCD variant is added.
- **The injection hook exists.** `_resolve_simulator_format` is the one place
  a bound format reaches a running simulator. Adding `encoding` beside
  `fields` / `presence` is one line plus the precedence rule.
- **The shared-package precedent exists.** `payprobe_common` already homes
  `crypto`, `agent_toolkit`, `rest_backend` and `llm_provider`; scenario-service
  `crypto.py` is a re-export shim over `payprobe_common.crypto` (roadmap item
  6, done 2026-07-07). The hatch layout (`only-include`, `dev-mode-dirs = [".."]`)
  and the `packages/` on `PYTHONPATH` rule mean a new subpackage needs no new
  install mechanics. The worker and load-worker images build from the repo
  root (`packages/worker/Dockerfile`, compose `context: ../..`), so they can
  copy `packages/payprobe_common`.
- **Executable documentation guards the ASCII path.** Every scenario under
  `examples/scenarios/` runs in `make test`; the six ISO-adjacent suites
  (`test_iso8583_protocol`, `test_tcp_framing`, `test_chaos`,
  `test_visa_simulator`, `test_proxy`, `test_iso8583_analyzer`) pass 89 tests
  today (2026-09-24). Those numbers are the baseline every phase must keep.

### Forces

- **The worker must not import scenario-service.** Stated in the worker
  codec's docstring; also practical, since the load-worker image would drag
  FastAPI and the registries along. The shared home must be a leaf package.
- **ASCII behaviour must stay byte-identical.** Hundreds of saved scenarios,
  the showcase, the examples and the certification packs all pack ASCII.
  The default profile is `ascii`, and phase 1 ships with zero wire change.
- **Code steps cannot import platform modules (verified).** `run_code` in
  `worker/engine/code_runner.py` launches `sys.executable -I -c <bootstrap>`
  with `_child_env()`, which passes only `PATH` (plus `NODE_PATH`, locale
  keys). `-I` ignores `PYTHONPATH` and user site, so `import payprobe_common`
  fails inside a code step by design. The catalog templates therefore keep a
  pasted codec, and this ADR makes that paste *generated from* the shared
  module with a parity test, never a second hand-edited copy.
- **Length units must be defined once.** Spec `length` today means
  characters for `n` / `an` / `ans` / `z` and hex characters for `b`. Binary
  profiles change the byte count on the wire but must not change what a
  format author writes, or every custom format breaks.
- **Odd-length BCD needs a pad rule.** PAN (DE 2, `llvar n..19`) and track 2
  (DE 35) are odd-length in practice; hosts differ on left versus right pad
  nibble and on `D` as the track separator. This is a per-field concern, not a
  profile concern.
- **Reversibility.** Anything that changes what a bound format does to a
  running simulator lands flag-gated, default OFF, and is flipped only after
  a real-environment run.

## Decision

Extract one ISO 8583 codec into `packages/payprobe_common/iso8583/` and make
the worker, the scenario-service analyzer and the catalog templates consumers
of it. The shared package owns the field dictionaries as data (1987, 1993,
VISA Base I), the encoding profiles (`ascii`, `binary`, and the five-axis
dict), per-field encoding overrides, packing, unpacking and validation. The
worker's `adapters/tcp/iso8583.py` and the scenario-service modules become
re-export shims, the same way `scenario-service/api/crypto.py` wraps
`payprobe_common.crypto`.

The MessageFormat's `definition.encoding` becomes the single source of truth
for how a bound dialect is written to the wire. `_resolve_simulator_format`
injects it beside `fields` and `presence`. The legacy `framing.encoding` text
codec is folded on read: `"ascii"` maps to the ASCII profile, `"cp037"` and
`"ebcdic"` to `text: ebcdic`, anything else is refused. A config that carries
both and disagrees is a start-time error for simulators and a validator error
for scenarios, never a silent pick. `header_echo` keeps `framing.encoding` as
a real text codec because that protocol has no field model.

Validation, chaos and framing become profile-aware: `iso_validate` measures
logical length (digits, characters, or hex characters for `b`) regardless of
wire packing; `bad_mti` corrupts the MTI in whatever encoding the MTI is in;
`iso_unpack` reports a stopped parse as an explicit error with `truncated:
true` instead of returning a partial dict; `framing.py` gains
`length_encoding: "bcd"`.

When no profile is configured, behaviour is byte-for-byte what ships today.

## Target shapes

### Shared package

```text
packages/payprobe_common/iso8583/
  __init__.py       pack(), unpack(), validate(), resolve_encoding(), ...
  codec.py          the five-axis byte codec (moved from iso8583_analyzer)
  fields.py         ISO8583_1987, ISO8583_1993, VISA_BASE_I as dicts (data, no exec)
  profiles.py       ASCII_OPTS, BINARY_OPTS, per-field override merge
  tlv.py            parse_tlv / build_tlv (moved; the worker does not use it yet)
```

Public API (the only one consumers touch):

```python
pack(mti: str, values: dict, fields: dict, encoding=None) -> bytes
unpack(data: bytes, fields: dict, encoding=None) -> dict   # {mti, de_list, fields, error?, truncated?}
validate(mti, fields, de_values, de_list=None, presence=None, encoding=None) -> list[str]
resolve_encoding(encoding) -> dict                         # unchanged semantics
```

`pack` returns bytes under every profile. The worker's shim keeps the old
`iso_pack(...) -> str` / `iso_unpack(str)` signatures for the ASCII profile
so nothing outside the codec changes in phase 1.

### Field spec, with per-field override

```jsonc
"2":  {"name": "PAN", "len_type": "llvar", "length": 19, "type": "n",
       "encoding": {"numeric": "bcd", "pad": "right"}},        // odd PAN, F nibble on the right
"35": {"name": "Track 2", "len_type": "llvar", "length": 37, "type": "z",
       "encoding": {"numeric": "bcd", "separator": "D"}},
"52": {"name": "PIN Data", "len_type": "fixed", "length": 16, "type": "b"},   // 16 hex chars = 8 wire bytes under binary: raw
"55": {"name": "ICC Data", "len_type": "lllvar", "length": 255, "type": "b",
       "encoding": {"binary": "raw", "length": "binary"}}
```

Rules: `length` keeps today's unit (characters, hex characters for `b`);
the codec derives wire bytes from the resolved axes. A per-field `encoding`
overrides only the axes it names; the profile supplies the rest. `pad`
(`left` default, `right`) and `separator` (`D` default, `=`) apply to BCD
odd-length and track fields only.

### Precedence, folded on read

```text
MessageFormat.definition.encoding          (bound via message_format_id)
  > simulator / connection config "encoding"   (same shape: profile name or dict)
    > legacy framing.encoding                  (folded: ascii -> ascii profile, cp037/ebcdic -> text axis)
```

Two present values that resolve to different profiles are refused with a
message naming both keys. Inline `fields` still win over the bound format's
table, as today.

### Framing

`LENGTH_ENCODINGS = ("binary", "ascii", "bcd")`; `encode_length` /
`decode_length` / `length_capacity` gain the BCD branch. Chaos `bad_length`
already routes through these helpers and needs no change.

## Options Considered

### Option A — Shared codec in `payprobe_common`, format-level profile, shims in both services (chosen)

**Pros:** one implementation, one dictionary, one set of parity tests;
Inspector and wire cannot disagree by construction; the binary code already
exists and moves rather than being rewritten; matches the `crypto` and
`agent_toolkit` precedents; leaf dependency direction (worker and
scenario-service both point at common, never at each other).
**Cons:** the worker gains a dependency on `payprobe_common` (image builds
already copy the repo root, so this is a `COPY` line and a wheel
`only-include` entry, not a packaging redesign); the catalog code-step
template cannot import it (sandbox is `python -I`), so its paste is generated
and parity-tested rather than deleted.

### Option B — Port the binary axes into the worker codec, keep three copies

**Pros:** smallest diff, no cross-package move.
**Cons:** the drift measured above stays and now has a second axis to drift
on; the analyzer and the wire would be two binary implementations that must
agree byte for byte with no shared test; this is the per-service-copy pattern
that caused real bugs twice in the assistant tool layer. Rejected.

### Option C — Make the worker the owner and import it from scenario-service

**Pros:** no new package.
**Cons:** reverses the dependency direction (scenario-service images build
from the `packages/` context and do not ship the worker; the worker brings
uvloop, aiohttp and the adapters); the catalog and analyzer would import a
runtime engine to read a field table. Rejected.

### Option D — Adopt an external ISO 8583 library

**Pros:** someone else maintains BCD and EBCDIC.
**Cons:** `CLAUDE.md` already records that the PyPI `iso8583` name resolves to
an unrelated project whose sdist does not build; no external library carries
PayProbe's dialect model (presence matrix, `type` classes, per-integration
tables, TLV rows, chaos hooks); the dependency-free dev path would be lost.
Rejected.

### Sub-decision — which `encoding` wins

- **(i) Format wins, legacy key folded (chosen).** The format is the shape a
  client packs with and the simulator decodes with; that is what "bound
  dialect" already means for `fields` and `presence`.
- (ii) `framing.encoding` wins. Rejected: it is a text codec name and cannot
  express a bitmap or length axis.
- (iii) Delete `framing.encoding`. Rejected: `header_echo` uses it as a real
  text codec and saved configs carry it; fold, do not break.

## Trade-off Analysis

The decisive factor is invariant #3's history. Two hand-maintained copies of
the tool layer drifted twice and each drift became a user-visible bug; the
field-table measurement above shows the codec copies have already drifted the
same way, before binary even exists. Option B is cheaper this month and more
expensive every month after. Option A costs one packaging edit and one
sandbox question, both bounded.

Threading the profile through the format rather than through the connection
keeps the connection model untouched (ADR history in ATLAS §4: the connection
is the shape, the matrix is the values) and gives the Inspector, the
playground samples, the simulators and the scenario steps one place to agree.

Everything is additive until the precedence flip, which is one flag.

## Consequences

**Easier**

- Real switches with binary bitmaps, BCD numerics, EBCDIC text and BCD or
  binary length prefixes are reachable from a scenario, a simulator, the
  proxy tap, a NATS participant and the playground, with one configuration
  key.
- The Inspector decodes exactly what the wire carried; a capture from the
  proxy tap can be replayed as a golden test.
- The follow-on ADRs (MAC on DE 64/128, DE 55 in the worker, scheme packs
  with requirement IDs) have a byte-accurate substrate. `tlv.py` moving to
  common is the first step of the DE 55 one.
- One dictionary means the worker's charset validation finally runs for
  unbound simulators, and DE 5, 6, 15, 19, 43, 53, 64 become reachable from
  the worker.

**Harder / risks**

- **Silent ASCII regression.** Any byte drift in the ASCII path breaks saved
  scenarios. Mitigation: phase 0 golden tests are byte comparisons against
  today's output, recorded before any code moves, plus the examples suite.
- **The code-step paste stays a paste.** Because the sandbox is `python -I`
  with no `PYTHONPATH`, `ISO8583_CODEC_SRC` cannot become an import. It
  becomes the ASCII subset of the shared codec rendered to text by a small
  generator, and `test_iso_catalog_codec_parity` asserts the pasted source
  packs and unpacks the phase 0 golden set byte-identically to
  `payprobe_common.iso8583`. Code steps stay ASCII-only until a later ADR
  decides whether the sandbox should expose a vetted helper module; binary
  scenarios use the `tcp` step, which already runs in-process.
- **The precedence flip changes what a bound format does.** Today a custom
  format with a dict `encoding` is ignored on the wire. After the flip it is
  honoured. Gate: `PAYPROBE_ISO8583_FORMAT_ENCODING`, default `0` in phase 3,
  flipped to `1` in phase 5 after a real-environment run, kept as the escape
  hatch.
- **Odd-length BCD and EBCDIC MTIs vary by host.** The per-field override
  covers the known variants; unknown ones will surface as parity failures
  against real captures, which is the intended way to learn them.
- **Worker image size and build.** One more `COPY`. The load-worker rebuild
  note in compose applies (rebuild the pinned image after the change).

**Revisit**

- If a second scheme dictionary (Mastercard) lands, `fields.py` becomes a
  registry-backed load rather than constants; the builtins in
  `message_format.py` already reference the same data, so that is a data
  move, not a codec change.
- Wildcard per-MTI profiles (different encodings per message class on one
  link) are not modelled; no host in the gap analysis needs them.

## Rollout / sequencing

Each phase leaves `make test` green and ships independently. Baseline before
phase 1: record the full-suite count and the 89 ISO-adjacent tests.

**Phase 0 — golden tests that fail first.**
`packages/worker/tests/test_iso8583_golden.py`: a frozen set of logical
messages (seeded from `test_iso8583_analyzer.py` and `test_visa_simulator.py`)
with their exact ASCII wire bytes as produced by today's worker codec, and
their exact binary bytes as produced by today's analyzer under `"binary"`.
Both are recorded from the current code, not hand-written. These tests are
the contract for every later phase.

**Phase 1 — extract, no behaviour change.**
Create `payprobe_common/iso8583/` from the analyzer's byte codec and the
scenario-service field tables as plain dicts. Turn `worker/adapters/tcp/iso8583.py`,
`iso_catalog.py`'s tables and the analyzer's pack/unpack into re-exports.
Add the package to `payprobe_common/pyproject.toml` `only-include` and to
`packages/worker/Dockerfile`. Field-table parity test: worker and
scenario-service now expose the same object. Golden ASCII bytes unchanged.
Rollback: revert the commit; the shims are pure re-exports.

**Phase 2 — profile completeness, offline only.**
Per-field `encoding` override with `pad` and `separator`; `length_encoding:
"bcd"` in `framing.py`; profile-aware `validate` (logical lengths, `b` as hex);
`unpack` `truncated` error. All exercised through the Inspector endpoints and
unit tests. No wire path changes yet.

**Phase 3 — thread the profile through the live path, flag OFF.**
`Iso8583Protocol`, `TcpResponder`, `ProxyResponder` and the NATS `iso8583`
codec resolve a profile from (format, config, folded `framing.encoding`) at
start and call `pack` / `unpack` with bytes. `ChaosEngine.malform` receives
the profile for `bad_mti`. `_resolve_simulator_format` injects `encoding`
only when `PAYPROBE_ISO8583_FORMAT_ENCODING=1`; the disagreement check runs
regardless and only warns while the flag is off. Scenario validator gains the
same check. Golden binary bytes now assert on the worker codec output.

**Phase 4 — loopback proof (the falsifiable milestone).**
Add builtin format `iso8583-binary` (representative profile: binary bitmap,
BCD numerics, raw binary, BCD lengths; labelled a profile, not a scheme).
Bind `VisaSimulator` to it, run the flow set `test_visa_simulator.py` covers in
ASCII over a live socket, capture the wire, and assert
`iso8583_analyze(encoding="binary")` returns field values byte-identical to
what the sender packed. Pass/fail is byte comparison and exit codes. Playground
`samples.wire.iso8583` gains one binary sample.

**Phase 5 — flip and clean up.**
After one real-environment run (a binary host or the showcase network under
the binary format), flip `PAYPROBE_ISO8583_FORMAT_ENCODING` default to `1`,
keep the opt-out. Delete the exec'd `*_FIELDS_SRC` strings (the field tables
are data by phase 1); `ISO8583_CODEC_SRC` stays as the generated,
parity-tested paste. Flip `docs/history/standards-gap-analysis.md` ISO 8583 row from
"ASCII-only" to done, update `docs/simulators/visa-scheme.md`, the
`payments-domain-reference` skill's extension-point map, the research-frontier
skill's ambition 1 status, ATLAS §13 and this ADR's status.

## Action Items

- [x] Record baselines: `make test` count and the six ISO suites (89 on 2026-09-24).
- [x] Phase 0: `test_iso8583_golden.py` with recorded ASCII and binary bytes.
- [x] Phase 1: `payprobe_common/iso8583/{codec,fields,profiles,tlv}.py`; shims in
      `worker/adapters/tcp/iso8583.py`, `scenario-service/models/iso_catalog.py`,
      `scenario-service/models/iso8583_analyzer.py`; `pyproject.toml`
      `only-include`; `packages/worker/Dockerfile` `COPY`; parity test that
      `worker.DEFAULT_FIELDS is payprobe_common.iso8583.ISO8583_1987`;
      `ISO8583_CODEC_SRC` generated from the shared module +
      `test_iso_catalog_codec_parity` against the phase 0 golden set.
- [x] Phase 2: per-field override (`pad`, `separator`), BCD length prefix in
      `framing.py` (`test_tcp_framing.py`), profile-aware `validate`,
      `unpack` `truncated`.
- [x] Phase 3: profile resolution in `Iso8583Protocol`, `TcpResponder`,
      `ProxyResponder`, `nats/codecs.py`; `ChaosEngine.malform(bad_mti)` takes
      the profile; `_resolve_simulator_format` injects `encoding` behind
      `PAYPROBE_ISO8583_FORMAT_ENCODING`; disagreement check in simulator start
      and the scenario validator; `docs/operations/configuration.md` row.
- [x] Phase 4: builtin `iso8583-binary` format; loopback test
      `test_iso8583_binary_wire.py`; playground binary sample.
- [ ] Phase 5: flag default ON after a real-environment run; remove exec'd
      source strings; docs and skills updated; ADR status to Accepted.

## Implementation status

Built 2026-09-24, same day as the ADR, on `feature/adr-0011-iso8583-codec`
(three commits: the ADR, phases 0–1, phases 2–4). Everything below is verified
by tests in the branch; nothing is claimed beyond what they assert.

| Phase | What shipped | Proof |
|---|---|---|
| 0 | `packages/worker/tests/fixtures/iso8583_golden.json`: 8 spec-valid messages × 6 encodings recorded from the pre-ADR worker (ASCII) and analyzer (binary) codecs | `test_iso8583_golden.py` (96 parametrised byte comparisons + round trips) |
| 1 | `packages/payprobe_common/iso8583/` (`codec`, `fields`, `validate`, `tlv`, `portable`); worker `adapters/tcp/iso8583.py`, the analyzer and the catalog are re-exports; the code-step paste is generated from `portable.py`; `ISO8583_1987` (50 DEs) is the one dictionary (`iso8583-1987` builtin grew from 29 to 50 DEs; the worker default table gained `type` classes) | `test_iso8583_golden.py::test_one_field_dictionary_everywhere`, `scenario-service/tests/test_iso_catalog_codec_parity.py` |
| 2 | Per-field `encoding` overrides (`pad`, `pad_nibble`, `separator`), `length: binary`, optional `mti` axis, profile-aware `bad_mti`, `unpack` reports `truncated` + `error`, `framing.length_encoding: "bcd"` | `test_iso8583_golden.py`, `test_tcp_framing.py`, `test_chaos.py` |
| 3 | `wire_encoding_from_config` precedence + fold + refusal in `Iso8583Protocol`, `TcpResponder`, `ProxyResponder`, NATS codec; orchestrator injects the format `encoding` behind `PAYPROBE_ISO8583_FORMAT_ENCODING` (default `0`), refuses a disagreeing pair with 400 when on, warns when off | `orchestrator/tests/test_simulators.py` (6 new), `test_iso8583_binary_wire.py::test_disagreeing_encoding_keys_*` |
| 4 | `VisaSimulator` passes the ASCII suite's flow set over a live socket under `binary`; raw binary frames decode byte-identically with the shared codec; ASCII client vs binary host fails loudly; `iso8583-binary` builtin format; playground binary sample | `test_iso8583_binary_wire.py` (10 tests) |

Suite counts after phase 4 (2026-09-24, per package, `PYTHONPATH=packages`):
worker **601 passed / 6 skipped** (was 482 / 6), scenario-service **341**
(was 331), orchestrator **399** (was 393). `test_socket_registry.py`'s
reconnect test is intermittent in full-suite runs on this branch (passes 10/10
alone); it does not touch the codec and is tracked separately.

Two behaviour changes worth knowing beyond the ADR text: the wire path no
longer strips spaces out of decoded text fields (the old text path did, which
mangled a DE 43 with embedded spaces), and the catalog paste's `iso_unpack`
trims only leading/trailing whitespace for the same reason. The worker's
legacy `iso_unpack(str)` helper keeps the strip-everything convenience.

Settled open question: the MTI follows the `numeric` axis unless an `mti`
axis is given (`{"mti": "ebcdic"}` for hosts that write the MTI in EBCDIC
while numerics stay ASCII); length indicators have their own `length` axis
with an `ebcdic` value.

## Open questions

1. **Should code steps ever pack binary?** Today they cannot import the
   shared codec (settled above: `python -I`, no `PYTHONPATH`). Exposing a
   vetted helper module to the sandbox is a security decision (it widens
   what user code can reach) and is deferred to its own ADR; until then
   binary traffic goes through the `tcp` step.
2. **MTI and length-prefix text under EBCDIC.** The analyzer's `text: ebcdic`
   axis covers `an` / `ans` fields. Whether the MTI and ASCII-style length
   indicators are also EBCDIC on such hosts (they usually are) needs one
   real capture to confirm; default to "follows the text axis".
3. **Where the disagreement check lives for connections.** Simulators fail at
   start; scenarios fail in the validator. Connections have no start moment.
   Proposal: the connection validator plus a `/diagnostics` note, no new
   endpoint.
4. **Should `iso8583-binary` be user-visible or test-only?** Proposal:
   builtin and visible, clearly described as a representative profile, since
   the Inspector already exposes the same profile name.

## Notes

**Out of scope, recorded on purpose:** MAC generation and verification on DE
64 / 128 (next ADR; needs this substrate); DE 55 BER-TLV awareness in the
worker's rule matcher and the VISA ARQC check (`tlv.py` moves here so that
ADR is a consumer, not a mover); composite subfield tables (DE 48, 62, 63),
padding and justification rules, an ISO 8583:2003 table; spec-exact VISA
field semantics and scheme certification packs with requirement IDs; ISO
20022 XSD validation; TLS on the adapter (ADR-0008 territory).

**Depends on:** nothing unbuilt. **Blocks:** the MAC, DE 55 and scheme-pack
ADRs.
