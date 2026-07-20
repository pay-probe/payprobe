"""Tool metadata: titles + behaviour hints, plus prompt/resource specs.

Kept SDK-free (no ``mcp`` import) so it can be unit-tested standalone and so
``tools.py`` stays a plain HTTP layer. ``server.py`` consumes these tables to
register everything with the FastMCP server.

Tools are organised into **groups** (:data:`TOOL_GROUPS`) — the flat
:data:`TOOL_SPECS` list ``server.py`` registers is *derived* from the grouped
table, so the grouping is the single source of truth. The portal's Integrations
page renders the same groups (via a generated snapshot — see
``scripts/gen_catalog.py``), so what an operator sees there can never drift from
what the server actually registers.

Behaviour hints map onto MCP ``ToolAnnotations``:

* ``readOnlyHint``  — the tool does not modify any state.
* ``destructiveHint`` — (only meaningful when not read-only) the tool may
  delete or tear down something. Clients use this to gate confirmation.
* ``idempotentHint`` — calling again with the same args has no *additional*
  effect (PUT-style upserts, deletes, cancels).
* ``openWorldHint`` — the tool reaches an external entity (a live endpoint, an
  LLM, a simulator socket) rather than only PayProbe's own store.
"""
from __future__ import annotations

# -- hint shorthands ----------------------------------------------------------
# Read-only families
READ = {"readOnlyHint": True, "idempotentHint": True}
READ_EXTERNAL = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}
# Write families (readOnlyHint defaults False)
CREATE = {"destructiveHint": False, "idempotentHint": False}
UPSERT = {"destructiveHint": False, "idempotentHint": True}   # PUT replace/set
DELETE = {"destructiveHint": True, "idempotentHint": True}    # remove/teardown
START = {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
STOP = {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
ADVANCE = {"destructiveHint": False, "idempotentHint": False}  # mutates session state
CANCEL = {"destructiveHint": True, "idempotentHint": True}
PAUSE = {"destructiveHint": False, "idempotentHint": True}

# -- grouped tool table -------------------------------------------------------
# (group title, [(tool function name, tool title, hints), ...]).
# Each function name must exist in ``mcp_server.tools``; ``test_registry``
# asserts the table and the module stay in sync. ``TOOL_SPECS`` below flattens
# this into the (name, title, hints) list ``server.py`` registers.
TOOL_GROUPS: list[tuple[str, list[tuple[str, str, dict]]]] = [
    ("Discovery", [
        ("list_step_catalog", "List step catalog", READ),
        ("list_connections", "List connections", READ),
        ("list_message_formats", "List message formats", READ),
        ("list_environments", "List environments", READ),
    ]),
    ("Environments", [
        ("get_environment", "Get environment", READ),
        ("save_environment", "Save environment", UPSERT),
        ("delete_environment", "Delete environment", DELETE),
    ]),
    ("Scenarios", [
        ("list_scenarios", "List scenarios", READ),
        ("get_scenario", "Get scenario", READ),
        ("assist_scenario", "Draft scenario from prompt", READ_EXTERNAL),
        ("validate_scenario", "Validate scenario", READ),
        ("create_scenario", "Create scenario", CREATE),
        ("update_scenario", "Update scenario", UPSERT),
    ]),
    ("ISO 8583 inspector", [
        ("iso8583_analyze", "Decode ISO 8583 message", READ),
        ("iso8583_build", "Build ISO 8583 message", READ),
        ("iso8583_diff", "Diff ISO 8583 messages", READ),
        ("iso8583_tlv_build", "Build BER-TLV", READ),
    ]),
    ("Runs", [
        ("run_scenario", "Run scenario", START),
        ("get_run", "Get run", READ),
        ("list_runs", "List runs", READ),
        ("cancel_run", "Cancel run", CANCEL),
        ("diagnose_run", "Diagnose run", READ),
        ("run_trend", "Run trend", READ),
    ]),
    ("Connections", [
        ("get_connection", "Get connection", READ),
        ("save_connection", "Save connection", UPSERT),
        ("delete_connection", "Delete connection", DELETE),
        ("test_connection", "Test connection", READ_EXTERNAL),
        ("diagnose_platform", "Diagnose platform connectivity", READ_EXTERNAL),
    ]),
    ("Simulators", [
        ("list_simulators", "List simulators", READ),
        ("get_simulator", "Get simulator", READ),
        ("start_simulator", "Start simulator", START),
        ("stop_simulator", "Stop simulator", STOP),
        ("save_running_simulator", "Save running simulator", CREATE),
        ("list_saved_simulators", "List saved simulators", READ),
        ("get_saved_simulator", "Get saved simulator", READ),
        ("create_saved_simulator", "Create saved simulator", CREATE),
        ("start_saved_simulator", "Start saved simulator", START),
        ("stop_saved_simulator", "Stop saved simulator", STOP),
        ("set_saved_simulator_enabled", "Enable saved simulator", UPSERT),
        ("delete_saved_simulator", "Delete saved simulator", DELETE),
    ]),
    ("payShield HSM", [
        ("start_payshield_simulator", "Start payShield simulator", START),
        ("hsm_command", "Send HSM command", START),
        ("run_hsm_example", "Run HSM example scenario", START),
    ]),
    ("Participant flows & topologies", [
        ("list_participant_flows", "List participant flows", READ),
        ("list_participant_groups", "List participant groups", READ),
        ("get_participant_group", "Get participant group", READ),
        ("create_participant_group", "Create participant group", CREATE),
        ("save_participant_group", "Save participant group", UPSERT),
        ("delete_participant_group", "Delete participant group", DELETE),
        ("list_participants", "List running participants", READ),
        ("list_topology_runs", "List network-flow runs", READ),
        ("list_network_traces", "List network traces", READ),
        ("get_network_trace", "Get correlated trace", READ),
        ("set_trace_capture", "Toggle trace capture", PAUSE),
    ]),
    ("Network flows", [
        ("list_network_flows", "List network flows", READ),
        ("get_network_flow", "Get network flow", READ),
        ("save_network_flow", "Save network flow", UPSERT),
        ("delete_network_flow", "Delete network flow", DELETE),
        ("validate_network_flow", "Validate network flow", READ),
        ("plan_network_flow", "Plan network flow", READ),
        ("start_network_flow", "Start network flow", START),
        ("stop_network_flow", "Stop network-flow run", STOP),
        ("install_showcase", "Install showcase demo network", START),
    ]),
    ("Chaos & resilience", [
        ("get_simulator_chaos", "Get simulator chaos dial", READ),
        ("set_simulator_chaos", "Set simulator chaos", START),
        ("start_chaos_storm", "Start chaos storm", START),
        ("cancel_chaos_storm", "Cancel chaos storm", STOP),
        ("list_resilience_runs", "List resilience runs", READ),
        ("get_resilience_run", "Get resilience run", READ),
        ("start_resilience_run", "Start resilience run", START),
        ("cancel_resilience_run", "Cancel resilience run", CANCEL),
    ]),
    ("Schedules", [
        ("list_schedules", "List schedules", READ),
        ("create_schedule", "Create schedule", CREATE),
        ("run_schedule_now", "Run schedule now", START),
        ("set_schedule_enabled", "Enable/pause schedule", UPSERT),
        ("delete_schedule", "Delete schedule", DELETE),
    ]),
    ("Message formats", [
        ("get_format", "Get message format", READ),
        ("create_format", "Create message format", CREATE),
        ("update_format", "Update message format", UPSERT),
        ("clone_format", "Clone message format", CREATE),
        ("delete_format", "Delete message format", DELETE),
    ]),
    ("Test data", [
        ("list_card_pools", "List card pools", READ),
        ("get_card_pool", "Get card pool", READ),
        ("save_card_pool", "Save card pool", UPSERT),
        ("delete_card_pool", "Delete card pool", DELETE),
        ("list_bin_ranges", "List BIN ranges", READ),
        ("get_bin_range", "Get BIN range", READ),
        ("save_bin_range", "Save BIN range", UPSERT),
        ("delete_bin_range", "Delete BIN range", DELETE),
        ("generate_bin_pans", "Generate PANs from BIN", CREATE),
        ("list_terminal_pools", "List terminal pools", READ),
        ("get_terminal_pool", "Get terminal pool", READ),
        ("save_terminal_pool", "Save terminal pool", UPSERT),
        ("delete_terminal_pool", "Delete terminal pool", DELETE),
        ("list_test_keys", "List test keys", READ),
        ("get_test_key", "Get test key", READ),
        ("save_test_key", "Save test key", UPSERT),
        ("delete_test_key", "Delete test key", DELETE),
    ]),
    ("Packs", [
        ("list_packs", "List packs", READ),
        ("get_pack", "Get pack", READ),
        ("install_pack", "Install pack", CREATE),
    ]),
    ("Run reports", [
        ("compare_runs", "Compare runs", READ),
        ("run_certification", "Run certification", READ),
        ("run_junit", "Run JUnit XML", READ),
        ("run_flakiness", "Run flakiness report", READ),
    ]),
    ("Model insights (advisory)", [
        ("get_run_insights", "Explain run failures", READ_EXTERNAL),
        ("list_insight_predictions", "Predict scenario outcomes", READ_EXTERNAL),
        ("train_insights", "Retrain insight models", UPSERT),
    ]),
    ("Go/No-Go sign-off", [
        ("certify_run", "Certify run (Go/No-Go)", CREATE),
        ("list_signoffs", "List sign-offs", READ),
        ("get_signoff", "Get sign-off", READ),
        ("approve_signoff", "Approve sign-off", CREATE),
    ]),
    ("Load testing", [
        ("list_load_runs", "List load runs", READ),
        ("get_load_run", "Get load run", READ),
        ("compare_load_runs", "Compare load runs", READ),
        ("start_load_run", "Start load run", START),
        ("stop_load_run", "Stop load run", STOP),
        ("list_load_workers", "List load workers", READ),
        ("stop_load_worker", "Stop load worker", STOP),
        ("stop_all_load_workers", "Stop all load workers", STOP),
    ]),
    ("Code execution", [
        ("validate_code", "Validate code", READ),
        ("execute_node", "Execute node", START),
    ]),
    ("Playground", [
        ("playground_targets", "List playground targets", READ),
        ("playground_execute", "Execute against target", START),
    ]),
    ("Variables", [
        ("get_global_variables", "Get global variables", READ),
        ("set_global_variables", "Set global variables", UPSERT),
        ("get_project_variables", "Get project variables", READ),
        ("set_project_variables", "Set project variables", UPSERT),
        ("get_set_variables", "Get set variables", READ),
        ("set_set_variables", "Set set variables", UPSERT),
        ("resolve_variables", "Resolve variables", READ),
        ("scenario_variables", "Scenario variables", READ),
    ]),
    ("Data tables", [
        ("list_tables", "List data tables", READ),
        ("runtime_tables", "Runtime data tables", READ),
        ("get_table", "Get data table", READ),
        ("save_table", "Save data table", UPSERT),
        ("delete_table", "Delete data table", DELETE),
    ]),
    ("Starter flows", [
        ("list_starter_flows", "List starter flows", READ),
        ("get_starter_flow", "Get starter flow", READ),
        ("create_starter_flow", "Create starter flow", CREATE),
        ("update_starter_flow", "Update starter flow", UPSERT),
        ("delete_starter_flow", "Delete starter flow", DELETE),
    ]),
    ("Projects & search", [
        ("list_projects", "List projects", READ),
        ("get_project", "Get project", READ),
        ("create_project", "Create project", CREATE),
        ("update_project", "Update project", UPSERT),
        ("delete_project", "Delete project", DELETE),
        ("search", "Search", READ),
        ("list_secrets", "List secrets", READ),
        ("export_scenario", "Export scenario", READ),
    ]),
    ("Debug session", [
        ("debug_set_breakpoints", "Set debug breakpoints", UPSERT),
        ("debug_step", "Debug step", ADVANCE),
        ("debug_continue", "Debug continue", ADVANCE),
        ("debug_pause", "Debug pause", PAUSE),
    ]),
    ("Catalog management", [
        ("manage_catalog", "Manage catalog", READ),
        ("save_catalog_target", "Save catalog target", UPSERT),
        ("delete_catalog_target", "Delete catalog target", DELETE),
        ("restore_catalog_target", "Restore catalog target", UPSERT),
    ]),
]

# Flat (name, title, hints) list — server.py registers these. Derived from the
# grouped table so the two can never disagree.
TOOL_SPECS: list[tuple[str, str, dict]] = [
    spec for _group, specs in TOOL_GROUPS for spec in specs
]


def kind_of(hints: dict) -> str:
    """A short, human-friendly behaviour label derived from a tool's hints.

    Used by the catalog generator to badge each tool on the Integrations page.
    """
    if hints.get("readOnlyHint"):
        return "read"
    if hints.get("destructiveHint"):
        return "stop" if hints.get("openWorldHint") else "delete"
    if hints.get("openWorldHint"):
        return "run"
    if hints.get("idempotentHint"):
        return "upsert"
    return "create"


# -- resources ----------------------------------------------------------------
# (uri, tool_function_name, title, description). The function's signature must
# match the ``{placeholders}`` in the URI (none, or one id).
RESOURCE_SPECS: list[tuple[str, str, str, str]] = [
    ("payprobe://catalog", "list_step_catalog", "Step catalog",
     "Every step target with its actions, params and response fields."),
    ("payprobe://environments", "list_environments", "Environments",
     "Environments a run can target (valid environment_name values)."),
    ("payprobe://connections", "list_connections", "Connections",
     "Saved adapter connections (named endpoints a step can target)."),
    ("payprobe://formats", "list_message_formats", "Message formats",
     "Registered ISO 8583 / ISO 20022 message formats."),
    ("payprobe://scenarios", "list_scenarios", "Scenarios",
     "All saved scenarios (summaries)."),
    ("payprobe://scenarios/{scenario_id}", "get_scenario", "Scenario",
     "Full scenario document by id."),
    ("payprobe://runs", "list_runs", "Runs",
     "All runs (summaries), newest first."),
    ("payprobe://runs/{run_id}", "get_run", "Run",
     "Full run detail: per-step results + summary."),
]
