# HSM Adapter

Connects to a Hardware Security Module via PKCS#11.

Tested with: Thales payShield 10K, Utimaco SecurityServer.

## Config

```json
{
  "pkcs11_lib_path": "/usr/lib/libCryptoki2_64.so",
  "token_label": "HSM_SLOT_01",
  "pin": "your-pin",
  "pool_size": 50
}
```

## Supported Actions

| Action | Description |
|---|---|
| `verify_pin_block` | Verify an ISO 9564 encrypted PIN block |
| `generate_mac` | Generate a message authentication code |
| `verify_mac` | Verify a MAC |
| `check_key_status` | Check if a named key is present and active |
| `encrypt_data` | Encrypt a data block under a named key |

## Notes

HSM sessions are expensive. Keep `pool_size` low (default: 50).
The adapter never exposes key material — only MAC/verification results.
