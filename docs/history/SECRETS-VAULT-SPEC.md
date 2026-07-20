# PayProbe Secrets Vault — Build Spec

A single, masked **inventory of every secret** PayProbe holds, plus the
encryption-at-rest health that protects them. Secrets already live across the
system — connection credentials, secret-marked scoped variables, and test-data
crypto keys — each encrypted at rest by the same `SecretBox`. What's missing is
**one place to see them all**: where each secret lives, whether it's actually
encrypted on disk, and a path to rotate it. The vault **never reveals plaintext**
— only a non-reversible fingerprint and metadata.

> **TL;DR.** Add `secret_refs()` to the Connection and Variable stores (masked
> owner/field/fingerprint, never values), aggregate them with the test-data keys
> into a `GET /secrets` endpoint that also reports `SecretBox` status and flags
> any secret stored as plaintext (key not set). Add a **Secrets** page under
> Manage: an encryption banner + grouped inventory, each row deep-linking to its
> owning manager to rotate. Rotation happens in the owning editor (full
> validation), not by a duplicate write path.

---

## 1. What already exists (don't rebuild it)

| Piece | Where |
|---|---|
| `SecretBox` — Fernet encrypt/decrypt, opt-in via `PAYPROBE_SECRET_KEY`, `is_secret_key()` field detection, `enabled` flag | `packages/scenario-service/api/crypto.py` |
| Connection secret fields (password/api_key/…) encrypted at rest | `api/connection_store.py` |
| Scoped variable secrets (`secret_names` per global/project/set) | `api/variable_store.py` |
| Test-data crypto keys (masked on read, fingerprint) | `api/test_data_store.py` ([[TEST-DATA-MANAGER-SPEC]]) |
| Existing managers (where rotation already works with validation) | portal `connections/`, `constructor/variables`, `test-data/` |

---

## 2. Backend

### 2.1 `ConnectionStore.secret_refs()`
Walk each stored connection; for every value under a key where `is_secret_key()`
is true, emit `{owner, field, fingerprint}` — `fingerprint` = `sha256(plaintext)[:8]`.
Never returns the value.

### 2.2 `VariableStore.secret_refs()`
For each scope (global/project/set) and each name in `secret_names`, emit
`{scope, scope_id, name, fingerprint}` (fingerprint of the current value, or empty
if unset).

### 2.3 `GET /secrets` (scenario-service)
Aggregate into one masked inventory + status:
```jsonc
{
  "status": { "encrypted_at_rest": true, "algorithm": "fernet" },  // SecretBox.enabled
  "entries": [
    { "source": "connection", "owner": "switch_visa", "field": "sign_on_key",
      "fingerprint": "9af3c012", "encrypted": true },
    { "source": "variable", "owner": "global", "field": "api_token",
      "fingerprint": "1b77…", "encrypted": true },
    { "source": "key", "owner": "bdk_primary", "field": "(material)",
      "fingerprint": "…", "encrypted": true }
  ]
}
```
`encrypted` mirrors `SecretBox.enabled` (a secret is only encrypted at rest when a
key is configured) — so a `false` here is the health warning: secrets are on disk
in plaintext until `PAYPROBE_SECRET_KEY` is set. Guarded by the existing
`require_auth` router. There is **no** endpoint that returns secret plaintext.

---

## 3. Portal — Secrets page

`src/app/secrets/` (service + component), under **Manage** at `/secrets`:

- **Encryption banner.** Green "Encrypted at rest (Fernet)" when enabled; amber
  "Not encrypted — set `PAYPROBE_SECRET_KEY`" with a short how-to when not.
- **Inventory**, grouped by source (Connections · Variables · Keys), each row:
  owner · field · fingerprint · a **Rotate** link that deep-links to the owning
  manager (`/connections`, `/variables`, `/test-data`) where the secret is edited
  with full validation.
- Read-only otherwise; values never shown.

---

## 4. Tests

`tests/test_secrets.py` (extend): `secret_refs()` masks values + emits fingerprints
for connections and variables; `GET /secrets` aggregates all three sources and
reports `encrypted_at_rest` matching the box state; no endpoint leaks plaintext.

## 5. Future increment (not this pass)

Inline rotate from the vault (per-source write paths) and rotation-age / "last
rotated" tracking. Deliberately deferred to avoid duplicating each store's
validated write path.
