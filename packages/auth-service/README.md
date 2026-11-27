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

**Demo login (public demos only).** `AUTH_DEMO_LOGIN=username:password` makes
`GET /demo-login` return those credentials to anyone, and the portal login page
pre-fills them with a "Demo workspace" notice. It names an existing account and
creates nothing. Unset (the default), the endpoint is a 404 and the login page
is unchanged.

**Sign-in by a trusted front door.** When an authenticating proxy (for example
APISIX's `openid-connect` plugin) already signs people in and checks their role
before any request reaches PayProbe, set `AUTH_PROXY_HEADER` to the header it
sets. `GET /proxy-login` then reads that header and issues the same token
`/token` does, and the portal login page signs straight in without showing its
form. Unset (the default), the endpoint is a 404.

| Variable | Default | Effect |
|---|---|---|
| `AUTH_PROXY_HEADER` | unset (off) | Header the proxy sets, e.g. `X-Userinfo`. |
| `AUTH_PROXY_HEADER_FORMAT` | `userinfo-base64-json` | `userinfo-base64-json`: base64 of the OIDC userinfo JSON (APISIX's `X-Userinfo`). `plain`: the header value is the username. |
| `AUTH_PROXY_USERNAME_CLAIMS` | `preferred_username,email,sub` | Userinfo claims tried in order; the first non-empty one is the username. |
| `AUTH_PROXY_ROLES` | `operator` | Roles a proxy-signed person gets. |
| `AUTH_PROXY_PROJECTS` | empty | `project_ids` a proxy-signed person gets. |

No stored user is needed. If a stored user of that name exists, its roles and
`project_ids` are used instead, so an admin can still tune one person. A
missing or undecodable header is a 401.

> **The header is trusted only because the proxy is the only way in.** Turn this
> on only when clients cannot reach the portal or auth-service around the proxy,
> and the proxy overwrites or strips any copy of the header a client sends.
> Otherwise anyone can sign in as anyone by setting the header themselves.

In the portal, a session that came from the proxy also sends the browser to the
proxy's logout path on sign-out (`proxyLogoutPath` in the portal environment,
default `/logout`), so it does not land on a login page that signs straight
back in.

## Endpoints

| Method | Path              | Auth   | Purpose                              |
|--------|-------------------|--------|--------------------------------------|
| POST   | `/token`          | none   | password grant → `{access_token}`    |
| GET    | `/demo-login`     | none   | demo credentials, 404 unless `AUTH_DEMO_LOGIN` |
| GET    | `/proxy-login`    | proxy header | token for the proxy-signed person, 404 unless `AUTH_PROXY_HEADER` |
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
