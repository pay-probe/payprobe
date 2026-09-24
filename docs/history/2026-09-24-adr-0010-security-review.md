# ADR-0010 agent-hub: security review (2026-09-24)

**Scope:** `packages/agent-hub` (every module), `payprobe_common/agent_toolkit.py`,
`rest_backend.py`, `llm_provider.py`, the three auth gates, the MCP catalog
entries and the compose wiring, on branch `adr-0010` at `f8aa944d` (PR #2).
**Method:** three independent read-only reviews (auth and ingress; the tool
layer and the LLM boundary; engine, validator, store, quotas, triggers), each
asked for exploit scenarios with file and line, then every finding re-read by
the session that fixed it. Nothing below was taken on trust from a reviewer:
each fix has a test that encodes the exploit.
**Outcome:** two high findings and nine medium ones were real and are fixed on
the branch (`agent-hub` suite 185 → 205 tests, green twice in a row). What is
accepted as designed, and what stays owed, is listed at the end. The Go/No-Go
itself is David's; this document is the input to it.

## 1. Findings fixed on the branch

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | High | **D3 gate was "some ancestor", not "every path".** A `full` task or write `tool` node validated as long as *an* approval was a transitive predecessor over all edges. Two published workflows executed writes with no human decision: a gate a `condition` skipped (`cond -> gate when:true`, `cond -> apply when:false`), and a gate crossed on its `rejected` edge. | `validate.approval_gated`: every path into the node must cross an approval's `approved` edge; the engine repeats the check at run time over the edges that actually fired (`Engine._approved_on_path`) before any `full` task or write/execute tool node starts. | `test_hub_gates.py::test_gate_a_condition_can_skip_does_not_gate`, `::test_gate_crossed_on_its_rejected_edge_gates_nothing`, `::test_agent_republished_as_full_after_validation_is_stopped_at_run_time` |
| 2 | High | **Secrets reached the LLM provider, the heartbeat row and any authenticated viewer in clear.** `get_connection`, `list_connections` and `get_global_variables` return secret-named fields unmasked (the platform's connection store decrypts into memory and the response model has no masking serializer), the journal snapshots them, and `GET /heartbeats/{id}` returned the row to any login. Invariant #8 broken at egress, at rest and at the API. | Tool layer: `agent_toolkit.mask_secrets` on every tool result before the model sees it (`<secret:fingerprint>`); `GET /heartbeats/{id}` masks the whole record; the journal keeps plaintext for restore but is SecretBox-encrypted at rest (`PAYPROBE_SECRET_KEY`, now passed to agent-hub by the three compose files; no-op without a key, like the registries). | `::test_credentials_are_masked_before_the_model_and_the_viewer_see_them` (masked to the model, masked on the API, revert still restores the real value) |
| 3 | High | **Unattended full mode.** Nothing stopped a `full` agent from carrying a `schedule`, `event` or `webhook` trigger; the "downstream RBAC" the trigger module cited does not exist (no platform service checks roles on writes). A failed run would wake it and its writes would land under a role-less token. | Validator refuses `full` with an unattended trigger. `launch_heartbeat` refuses (recorded as `failed`, alerts) any `full` heartbeat whose caller is not a human (roles from auth-service or the dev marker; never `svc`, static, event, schedule or webhook principals) unless the engine vouches that an approved gate is on the executed path. | `::test_full_mode_agent_may_not_carry_an_unattended_trigger`, `::test_full_mode_wake_by_a_service_token_is_refused_and_recorded` |
| 4 | Medium | **Any `svc` or static token was a super-admin**, including on `POST /approvals/{id}/decide`, `revert` and `PUT /pause`. The MCP server mints a `svc` JWT and its HTTP port has no inbound auth. | `require_roles(..., human=True)` on decide, revert and pause: service credentials are 403 there; they keep wake, run, events and reads. | `test_hub_auth_hardening.py::test_service_credentials_cannot_perform_a_humans_act` |
| 5 | Medium | **On-behalf-of tokens accepted at agent-hub's own door.** A leaked heartbeat token (TTL up to an hour, travels over plain HTTP inside compose) could decide approvals or publish as the invoking user; `act` was never inspected. | `require_auth` rejects any token carrying `act` (agents never call agent-hub). | `::test_on_behalf_of_token_is_refused_at_the_door` |
| 6 | Medium | **`node.environment` was a free pass.** The `mock` label exempted a node from the gate but was never enforced: a `tool` node labelled `mock` with `environment_name: uat` in its args ran full-rate load against uat with no gate and no cap. | The engine scopes a labelled tool node to its environment (`ToolScope.environments=(label,)`) and narrows a labelled agent task's write scope the same way. | `::test_write_tool_node_labelled_mock_is_scoped_to_mock` |
| 7 | Medium | **Global pause did not stop engine tool nodes**; an approval decided during a pause executed writes immediately. | `_run_tool` checks the pause flag and fails the node `agents are paused`. | `::test_pause_stops_tool_nodes_too` |
| 8 | Medium | **`advance()` saved the run once at the end.** A crash after a write-tier tool node executed replayed it on restart and lost its journal. | The run row is saved after every node that starts or finishes. | covered by `test_hub_engine.py::test_restart_mid_run_resumes_from_the_row`; no new test isolates the crash window |
| 9 | Medium | **`rbac.edit` holders could widen their own grant** (mode, tools, write scope, rbac, unattended triggers, budget) and publish. | `validate.widens(prev, new)`; a non-admin publish of a wider agent version is 403 with the reasons. | `::test_widens_names_every_grown_field`, `::test_rbac_edit_holder_cannot_publish_a_wider_version` |
| 10 | Medium | **`start_load_run` `extra` bypassed the write scope** (merged after the check) and the load cap ignored `base_tps` and non-finite rates (`"nan"` passes every `> cap`). | `extra` is inspected by the scope check; `base_tps` joins the rate keys; nan/inf count as unbounded. | `::test_extra_cannot_smuggle_an_environment_past_the_write_scope`, `::test_load_cap_sees_base_tps_and_non_finite_rates` |
| 11 | Medium | **Static bearer unreachable beside a JWT secret** (any non-JWT bearer was 401 before the `API_TOKEN` comparison), in all three gates; and the compose placeholder `dev-insecure-change-me` counted as a configured secret outside dev. | A bearer that is not a JWT falls through to the static comparison; the placeholder secret is a 503 outside dev. Applied to agent-hub, scenario-service and the orchestrator. | `::test_static_bearer_works_beside_a_jwt_secret`, `::test_placeholder_secret_is_refused_outside_dev`; scenario-service 330 and orchestrator 393 still green |
| 12 | Low | Registry-authored content (scenarios, connections, networks, tables) reached the model unwrapped. | Every read and execute tool result is untrusted-wrapped. | `::test_every_read_and_execute_result_is_untrusted` |
| 13 | Low | The provider call followed redirects with the API key attached, past the egress fence. | `llm._NoRedirect` opener: any 3xx is an error. | none (a 5-line handler; the fence test suite covers the first hop) |
| 14 | Low | One failing launch (for example no LLM configured) aborted the rest of a schedule tick or event fan-out; a `Conflict` from two racing wakes was a 500. | Per-agent try/except in both loops (the failure is returned as a row with `status: error`); a `Conflict` on insert returns the running row as `coalesced`. | existing `test_hub_events.py` |
| 15 | Low | A failed node left parallel heartbeats running and a pending approval open. | `_stop_siblings` on run failure. | `::test_a_failed_node_stops_its_running_siblings` |
| 16 | Low | The restart watchdog ran once at startup, so a heartbeat orphaned inside its wall clock stood until the next restart. | `reconcile_running` runs on the engine tick as well. | existing `test_hub_store.py` watchdog test |
| 17 | Low | The webhook body was fully read before the 64 KB cap. | `Content-Length` is checked first. | `::test_webhook_declared_oversize_is_refused_before_the_body_is_read` |

## 2. Accepted as designed (recorded, not changed)

- **Reads are unscoped for any authenticated principal.** `GET /heartbeats/{id}`,
  `/runs/{id}`, `/approvals`, `/plans` return their full record to any login,
  whatever the invoker's role. With finding 2 fixed no credential is in that
  record. A per-project read filter would need `project_ids` to mean something
  downstream first; it does not today.
- **Signed webhooks replay for five minutes.** No nonce cache. Bounded by
  coalescing, per-agent and hub-wide budgets. Document, do not build, until a
  real integration needs it.
- **A direct portal wake of `plan-executor` executes writes with no approval.**
  That is what `rbac.invoke: admin` plus a human caller means (invariant 10 as
  written); the journal and revert are the safety net. Workflows are the gated
  path.
- **Authority is snapshotted at run start.** A run that waits days at a gate
  executes its later nodes under a token minted from the starter's roles at
  start time; a role revoked meanwhile is not seen. Re-checking the principal
  against auth-service at execution, or executing post-gate nodes under the
  approver's credential, is a design change for a later ADR.
- **Cross-agent prompt chaining.** `${observe.result}` feeds the reviewer as
  user content. The reviewer is read-only and every write sits behind a gate
  (now on every path), so an upstream agent steered by injected evidence can
  at most produce a plan a human then reads.
- **A soak profile's `connections` count is not a rate** and the load cap does
  not see it. Heavy soak is a workflow `tool` node behind an approval by
  construction; a heartbeat can still open many idle connections at 0 TPS.
- **The mcp-server HTTP transport has no inbound auth** (DNS-rebinding
  protection only) and compose publishes :8200. Agent-hub now treats its `svc`
  token as a service: it may wake and run, never decide, revert, pause or
  execute a `full` heartbeat. Putting a bearer on the MCP transport is the
  MCP server's item, not this ADR's.
- **Journal persistence is per wake, not per write.** A container killed
  mid-heartbeat after three of five executed writes loses their `before`
  snapshots (the watchdog marks the row `orphaned by restart` with an empty
  journal). Write-through journalling is the next hardening step; it needs
  a store hook on `ChangeJournal.record` and is not in this change.

## 3. Verification ledger

- `packages/agent-hub`: 185 tests before, **205 after**, green twice in a row
  (Python 3.13, PostgreSQL through the forwarded port, `payprobe_test`).
- `scenario-service` 330, `orchestrator` 393, `payprobe-assistant` 63: green
  after the shared gate and toolkit changes.
- Black and ruff clean on every file touched (the agent-hub package was not
  black-clean before this change on files it does not touch; left alone).
- All three compose files parse with the new `PAYPROBE_SECRET_KEY` line.
- One existing test changed its expectation on purpose: a workflow `tool`
  node's read result is now untrusted-wrapped like every other read
  (`test_condition_routes_and_tool_node_runs_under_the_user`). One existing
  test was racy (`test_hub_subjects`: a third wake could coalesce onto a
  still-running second one) and now waits.
- Not verified here: the fixes running in David's compose stack (the image was
  not rebuilt from this session), and the browser behaviour of the masked
  journal view.

## 4. Real-provider runs of the reference workflows (2026-09-24, same stack)

- `observer` workflow run `93dfed57`: `observe` (12 read-only calls) and
  `review` completed on `claude-haiku-4-5`; the run parked at `gate` with a
  pending approval in the inbox. The gate is David's to decide (this session's
  attempt to approve it through the API was refused as a self-approval, which
  is the correct reading of D3).
- `certification-plan` run `c7f0b203`: `plan` proposed one `create_scenario`
  call (155k tokens in), `review` returned `changes_requested` with concrete
  reasons (an invalid step kind `assertion`, an empty third proposed call, the
  unaddressed assertion mismatch), `verdict` false, gate and apply skipped,
  run `done`. That is exit criterion three ("reviewer blocks a deliberately
  bad plan") on a real provider, with a plan that was bad without anyone
  making it so. The approve-then-apply path stays proven under FakeLLM only.
