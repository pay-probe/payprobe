# PayProbe Test Data Manager — Build Spec

A central place to **define, generate, and reuse the data a payment test feeds**: card pools,
BIN ranges, terminal pools, and crypto keys. Today these values are scattered — inline `cards`/
`terminals` lists on a scenario doc, ad-hoc BINs typed into `${rand.pan(...)}`, and DUKPT/PVV key
material pasted into crypto nodes. The worker already *generates* and *consumes* this data
(`packages/worker/engine/generators.py`, `crypto_tools.py`); what's missing is a **surface to
manage and inspect it** the same way Connections, Tables, and Message Formats are managed.

> **TL;DR.** Add a file-backed **TestDataStore** in scenario-service (same low-write, single-doc
> pattern as Connections/Tables), expose CRUD + a BIN→PAN generate endpoint, and add a **Test
> Data** page to the portal's *Manage* group with four tabs. Key material is encrypted at rest via
> the existing `SecretBox` and **never revealed** on read (masked + fingerprint only). Phase 1
> (this spec, implemented) is the registry + generation + UI; Phase 2 wires named datasets into
> scenarios/load runs at runtime.

---

## 1. What already exists (don't rebuild it)

| Concept | Existing PayProbe piece | Where |
|---|---|---|
| Per-transaction generators (`${rand.pan(bin)}`, `${pool.card}`, `${pool.terminal}`, `${seq.stan}`) | `GeneratorContext` | `packages/worker/engine/generators.py` |
| Luhn-valid PAN construction | `luhn_check_digit`, `GeneratorContext._pan` | `packages/worker/engine/generators.py` |
| Card/terminal pools fed into a run | `scenario.get("cards")` / `scenario.get("terminals")` (inline lists) | `packages/worker/load_worker.py` (≈208) |
| DUKPT / PVV / PIN-block / CVV crypto | `crypto_tools.py` | `packages/worker/engine/crypto_tools.py` |
| File-backed named registry pattern | `ConnectionStore`, `TableStore`, `VariableStore` | `packages/scenario-service/api/*_store.py` |
| Encryption-at-rest for secrets | `SecretBox` (Fernet, opt-in via `PAYPROBE_SECRET_KEY`) | `packages/scenario-service/api/crypto.py` |
| Two-level Manage nav + registry pages | `NAV`, `connections.component` | `packages/portal/src/app/core/nav.model.ts`, `app/connections/` |

**Key realization.** Pools are currently *inline* on the scenario/load payload. The store doesn't
need to change the worker to be useful on day one: it lets an author build a pool/BIN range/key set
**once**, inspect it, and copy/resolve it into a scenario. Runtime by-name resolution is the Phase 2
follow-up (§6).

---

## 2. Data model

One JSON document (`TEST_DATA_FILE`, sibling of `connections`/`tables`; `:memory:` for tests),
four name-keyed collections. Names are slugged like every other registry.

```jsonc
{
  "card_pools":     { "<name>": { "description": "", "cards": ["4111111111111111", ...] } },
  "bin_ranges":     { "<name>": { "description": "", "bin": "411111", "length": 16, "brand": "visa" } },
  "terminal_pools": { "<name>": { "description": "", "terminals": ["TERM0001", ...] } },
  "keys":           { "<name>": { "description": "", "type": "bdk|ipek|pvk|zmk|generic", "value": "<hex>" } }
}
```

- **Card pool** — a named list of PANs (or arbitrary card tokens) for `${pool.card}`.
- **BIN range** — a BIN + length + brand; the source for generating Luhn-valid PANs on demand and
  for seeding a card pool. (`brand` is metadata/labeling only.)
- **Terminal pool** — a named list of terminal IDs for `${pool.terminal}`.
- **Key** — test crypto key material (BDK/IPEK for DUKPT, PVK for PVV, ZMK, or generic).
  `value` is hex. **Secret**: encrypted at rest, masked on read (see §4).

Validation: BIN must be numeric and shorter than `length`; `length` in 12–19; key `value` must be
valid hex of even length; `type` from the allowed set.

---

## 3. Backend — scenario-service

### 3.1 `api/test_data_store.py`
`TestDataStore(path, secret_box=default_box)` mirroring `ConnectionStore`: a `threading.Lock`,
atomic temp-file replace, `:memory:` short-circuit. Pydantic drafts per collection
(`CardPoolDraft`, `BinRangeDraft`, `TerminalPoolDraft`, `KeyDraft`) + named models. Methods per
collection: `list_* / get_* / upsert_* / delete_*`.

Key material is encrypted explicitly on save and decrypted on load (the field is `value`, not a
SecretBox heuristic name, so the store calls `self._box.encrypt/decrypt` directly). The in-memory
copy holds plaintext; the file holds `enc:v1:` ciphertext when `PAYPROBE_SECRET_KEY` is set.

### 3.2 Generation helper
A dependency-free `luhn_check_digit` + `generate_pan(bin, length, rng)` (lifted from the worker, so
scenario-service stays standalone) and `fingerprint(value)` = `sha256(value)[:8]` for key display.

### 3.3 Routes in `api/main.py` (mirror the `/connections` block)
```
GET    /test-data/card-pools                 list
GET    /test-data/card-pools/{name}          get
PUT    /test-data/card-pools/{name}          upsert
DELETE /test-data/card-pools/{name}          delete
  ... same CRUD for /bin-ranges, /terminal-pools, /keys ...
POST   /test-data/bin-ranges/{name}/generate?count=N&as_pool=<pool-name?>
                                             -> { "pans": [...] }   (Luhn-valid; optionally
                                                also upserts a card pool named <pool-name>)
```
Keys are **redacted** on `list`/`get`: return `{ name, description, type, length, fingerprint }`
and never the `value`. There is no read endpoint that returns key plaintext over the API.

Wire `TEST_DATA_FILE = _sibling_file("test_data")`, instantiate in startup
(`app.state.test_data = TestDataStore(TEST_DATA_FILE)`), guard with the existing `require_auth`
router. Operation IDs follow the camelCase convention (`listCardPools`, `generateBinPans`, …).

---

## 4. Secret handling (keys)

- **At rest**: `value` encrypted via `SecretBox` (same opt-in Fernet key as Connections).
- **On read**: masked — only `type`, `length`, and a non-reversible `fingerprint` (sha256 prefix)
  are returned, so a key can be identified/diffed without exposing material. Aligns with the
  separate *Secrets Vault* feature ("view/rotate, never reveal").
- **Write-only**: PUT accepts a new `value`; an empty/omitted `value` on update keeps the existing
  one (so editing description/type doesn't require re-pasting the key).

---

## 5. Portal — Test Data page

`src/app/test-data/` (service + standalone component), tabbed: **Card Pools · BIN Ranges ·
Terminal Pools · Keys**, built with signals like `connections.component`. Each tab is a
list + editor: create/rename/delete, and for BIN ranges a **Generate** action (count → PANs, with
a "save as card pool" option). Keys tab shows fingerprint/type/length only and a paste-to-replace
field. Register `/test-data` in `app.routes.ts` (title "Test Data") and add it to the **Manage**
group in `nav.model.ts`.

---

## 6. Phase 2 — runtime wiring (BUILT)

Scenarios/load runs now reference pools **by name** instead of inlining:

- `ScenarioDraft` gains `card_pool` / `terminal_pool` (string names; persisted by scenario-service).
- The orchestrator's `_attach_test_data` (in the `_resolve_run` assembly chain, so it covers both
  ordinary and load runs) resolves each referenced pool from the registry into inline
  `cards`/`terminals` on the scenario — best-effort, inline values win. The worker is unchanged: it
  still consumes inline `cards`/`terminals`.
- The worker's `ScenarioRunner` seeds a per-scenario `GeneratorContext` from the scenario's
  `cards`/`terminals`, so `${pool.card}` / `${pool.terminal}` resolve in **ordinary** runs too (load
  runs already seed one shard-wide context).
- Constructor settings panel has **Card pool** / **Terminal pool** pickers populated from the
  registry.

Still **not** wired (future): crypto nodes referencing a key by name (would need an authenticated
internal resolve endpoint — the only path that returns key material, never the browser).

---

## 7. Tests

`tests/test_test_data.py`: CRUD round-trip + slugging for each collection; BIN generate produces
the requested count of **Luhn-valid** PANs of the right length/BIN; `as_pool` creates a card pool;
key `value` is masked on read but recoverable in-memory; with a `SecretBox` key set, the on-disk
file contains `enc:v1:` ciphertext, not plaintext. Run the scenario-service suite green; build the
portal.
