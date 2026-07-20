# Deploy + run the connection/default-connection migration

Concrete steps to take this branch live and run the data migration. All of it is
backward-compatible and reversible; the data steps are **dry-run by default**.

The flags (`PAYPROBE_CONNECTION_OVERRIDE_WINS`, `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION`)
already default **on** in code, so a rebuild activates them — no compose changes needed.

## 0. Pre-flight (local)

```bash
make test-scenario      # scenario-service suite
make test-orchestrator  # orchestrator suite
# (5 orchestrator failures are pre-existing env gaps: missing 'jwt' pkg + payShield
#  crypto — unrelated to this change. Everything else is green.)
make portal-build       # production Angular build — must succeed
```

## 1. Build + restart the changed services

Only three services carry this change: scenario-service (endpoints + store),
orchestrator (resolution), portal (editor).

```bash
cd infra/docker
docker compose build scenario-service orchestrator portal
docker compose up -d   scenario-service orchestrator portal
```

## 2. Smoke-check

```bash
curl -fsS localhost:8000/ready          # scenario-service
curl -fsS localhost:8100/health         # orchestrator
```

> **Auth:** with `PAYPROBE_ENV=dev` (the compose default) the `/admin/migrate/*`
> endpoints are open. In a hardened deployment (`PAYPROBE_ENV != dev`) add
> `-H "Authorization: Bearer <token>"` (a JWT from the portal login / auth-service)
> to every call below.

## 3. Run the migration — dry-run first, then apply

Your live data was verified a **no-op** for the connection/env migration (already on the
override-matrix model, `safe_to_flip`), so only the default-connection steps below
actually change anything. Confirm that's still true:

```bash
curl -s -XPOST localhost:8000/admin/migrate/collisions | jq .safe_to_flip   # expect: true
```

### 3a. Seed the default connections

```bash
# dry-run: review what it would create (expect one: tcp_iso8583, from local-env)
curl -s -XPOST localhost:8000/admin/migrate/seed-default-connections | jq '.create'
# apply
curl -s -XPOST 'localhost:8000/admin/migrate/seed-default-connections?apply=true' | jq '{created,failed}'
```

Resolution is already on by default, so from now on a bare-target step (one with no chosen
connection) routes to its type's default connection.

### 3b. Slim the environments (optional — removes the now-redundant inline adapter)

```bash
# dry-run: should propose removing local-env's inline tcp_iso8583 (the default now owns it)
curl -s -XPOST localhost:8000/admin/migrate/slim-environments | jq '{remove,blocked}'
# apply
curl -s -XPOST 'localhost:8000/admin/migrate/slim-environments?apply=true' | jq '{environments_changed,blocked}'
```

If anything shows under `blocked`, leave it — that type isn't fully covered yet; reconcile
the connection/env values first, then re-run.

## 4. Verify

- `tcp_iso8583` now appears in the connections list with `default: true`.
- `local-env`'s `adapters` block no longer contains `tcp_iso8583` (if you ran 3b).
- Re-run a representative scenario and confirm it still passes — e.g. the ISO 8583
  purchase+reversal scenario, which is bound to the `outgoing` connection and is therefore
  unaffected either way.

(Ask me to verify these via the live read tools once you've deployed — I can diff the
connections/environments and confirm the default appears + the env slimmed.)

## 5. Rollback

Each step reverts independently:

- **Resolution:** set `PAYPROBE_DEFAULT_CONNECTION_RESOLUTION=0` on the orchestrator and
  restart — bare targets go back to the env adapter.
- **Precedence:** set `PAYPROBE_CONNECTION_OVERRIDE_WINS=0` to restore legacy env-wins.
- **Slim (3b):** re-add the `tcp_iso8583` block to `local-env` (Environments page or
  `PUT /environments/local-env`); the default connection still holds the same config.
- **Seed (3a):** delete the `tcp_iso8583` default connection if unwanted.

Nothing here is destructive beyond 3b, and 3b is reversible because the default connection
carries the same configuration that was removed from the environment.
