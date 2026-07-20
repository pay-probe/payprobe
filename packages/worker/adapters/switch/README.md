# Switch Adapter

Connects to a payment switch via ISO 8583 over TCP.

## Config

```json
{
  "host": "10.0.1.50",
  "port": 7000,
  "protocol": "iso8583",
  "length_prefix": "2byte",
  "keepalive_interval_sec": 30
}
```

## Supported Actions

| Action | MTI | Description |
|---|---|---|
| `send_0100` | 0100 | Authorization request |
| `send_0400` | 0400 | Reversal request |
| `send_0800` | 0800 | Network management (echo test) |
| `query_switch_log` | — | Query switch transaction log via management API |

## Notes

Handles ISO 8583 message framing with 2-byte length prefix.
Maintains a keep-alive echo (0800/0810) on the configured interval.
