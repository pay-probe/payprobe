# Load Test Runbook — configure & run

Exact steps to configure and run a PayProbe load test, using the P0 console features
(per-transaction data generators + DUKPT/PVV). Two paths: the **Portal UI** (normal) and the
**API** (automation / scale). Ports below are the compose defaults.

| Service | URL |
|---|---|
| Portal | http://localhost:8080 |
| Orchestrator API (load runs) | http://localhost:8100 |
| Scenario service | http://localhost:8000 |
| Redis (load bus) | redis://localhost:6379 |

---

## Step 0 — bring the stack up

```bash
cd infra/docker
docker compose up -d redis scenario-service auth-service orchestrator portal
# (add prometheus grafana if you want the live dashboards)
docker compose ps          # all healthy?
curl -s localhost:8100/status | jq    # orchestrator up
```

For a no-real-systems dry run, use the bundled **mock** environment (`examples/environments/mock.json`)
— it needs nothing external.

---

## Step 1 — define the environment

The environment tells the adapters where the system-under-test is. Start from
`examples/environments/grpc.json` (or `mock.json`) and edit host/port/TLS. Keep BDK/PVK/keys as
**secrets** (SecretBox), referenced later as `${vars.NAME}`.

You can pass the environment inline in the load request (Step 3, `environment`) or by name
(`environment_name`) if it's registered.

---

## Step 2 — author the transaction scenario (with realism)

This is the unit the load driver repeats. The P0 generators make every transaction carry **unique**
data; the crypto node produces a **real DUKPT PIN block**. Save it in the constructor, or pass it
inline as `scenarios: [ ... ]`.

```jsonc
{
  "id": "load-payment",
  "name": "payment_under_load",
  "stop_on_failure": true,
  "timeout_ms": 10000,

  // ${pool.*} draws from these (round-robin). Omit if you don't use pools.
  "terminals": ["TERM0001", "TERM0002", "TERM0003"],

  "variables": { "BDK": "${secret.ACQ_BDK}" },   // resolved from SecretBox

  "steps": [
    {
      "id": "pinblk",
      "kind": "crypto",
      "config": {
        "operation": "dukpt_pin_block",
        "bdk": "${vars.BDK}",
        "ksn": "FFFF9876543210E${seq.ksn(5)}",   // KSN counter advances per txn
        "pin": "1234",
        "pan": "${rand.pan(411111)}"             // Luhn-valid, unique per txn
      }
    },
    {
      "id": "auth",
      "target": "http",
      "action": "send_auth_request",
      "payload": {
        "pan":      "${rand.pan(411111)}",
        "stan":     "${seq.stan(6)}",            // 000001, 000002, ...
        "rrn":      "${now.rrn}",                // 12-digit, time-based
        "amount":   "${rand.amount(100,50000)}",
        "currency": "GEL",
        "terminal": "${pool.terminal}",
        "pin_block":"${pinblk.response.pin_block}",
        "ksn":      "${pinblk.response.ksn}"
      },
      "assertions": [
        { "field": "response_code", "operator": "eq", "expected": "00" }
      ]
    }
  ]
}
```

Generator tokens available anywhere in a payload: `${uuid}`, `${seq.NAME}` / `${seq.NAME(width)}`,
`${rand.int(a,b)}`, `${rand.amount(a,b)}`, `${rand.digits(n)}`, `${rand.hex(n)}`,
`${rand.pan(bin[,len])}`, `${now.rrn|epoch|iso}`, `${pool.terminal|card}`.
Crypto ops: `dukpt_ipek`, `dukpt_derive_key` (variant `pin|mac_req|mac_resp|data_req`),
`dukpt_pin_block`, `pvv`.

> Validate the scenario once as a normal run before loading it:
> `POST /runs {"scenarios":[ <scenario> ], "environment_name":"mock"}` and check it passes.

---

## Step 3 — launch the load run

### Path A — Portal (recommended)

1. Open **http://localhost:8080 → Load**.
2. Pick the **scenario** and **environment**.
3. Choose a **profile**:
   - **Steady** — hold a target TPS (set *Target TPS*, *Duration*).
   - **Ramp** — *Start TPS → End TPS* over *Ramp seconds* (find the knee).
   - **Spike** — *Base TPS* with bursts to *Spike TPS* every *N s* for *M s*.
   - **Soak** — hold *Connections* sending a heartbeat every *interval* (set *heartbeat action*).
4. Set **Workers** (1 for local; more to fan out — see Step 5).
5. Click **Launch**. The live panel streams TPS, p95/p99, errors, live/dead/reconnect.

### Path B — API

```bash
curl -s -X POST localhost:8100/load-runs \
  -H 'Content-Type: application/json' \
  -d '{
    "scenario_ids": ["load-payment"],
    "environment_name": "mock",
    "label": "payment-steady-500tps",
    "type": "steady",
    "target_tps": 500,
    "duration_s": 120,
    "workers": 1,
    "max_in_flight_per_worker": 2000
  }'
# -> {"run_id":"<id>","status":"running","profile":{...}}
```

Profile field cheat-sheet (all in the request body):

| Profile | Required fields |
|---|---|
| steady | `target_tps`, `duration_s` |
| ramp | `start_tps`, `end_tps`, `ramp_s`, `duration_s` |
| spike | `base_tps`, `spike_tps`, `spike_every_s`, `spike_duration_s`, `duration_s` |
| soak | `connections`, `heartbeat_interval_s`, `heartbeat_action`, `duration_s` |

---

## Step 4 — watch it live

```bash
RUN=<run_id>
watch -n1 "curl -s localhost:8100/load-runs/$RUN | jq '{tps,target_tps,errors,last_error,latency_ms,workers_reporting}'"
```

`latency_ms` carries exact fleet-merged `p50/p95/p99`. `workers_reporting` below `workers` means
part of the fleet isn't up. A non-zero `last_error` tells you *why* transactions fail (e.g. an
empty pool, or a switch decline) instead of a silent 0 TPS.

---

## Step 5 — scale to a real fleet (optional, for high TPS)

One orchestrator runs workers in-process for small runs (capped at
`PAYPROBE_INPROC_MAX_TPS`, default 2000). Above the cap you need real worker processes. Each
process claims exactly one shard, so set `workers = N` on the run and bring up `N` workers.
There are two ways to manage the fleet — pick either; they target the same Redis-coordinated
workers.

### 5a — Custom (manual)

Start the workers yourself — on as many hosts/pods/containers as `workers`:

```bash
# once per shard, on each worker host
REDIS_URL=redis://<redis-host>:6379 LOAD_RUN_ID=$RUN python -m worker.load_worker
```

Or with Compose, scale the bundled `load-worker` service (profile `load`):

```bash
LOAD_RUN_ID=$RUN docker compose --profile load up -d --scale load-worker=4 load-worker
```

### 5b — Dynamic (from the portal)

Configure a **worker provisioner** on the orchestrator and the Load page gains a **Fleet**
control: set a worker count and click **Scale**, and the orchestrator spawns/reaps workers for
you. A launched run also brings its own fleet up automatically and tears it down when it
finishes. Select the backend with `PAYPROBE_WORKER_PROVISIONER`:

| Value | What it does | Needs |
|---|---|---|
| `auto` (default) | docker if image set, else compose, else local subprocess | — |
| `local` | spawn `load_worker` subprocesses on the orchestrator host | `REDIS_URL` |
| `docker` | start each worker as a fresh `docker run -d` container | docker socket + `PAYPROBE_WORKER_IMAGE` (+ `PAYPROBE_WORKER_NETWORK`) |
| `compose` | run `load-worker` containers via `docker compose run -d` | docker socket + `PAYPROBE_COMPOSE_FILE` mounted into the orchestrator |
| `none` | no auto-spawn — falls back to the custom path (manual command shown in the portal) | — |

**Each worker as its own container (`docker` backend).** With
`PAYPROBE_WORKER_PROVISIONER=docker`, every Scale spawns a fresh container —
`docker run -d --rm --name payprobe-lw-<runid>-<i> --network <net> -e LOAD_RUN_ID=<id>
--entrypoint python <image> -m worker.load_worker` — and scaling down `docker rm`s the
highest-indexed ones. It needs only the docker socket mounted into the orchestrator plus
`PAYPROBE_WORKER_IMAGE` (the prebuilt worker image, e.g. `payprobe-load-worker`) and
`PAYPROBE_WORKER_NETWORK` (the network where Redis resolves, e.g. `payprobe_default`) — no
compose file. Containers self-clean on exit (`--rm`) and are reaped on stop.

`GET /load-workers/provisioner` reports the active backend; `POST /load-runs/{id}/scale`
`{ "desired": N }` is what the portal's Fleet control calls. When no provisioner is available
the response carries the manual command instead, so the custom path is always one copy-paste
away.

To force the true distributed path (never fall back to in-process workers), also set
`PAYPROBE_LOAD_EXTERNAL_WORKERS=1`. Each worker seeds its generators from `run_id:worker_index`,
so the fleet produces independent-but-reproducible card data.

---

## Step 6 — stop, history, trend

```bash
curl -s -X POST localhost:8100/load-runs/$RUN/stop      # graceful stop
curl -s localhost:8100/load-runs | jq                   # load-run history
curl -s localhost:8100/runs/trend | jq                  # p95/TPS trend over time
```

In the Portal these are the **Stop** button, the **Load** history list, and the **Trends** page.

---

## Quick reference — minimal steady run, end to end

```bash
cd infra/docker && docker compose up -d redis scenario-service auth-service orchestrator
RUN=$(curl -s -X POST localhost:8100/load-runs -H 'Content-Type: application/json' -d '{
  "scenarios":[{"id":"smoke","name":"smoke","steps":[
    {"id":"auth","target":"http","action":"send_auth_request",
     "payload":{"pan":"${rand.pan(411111)}","stan":"${seq.stan(6)}","amount":"${rand.amount(100,5000)}"}}]}],
  "environment_name":"mock","type":"steady","target_tps":200,"duration_s":60,"workers":1
}' | jq -r .run_id)
watch -n1 "curl -s localhost:8100/load-runs/$RUN | jq '{tps,errors,latency_ms}'"
```
