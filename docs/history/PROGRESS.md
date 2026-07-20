# PROGRESS — autonomous test-infra hardening

Runnable suite: `cd packages && python -m pytest worker/tests orchestrator/tests scenario-service/tests`
(exits non-zero on any failure). Treated as ground truth each iteration.

## Status: Definition of Done — MET (suite green, 195 passed / 0 failed)
- Every scenario runs end-to-end with clear pass/fail — enforced by
  `worker/tests/test_example_scenarios.py` (runs every `examples/scenarios/*`).
- One command, non-zero on failure — `make test`.
- Per-scenario + summary report — orchestrator `GET /runs/{id}` (JSON),
  `/junit`, `/report` (HTML); documented in README.
- No silent skip / swallowed errors — linear-path kind dispatch fix +
  example-scenario guard assert no step lacks a clear status.
- Flaky — none observed; `flaky` marker + quarantine convention documented.
- README — "Running the tests / Adding a scenario / Reading a report" added.

## Blockers needing human input
_None._ Remaining ideas are enhancements, not blockers: gRPC adapter (design
only), a portal "run against a real environment" picker (runs still select a
bundled env; mock-mode envs route to the mock adapter).

## Baseline (2026-06-18)
- Full suite: **180 passed, 3 failed**.
- Failures (all `worker/tests/test_engine.py`, kind=`crypto` nodes):
  - `test_crypto_node_pin_block_roundtrip`
  - `test_crypto_emv_arqc_chain_from_mdk`
  - `test_crypto_emv_arqc_to_arpc_verify_flow`
- Root cause (measured, not assumed): `run_crypto()` in
  `worker/engine/crypto_tools.py` returns `{"error": "pycryptodome is not
  installed in this runtime"}`. The worker's `pyproject.toml` declares
  `python-pkcs11` and `iso8583` but **omits `pycryptodome`**, which
  `crypto_tools.py` requires (`from Crypto.Cipher import DES, DES3`). So the
  crypto code-path silently degrades to an error result and the 3 scenarios
  error out. Category (a): infra/dependency broken.

## Iterations
(newest first — appended each loop)

### Iteration 2 — edge-less path now dispatches by node kind (category a)
- **Did:** added `ScenarioRunner._execute_node()` (kind-aware dispatch) and
  routed the linear/edge-less loop in `run()` through it; control-flow nodes in
  an edge-less scenario are now recorded via `_record_control` instead of
  crashing. Fixes `KeyError: 'target'` for crypto/code/http/init/call nodes in
  scenarios without edges.
- **Added test:** `test_edgeless_non_action_nodes_run_without_target_keyerror`
  (code + crypto node, no edges) — both run and pass.
- **Result:** **184 passed, 0 failed** (183 + new test). No regressions.
- **Next:** measure remaining DoD gaps — single `make test` command,
  example-scenario end-to-end coverage, flakiness, README. Pick highest value.

### Iteration 1 — fix missing `pycryptodome` dependency (category a)
- **Did:** added `pycryptodome>=3.19` to `packages/worker/pyproject.toml`
  dependencies (it was required by `engine/crypto_tools.py` but undeclared).
  Installed it in the runtime to verify.
- **Result:** full suite **180→183 passed, 0 failed**. The 3 crypto scenarios
  now run end-to-end with real DES/3DES instead of erroring.
- **Verified:** `pytest worker/tests orchestrator/tests scenario-service/tests`
  → 183 passed.
- **Next:** while reproducing, found a real latent bug — the edge-less
  (linear) path in `runner.run()` calls `_run_step()` which does
  `step["target"]` unconditionally, so a scenario with no edges containing a
  non-`action` node (crypto/code/http/…) raises `KeyError: 'target'` and the
  step errors. The graph path dispatches by `kind`; the linear path does not.
  Fix that next (category a: scenario errors out).

### Iterations 3–17 — "15 important topics" hardening pass
Before this batch: 184 passed. After: **195 passed, 0 failed** (`make test`,
exit 0). Each item verified before moving on; no expected result edited to pass.

1. **Makefile / one-command suite** — `make test` (+ test-worker/-cov/install/
   clean), non-zero on failure. DoD bullet.
2. **Example-scenario E2E test** — `test_example_scenarios.py` runs every
   `examples/scenarios/*.json` through the engine (mock); asserts a clear
   scenario status and no step left in a non-clear status. All 3 pass; catches
   silent breakage.
3. **Deps** — declared `pycryptodome` (runtime) + `httpx` (worker dev); green
   from a clean install. Registered `flaky` marker in both pytest configs.
4. **TCP auto-reconnect** — `_open()`/`_reconnect()` with exponential backoff +
   re-sign-on; `_exchange` waits for an in-progress reconnect (bounded by the
   response timeout). Test: server drops link after one reply → next call
   reconnects and succeeds.
5. **Events sink** — `events()`/`drain_events()` expose unsolicited /
   uncorrelated messages (tested).
6. **Response-shape coverage** — assert StepResult exposes named DEs
   (response_code/stan/rrn) + raw `fields` for the report inspector.
7. **Test-connection endpoint** — orchestrator `POST /connections/test` opens
   the socket + health-checks → `{ok, latency_ms, error}`; reconnect disabled so
   a bad host fails fast. 3 tests incl. a live mock-server happy path.
8. **Connections page: Test button** — calls the endpoint, shows ✓ latency / ✗
   error (portal build clean).
9. **Connections page: Clone** — duplicate into a new unsaved draft.
10. **Editor: connection on node** — action-node subtitle shows `· ⇄ <conn>`
    when a connection is bound.
11. **Connection validation** — pydantic validators (port 0–65535, known
    protocol/adapter, `framing.length_prefix_bytes >= 1`); 422 on bad input
    (tested).
12. **Report per-connection** — orchestrator keeps the original catalog target
    in `config._catalog_target` when repointing a step to its connection, so the
    report shows both step type and connection (tested).
13. **README** — "Running the tests", "Adding a scenario", "Reading a report",
    flaky/quarantine, secrets note (DoD bullet).
14. **Secrets** — verified no leakage path (connection configs carry no
    credentials; test endpoint never echoes config); documented variables/secrets
    for keys/PINs.
15. **Self-review — weakest part now:** the suite proves scenarios *execute*
    cleanly in mock mode, but real-target runs still need a non-mock environment
    chosen in the portal (Execute picker only lists bundled envs). That + the
    gRPC adapter are the highest-value next items — enhancements, not blockers.
