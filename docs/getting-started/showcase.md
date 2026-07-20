# The showcase network — PayProbe in five minutes

`scripts/showcase.py` builds and starts a small but complete simulated
acquiring network, so you can see the whole platform working end to end without
authoring anything by hand. It talks only to the public REST APIs, so it also
serves as a worked example of how to script PayProbe.

## What it builds

```
  driver scenario ──0200──▶  switch  (participant flow, 1 instance)
                                │  Call → "showcase-issuers" group (round-robin)
                                ▼
                    issuer A / B / C   (3 instances of one issuer flow)

              payShield HSM simulator running alongside
```

- **3 issuer instances** — one participant flow that approves every `0200`
  with `DE39 = 00`, run three times behind a **group** (a fleet).
- **A switch** — a participant flow that receives the `0200`, forwards it to
  the issuer group, and relays the issuer's response code back.
- **A payShield HSM simulator** — a saved simulator brought up with the network
  (real crypto under a test LMK).
- **A driver scenario** — sends a purchase `0200` at the switch and asserts the
  approval. Wired as an autostart initiator, so traffic flows the moment the
  network is ready.

The pieces are wired on the network canvas' terms: `switch → group → issuers`
and `driver → switch`. Start order is **derived** from that wiring — issuers
before the switch, payShield first, driver last — nothing is ordered by hand.

## Run it

> **In the portal:** open **Simulated Network → Networks**; on the empty page click **Load showcase network** (one click — no CLI). Then read on for the scripted equivalent and the `--certify` verdict.

Bring the stack up (`infra/docker`, or the services locally), then:

```bash
make showcase              # build the artifacts + start the network
# or, with explicit endpoints / auth:
python scripts/showcase.py \
    --scenario-url http://localhost:8000 \
    --run-url http://localhost:8100 --token "$PP_TOKEN"
```

It prints the resolved launch plan and the run's health, e.g.:

```
· network
  plan: sims=['showcase-payshield']  start-order: issuers → switch  initiators=['scn-…']
· starting the network …
  run 4f3a1c9d  health 4/4 ready=True
    ↳ showcase-issuer @ 127.0.0.1:9410
    ↳ showcase-issuer @ 127.0.0.1:9411
    ↳ showcase-issuer @ 127.0.0.1:9412
    ↳ showcase-switch @ 127.0.0.1:9401
  driver run(s): ['run-…']
```

## End with a verdict — `--certify`

```bash
python scripts/showcase.py --certify
```

After starting the network this drives a steady load run at the switch, plays a
**payShield outage** (100% drop) as a chaos storm, and scores a **resilience
certificate** — the platform's thesis in one command: spin up a network, stress
it, walk away with an evidence-grade grade.

```
  RESILIENCE CERTIFICATE — grade B  score 82.0/100  PASS
    [PASS] Success rate held under chaos: 0.71 (threshold 0.60)
    [FAIL] p95 latency stayed bounded: 240.0 (threshold 200.0)
    …
  Full certificate: portal → Resilience
```

(`--certify` needs the stack up with a chaos-capable payShield simulator — it
exercises live load + chaos, so run it against a real environment.)

## What to look at next

- **Portal → Simulated Network → Networks** — open *Showcase network* on the
  canvas; the nodes carry live health dots while the run is up.
- **Portal → Network Trace** — hit *Resume capture*, let the driver fire, then
  open a transaction: it's stitched across `switch → issuer → back` by
  correlation id.
- **Chaos / Resilience** — point a chaos storm at the payShield simulator and
  watch the network degrade and recover; score it into a resilience
  certificate.
- **The assistant** — ask it "what networks are running?" or "what's the start
  order of the showcase network?" — the runtime-read tools answer from live
  state.

Flags: `--build-only` creates the artifacts without starting (start it from the
portal instead); `--teardown` (or `make showcase-teardown`) stops the run and
deletes everything it created. The script is idempotent — re-running updates the
same named artifacts.
