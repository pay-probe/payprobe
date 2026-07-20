# PayProbe Test Console — Build Spec

How to turn PayProbe into a real payment **test console** in the operational shape of the
standalone RestPay console — and the concrete development backlog to get there.

> **TL;DR.** You are ~80% there. The distributed load fleet, the `/load` console page, the
> gRPC/TCP adapters, and most EMV crypto already exist. The console *experience* (pick a
> transaction, drive it at a rate or hold connections, watch live p95/p99 + live/dead/reconnect)
> is already wired end-to-end. What's missing is **realism plumbing** (DUKPT, per-transaction
> data variation, terminal/card provisioning) and a few **console-grade controls** (transaction-type
> selector, weighted traffic mix, hot re-tune, cyclic soak). This doc maps what exists and lists
> what to build, prioritized.

---

## 1. What already exists (don't rebuild it)

| RestPay console concept | PayProbe equivalent (already built) | Where |
|---|---|---|
| `restpay-control.py` + curl driving tests | **Load console page** (`pp-load`) | `packages/portal/src/app/load/load.component.ts` |
| `ControlServer` REST (`/config /status /stop`) | `POST /load-runs`, `GET /load-runs/{id}`, `POST /load-runs/{id}/stop` | `packages/orchestrator/api/main.py` (≈672–780) |
| Multi-instance docker fan-out | **load_coordinator** shards a profile across N workers over Redis | `packages/orchestrator/api/load_coordinator.py` |
| 9 strategy classes (load/spike/heartbeat/…) | **LoadProfile** types `steady` / `ramp` / `spike` / `soak` from one pacing loop | `packages/worker/engine/load/profile.py`, `driver.py` |
| `GrpcClient` to the switch | **descriptor-driven gRPC adapter** (+ TCP/ISO 8583) | `packages/worker/adapters/grpc/`, `adapters/tcp/` |
| `StatisticsTracker` (p95/p99 reservoir) | **LatencyHistogram** — buckets merge across the fleet, so p95/p99 are *exact* | `packages/worker/engine/load/histogram.py` |
| EMV crypto helpers (PIN block, CVV, ARQC) | **crypto_tools** (DES/3DES, ISO-0 PIN block, Retail MAC, CVV/CVC, ARQC) + **EMV Crypto palette** | `packages/worker/engine/crypto_tools.py`, `packages/scenario-service/models/emv_crypto_catalog.py` |
| Live/dead/reconnect counters | already in the load snapshot (`live`, `dead`, `reconnects`) | `load-api.service.ts` `LoadSnapshot` |
| `restpay-stress-cycle.py` soak + leak watch | `soak` profile + **schedules** + Prometheus/Grafana infra | `profile.py` (SOAK), `schedule_store.py`, `infra/grafana` |

**The important realization:** in RestPay each "test type" was a hand-written strategy class. In
PayProbe the *cadence* (steady/ramp/spike/soak) is decoupled from the *message* — the load driver
just runs **a scenario** repeatedly. So "payment", "refund", "reversal", "heartbeat" are not new
engine code; they are **scenarios** you select. That's a better architecture than RestPay's — but it
means the missing work is about making those scenarios realistic and easy to pick, not about
strategy plumbing.

---

## 2. How to stand up a "real" test console today

You can run a credible payment load console right now, before building anything new:

1. **Author the transaction scenarios** in the constructor — one each for *payment*, *refund*,
   *reversal*, *heartbeat* — against your gRPC/TCP adapter, using the EMV Crypto palette for any
   PIN block / ARQC / MAC steps. Save them; note their scenario IDs.
2. **Define the environment** (host/port/TLS, secrets via SecretBox) the adapter connects to.
3. **Open `/load`**, pick the scenario, choose a profile:
   - *Steady* = fixed TPS soak of one transaction type (RestPay `load`).
   - *Ramp* = find the knee / max TPS (RestPay had no clean analog — this is better).
   - *Spike* = base + periodic bursts (RestPay `spike`).
   - *Soak* = hold N connections sending heartbeats (RestPay `heartbeat`); set `heartbeat_action`.
4. **Set `workers`** to fan the load across processes; scale `python -m worker.load_worker`
   (containers/pods) behind one Redis to reach the 20K TPS / 100K conn targets.
5. **Watch live** — TPS, p95/p99, live/dead/reconnect, `last_error` stream off `load.sample`.
6. **Stop / history / trend** — `POST /load-runs/{id}/stop`; runs land in history and `/runs/trend`.

That already reproduces the *operational* console. The gaps below are what make it *realistic
and repeatable at scale* like a certification-grade harness.

---

## 3. Development backlog (prioritized)

### P0 — realism plumbing (without these, load is "fake": identical data, no DUKPT)

**3.1 Per-transaction data variation / generators.**
Today the load driver replays *one* scenario verbatim, and `variables.py` only resolves
`${step.response.…}` back-references — there is no way to vary card/terminal/RRN/STAN per
transaction. At 20K TPS that means identical PAN/STAN on every message, which most switches
reject as duplicates and which makes the test meaningless.
*Build:* a generator namespace usable in payloads — e.g. `${rand.pan(bin)}`, `${seq.stan}`,
`${uuid}`, `${rand.amount(min,max)}`, `${pool.terminal}`, `${now.rrn}`. Implement as a resolver
extension alongside `resolve_value` in `packages/worker/engine/variables.py`, seeded per
worker+stream so runs are reproducible.
*Why:* this is the single biggest realism gap; RestPay's `TestDataGenerator` / `CardDataLoader` /
terminal seed refs existed exactly for this.

**3.2 DUKPT engine.**
`crypto_tools.py` has DES/3DES, ISO-0 PIN block, Retail MAC, CVV, ARQC — but **no DUKPT**
(BDK → IPEK → derived PEK/MAC keys, KSN management). RestPay shipped a whole `dukpt-java` lib.
*Build:* port DUKPT (BDK/IPEK derivation, KSN increment, PEK/MAC session keys, ISO-0/ISO-4 PIN
block under DUKPT) into `crypto_tools.py` and expose it as `crypto` node operations + a palette
group beside EMV Crypto. Reuse the test vectors from the RestPay `dukpt-java` tests for parity.
*Why:* terminal-originated PIN/MAC traffic is DUKPT in the real world; without it you can't
exercise the switch's PIN translation path.

**3.3 PVV calculator.**
Only CVV/CVC present. Add Visa PVV (and optionally IBM 3624 offset) to `crypto_tools.py` for
issuer-side PIN verification scenarios.

### P1 — console-grade controls (make it feel like a test console, not a load form)

**3.4 Transaction-type selector + curated starter flows.**
The API already takes `scenario_ids[]`, but the operator shouldn't have to hand-build payment /
refund / reversal / heartbeat each time. Ship them as **starter flows** (`starter_flow.py`) and
add a "Transaction type" dropdown on `/load` that maps to the right scenario — the one-click
parity with RestPay's `--test refund|reversal|heartbeat`.

**3.5 Weighted traffic mix (multi-scenario blend).**
RestPay's `multi_client` ran independent clients; real switches see a *mix*. Extend `LoadRunRequest`
to accept weighted scenarios (e.g. `80% payment / 15% refund / 5% reversal`) and have the driver
pick per-transaction by weight. Touches `LoadProfile`/`Shard` (carry the weighted set) and the
worker's `make_run_once` (choose a scenario per call) in `packages/worker/load_worker.py`.

**3.6 Hot re-tune mid-run.**
RestPay `POST /config` switched type/params live. PayProbe load runs are a fixed profile. Add
`POST /load-runs/{id}/retune` to adjust `target_tps` (and spike params) on a running run — push
the new shard params over the bus; the driver already reads `target_tps_at(elapsed)` each tick, so
the pacing picks it up. Add a TPS slider to the live console.

**3.7 Backpressure visibility.**
The driver already caps in-flight via a semaphore (`max_in_flight`). Surface
*saturation* in the snapshot (in-flight vs cap, throttled count) so the operator sees when the
target is the bottleneck — the useful half of RestPay's `backpressure` strategy.

### P2 — provisioning, repeatability, leak hunting

**3.8 Terminal/card pool + provisioning.**
RestPay onboarded terminals (RSA/CMAC/batch) and stored cards in a DB. If your switch requires
onboarded terminals or registered cards before payments, add: (a) an **onboarding pre-run step**
(a scenario that provisions and emits terminal refs), and (b) a small **card/terminal catalog
store** (mirror `table_store.py` / `connection_store.py`) so `${pool.terminal}` / `${pool.card}`
draw from valid, persisted entities instead of random ones.

**3.9 Cyclic soak + leak detection harness.**
Reproduce `restpay-stress-cycle.py`: a schedule that loops soak → stop → sample → repeat while
recording RSS/heap from the worker containers. You already have `soak`, `schedules`, and
Prometheus/Grafana — wire a "cyclic soak" schedule type and a Grafana panel that trends worker
memory across cycles, with an alert on monotonic growth.

**3.10 Onboarding/heartbeat keepalive parity.**
Confirm the `soak` `heartbeat_action` + `heartbeat_interval_s` map to your switch's keepalive
(connection ping vs app-level echo), and add planned disconnect/reconnect jitter
(RestPay's `--disconnect-interval` / `--disconnect-jitter`) to the soak profile for connection-churn
testing.

### Cross-cutting / hygiene

- **Secrets:** keep all switch creds/keys in **SecretBox** — do *not* copy RestPay's pattern of
  plaintext Oracle creds + PEMs committed in the bundle.
- **TLS to SUT:** ensure the gRPC/TCP adapters carry client TLS + server trust for the real switch.
- **Report parity:** load runs should produce the same certification/JUnit/compare artifacts your
  functional runs do, so a load run can gate a release.

---

## 4. Suggested order of execution

1. **3.1 data generators** + **3.4 transaction-type starter flows** → makes load *meaningful* and
   *one-click*. Biggest value, smallest blast radius (engine + portal, no new infra).
2. **3.2 DUKPT** + **3.3 PVV** → crypto realism for PIN/MAC paths.
3. **3.5 weighted mix** + **3.6 hot re-tune** + **3.7 saturation** → the true "console" feel.
4. **3.8 provisioning** + **3.9 cyclic soak** → certification-grade, repeatable, leak-aware.

Each item lands in code you already own; none requires a new service.
