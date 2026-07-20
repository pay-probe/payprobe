# PayProbe Scenario Service

CRUD API for test scenario management, validation, and versioning.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/scenarios` | List scenario summaries |
| POST | `/scenarios` | Create scenario (v1) |
| GET | `/scenarios/{id}` | Get latest version |
| PUT | `/scenarios/{id}` | Update — creates a new version |
| DELETE | `/scenarios/{id}` | Delete scenario + history |
| GET | `/scenarios/{id}/versions` | Version history |
| GET | `/scenarios/{id}/versions/{v}` | Get a specific version |
| POST | `/scenarios/{id}/versions/{v}/restore` | Copy old version forward as new |
| POST | `/validate` | Validate a draft (variable refs, assertions, duplicate ids) |
| GET | `/catalog` | Step catalog driving the portal palette |

Validation enforces unique step ids, `${step_xxx.response.field}` references
resolving to an *earlier* step, and operator/expected-value consistency.

Updates support optimistic locking: pass `base_version` in the save request
and a stale save is rejected with **409** + the current version. Omit it to
overwrite explicitly.

## Development

```bash
pip install -e ".[dev]"
uvicorn api.main:app --reload --port 8000   # portal dev server expects port 8000
pytest tests/ -v
```

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `DATABASE_URL` | `scenarios.db` | Postgres DSN → asyncpg store (production); anything else → SQLite path (`:memory:` for tests) |
| `SCENARIO_SEED_DIR` | `examples/scenarios/` | Seeded on first start when the store is empty |
| `CORS_ORIGINS` | `http://localhost:4200,http://127.0.0.1:4200` | Comma-separated allowed origins |
| `API_TOKEN` | _(unset = auth off)_ | If set, endpoints require `Authorization: Bearer <token>` |
| `MAX_BODY_BYTES` | `1048576` | Request size limit (413 above it) |

In docker compose the service runs against the shared PostgreSQL
(`scenarios` + `scenario_versions` tables from `infra/postgres/init.sql`);
deletes are soft (`is_active = FALSE`) so historical runs keep their
scenario references. Requests are logged as structured JSON via structlog.
