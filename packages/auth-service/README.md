# PayProbe Auth Service

JWT authentication and user management. The single source of identity for the
platform: it issues short-lived HS256 JWTs that the orchestrator and
scenario-service verify with the shared `AUTH_JWT_SECRET`. Tokens carry `roles`
and `project_ids` so those services enforce RBAC and per-project isolation
locally, without a callback.

## Run

```bash
pip install -e ".[dev]"
AUTH_JWT_SECRET=dev-secret PAYPROBE_ENV=dev \
    uvicorn api.main:app --reload --port 8300
pytest tests/ -v
```

On first start it seeds an admin (`AUTH_ADMIN_USER`/`AUTH_ADMIN_PASSWORD`; in
dev, `admin`/`admin`). In prod, set those env vars or the service starts with no
users rather than a known-default account.

## Endpoints

| Method | Path              | Auth   | Purpose                              |
|--------|-------------------|--------|--------------------------------------|
| POST   | `/token`          | none   | password grant → `{access_token}`    |
| GET    | `/me`             | bearer | claims for the current token         |
| GET    | `/verify`         | bearer | introspection (`{active, claims}`)   |
| GET    | `/users`          | admin  | list users                           |
| POST   | `/users`          | admin  | create a user (roles, project_ids)   |
| DELETE | `/users/{name}`   | admin  | delete a user                        |
| GET    | `/health`         | none   | liveness                             |

## Getting a token

```bash
curl -s -X POST http://localhost:8300/token \
    -d username=admin -d password=admin | jq -r .access_token
```

Send it to any gated service as `Authorization: Bearer <token>`.

## Signing

HS256 with a shared secret (`AUTH_JWT_SECRET`) — simplest thing that works
across containers, and the verifiers already support it. **Hardening path:**
switch to RS256, keep the private key here, and publish the public key at
`/.well-known/jwks.json`; verifiers fetch it instead of sharing a secret. Only
`models/security.py` (`issue_token`/`decode_token`) and the verifier's key
source change.
