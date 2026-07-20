# DB Probe Adapter

Read-only database adapter for cross-system assertions.
Used to verify that a transaction processed by the payment server
appears correctly in downstream system databases.

## IMPORTANT: Read-Only

This adapter **never writes** to any database. All queries must be SELECT statements.
Any attempt to execute a non-SELECT query will raise an error.

## Config

```json
{
  "engine": "postgresql",
  "host": "db-core.internal",
  "port": 5432,
  "dbname": "corebank",
  "user": "payprobe_readonly",
  "password": "your-password",
  "pool_size": 10
}
```

## Supported Engines

- `postgresql` (via asyncpg)
- `oracle` (via python-oracledb async)
- `mssql` (via aioodbc)

## Supported Actions

| Action | Payload | Description |
|---|---|---|
| `query_transaction` | `rrn` or `auth_code` | Look up a transaction by reference |
| `query_audit_log` | `entity_id`, `since` | Fetch audit trail entries |
| `assert_record_exists` | `table`, `where` | Assert a record exists matching criteria |
| `query_raw` | `sql`, `params` | Execute arbitrary read-only SQL |
