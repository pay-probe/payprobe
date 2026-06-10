# xPay Adapter

Connects to an xPay payment server via REST/HTTPS.

## Config

```json
{
  "base_url": "https://xpay.example.com",
  "api_key": "your-api-key",
  "timeout_ms": 5000,
  "pool_size": 20000
}
```

## Supported Actions

| Action | Payload | Response fields |
|---|---|---|
| `send_auth_request` | `amount`, `currency`, `card_profile` | `response_code`, `auth_code`, `rrn` |
| `send_reversal` | `rrn`, `amount` | `response_code` |
| `send_refund` | `original_rrn`, `amount` | `response_code`, `refund_rrn` |
| `query_transaction` | `rrn` | `status`, `amount`, `auth_code` |
| `void_transaction` | `rrn` | `response_code` |

## Response Codes

Standard ISO 8583 response codes apply. `00` = Approved.
