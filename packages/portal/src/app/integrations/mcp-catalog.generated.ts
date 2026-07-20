// AUTO-GENERATED — do not edit by hand.
// Source: packages/mcp-server (registry.py + prompts.py).
// Regenerate: cd packages/mcp-server && python -m scripts.gen_catalog
// CI guards freshness via tests/test_catalog_generated.py.

export interface McpCatalogTool {
  name: string;
  title: string;
  /** read | upsert | create | delete | run | stop */
  kind: string;
}
export interface McpCatalogGroup {
  name: string;
  tools: McpCatalogTool[];
}
export interface McpCatalogResource {
  uri: string;
  title: string;
  description: string;
}
export interface McpCatalogPrompt {
  name: string;
  title: string;
  description: string;
}
export interface McpCatalog {
  toolCount: number;
  groups: McpCatalogGroup[];
  resources: McpCatalogResource[];
  prompts: McpCatalogPrompt[];
}

export const MCP_CATALOG: McpCatalog = {
  "toolCount": 158,
  "groups": [
    {
      "name": "Discovery",
      "tools": [
        {
          "name": "list_step_catalog",
          "title": "List step catalog",
          "kind": "read"
        },
        {
          "name": "list_connections",
          "title": "List connections",
          "kind": "read"
        },
        {
          "name": "list_message_formats",
          "title": "List message formats",
          "kind": "read"
        },
        {
          "name": "list_environments",
          "title": "List environments",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Environments",
      "tools": [
        {
          "name": "get_environment",
          "title": "Get environment",
          "kind": "read"
        },
        {
          "name": "save_environment",
          "title": "Save environment",
          "kind": "upsert"
        },
        {
          "name": "delete_environment",
          "title": "Delete environment",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Scenarios",
      "tools": [
        {
          "name": "list_scenarios",
          "title": "List scenarios",
          "kind": "read"
        },
        {
          "name": "get_scenario",
          "title": "Get scenario",
          "kind": "read"
        },
        {
          "name": "assist_scenario",
          "title": "Draft scenario from prompt",
          "kind": "read"
        },
        {
          "name": "validate_scenario",
          "title": "Validate scenario",
          "kind": "read"
        },
        {
          "name": "create_scenario",
          "title": "Create scenario",
          "kind": "create"
        },
        {
          "name": "update_scenario",
          "title": "Update scenario",
          "kind": "upsert"
        }
      ]
    },
    {
      "name": "ISO 8583 inspector",
      "tools": [
        {
          "name": "iso8583_analyze",
          "title": "Decode ISO 8583 message",
          "kind": "read"
        },
        {
          "name": "iso8583_build",
          "title": "Build ISO 8583 message",
          "kind": "read"
        },
        {
          "name": "iso8583_diff",
          "title": "Diff ISO 8583 messages",
          "kind": "read"
        },
        {
          "name": "iso8583_tlv_build",
          "title": "Build BER-TLV",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Runs",
      "tools": [
        {
          "name": "run_scenario",
          "title": "Run scenario",
          "kind": "run"
        },
        {
          "name": "get_run",
          "title": "Get run",
          "kind": "read"
        },
        {
          "name": "list_runs",
          "title": "List runs",
          "kind": "read"
        },
        {
          "name": "cancel_run",
          "title": "Cancel run",
          "kind": "delete"
        },
        {
          "name": "diagnose_run",
          "title": "Diagnose run",
          "kind": "read"
        },
        {
          "name": "run_trend",
          "title": "Run trend",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Connections",
      "tools": [
        {
          "name": "get_connection",
          "title": "Get connection",
          "kind": "read"
        },
        {
          "name": "save_connection",
          "title": "Save connection",
          "kind": "upsert"
        },
        {
          "name": "delete_connection",
          "title": "Delete connection",
          "kind": "delete"
        },
        {
          "name": "test_connection",
          "title": "Test connection",
          "kind": "read"
        },
        {
          "name": "diagnose_platform",
          "title": "Diagnose platform connectivity",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Simulators",
      "tools": [
        {
          "name": "list_simulators",
          "title": "List simulators",
          "kind": "read"
        },
        {
          "name": "get_simulator",
          "title": "Get simulator",
          "kind": "read"
        },
        {
          "name": "start_simulator",
          "title": "Start simulator",
          "kind": "run"
        },
        {
          "name": "stop_simulator",
          "title": "Stop simulator",
          "kind": "stop"
        },
        {
          "name": "save_running_simulator",
          "title": "Save running simulator",
          "kind": "create"
        },
        {
          "name": "list_saved_simulators",
          "title": "List saved simulators",
          "kind": "read"
        },
        {
          "name": "get_saved_simulator",
          "title": "Get saved simulator",
          "kind": "read"
        },
        {
          "name": "create_saved_simulator",
          "title": "Create saved simulator",
          "kind": "create"
        },
        {
          "name": "start_saved_simulator",
          "title": "Start saved simulator",
          "kind": "run"
        },
        {
          "name": "stop_saved_simulator",
          "title": "Stop saved simulator",
          "kind": "stop"
        },
        {
          "name": "set_saved_simulator_enabled",
          "title": "Enable saved simulator",
          "kind": "upsert"
        },
        {
          "name": "delete_saved_simulator",
          "title": "Delete saved simulator",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "payShield HSM",
      "tools": [
        {
          "name": "start_payshield_simulator",
          "title": "Start payShield simulator",
          "kind": "run"
        },
        {
          "name": "hsm_command",
          "title": "Send HSM command",
          "kind": "run"
        },
        {
          "name": "run_hsm_example",
          "title": "Run HSM example scenario",
          "kind": "run"
        }
      ]
    },
    {
      "name": "Participant flows & topologies",
      "tools": [
        {
          "name": "list_participant_flows",
          "title": "List participant flows",
          "kind": "read"
        },
        {
          "name": "list_participant_groups",
          "title": "List participant groups",
          "kind": "read"
        },
        {
          "name": "get_participant_group",
          "title": "Get participant group",
          "kind": "read"
        },
        {
          "name": "create_participant_group",
          "title": "Create participant group",
          "kind": "create"
        },
        {
          "name": "save_participant_group",
          "title": "Save participant group",
          "kind": "upsert"
        },
        {
          "name": "delete_participant_group",
          "title": "Delete participant group",
          "kind": "delete"
        },
        {
          "name": "list_participants",
          "title": "List running participants",
          "kind": "read"
        },
        {
          "name": "list_topology_runs",
          "title": "List network-flow runs",
          "kind": "read"
        },
        {
          "name": "list_network_traces",
          "title": "List network traces",
          "kind": "read"
        },
        {
          "name": "get_network_trace",
          "title": "Get correlated trace",
          "kind": "read"
        },
        {
          "name": "set_trace_capture",
          "title": "Toggle trace capture",
          "kind": "upsert"
        }
      ]
    },
    {
      "name": "Network flows",
      "tools": [
        {
          "name": "list_network_flows",
          "title": "List network flows",
          "kind": "read"
        },
        {
          "name": "get_network_flow",
          "title": "Get network flow",
          "kind": "read"
        },
        {
          "name": "save_network_flow",
          "title": "Save network flow",
          "kind": "upsert"
        },
        {
          "name": "delete_network_flow",
          "title": "Delete network flow",
          "kind": "delete"
        },
        {
          "name": "validate_network_flow",
          "title": "Validate network flow",
          "kind": "read"
        },
        {
          "name": "plan_network_flow",
          "title": "Plan network flow",
          "kind": "read"
        },
        {
          "name": "start_network_flow",
          "title": "Start network flow",
          "kind": "run"
        },
        {
          "name": "stop_network_flow",
          "title": "Stop network-flow run",
          "kind": "stop"
        },
        {
          "name": "install_showcase",
          "title": "Install showcase demo network",
          "kind": "run"
        }
      ]
    },
    {
      "name": "Chaos & resilience",
      "tools": [
        {
          "name": "get_simulator_chaos",
          "title": "Get simulator chaos dial",
          "kind": "read"
        },
        {
          "name": "set_simulator_chaos",
          "title": "Set simulator chaos",
          "kind": "run"
        },
        {
          "name": "start_chaos_storm",
          "title": "Start chaos storm",
          "kind": "run"
        },
        {
          "name": "cancel_chaos_storm",
          "title": "Cancel chaos storm",
          "kind": "stop"
        },
        {
          "name": "list_resilience_runs",
          "title": "List resilience runs",
          "kind": "read"
        },
        {
          "name": "get_resilience_run",
          "title": "Get resilience run",
          "kind": "read"
        },
        {
          "name": "start_resilience_run",
          "title": "Start resilience run",
          "kind": "run"
        },
        {
          "name": "cancel_resilience_run",
          "title": "Cancel resilience run",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Schedules",
      "tools": [
        {
          "name": "list_schedules",
          "title": "List schedules",
          "kind": "read"
        },
        {
          "name": "create_schedule",
          "title": "Create schedule",
          "kind": "create"
        },
        {
          "name": "run_schedule_now",
          "title": "Run schedule now",
          "kind": "run"
        },
        {
          "name": "set_schedule_enabled",
          "title": "Enable/pause schedule",
          "kind": "upsert"
        },
        {
          "name": "delete_schedule",
          "title": "Delete schedule",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Message formats",
      "tools": [
        {
          "name": "get_format",
          "title": "Get message format",
          "kind": "read"
        },
        {
          "name": "create_format",
          "title": "Create message format",
          "kind": "create"
        },
        {
          "name": "update_format",
          "title": "Update message format",
          "kind": "upsert"
        },
        {
          "name": "clone_format",
          "title": "Clone message format",
          "kind": "create"
        },
        {
          "name": "delete_format",
          "title": "Delete message format",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Test data",
      "tools": [
        {
          "name": "list_card_pools",
          "title": "List card pools",
          "kind": "read"
        },
        {
          "name": "get_card_pool",
          "title": "Get card pool",
          "kind": "read"
        },
        {
          "name": "save_card_pool",
          "title": "Save card pool",
          "kind": "upsert"
        },
        {
          "name": "delete_card_pool",
          "title": "Delete card pool",
          "kind": "delete"
        },
        {
          "name": "list_bin_ranges",
          "title": "List BIN ranges",
          "kind": "read"
        },
        {
          "name": "get_bin_range",
          "title": "Get BIN range",
          "kind": "read"
        },
        {
          "name": "save_bin_range",
          "title": "Save BIN range",
          "kind": "upsert"
        },
        {
          "name": "delete_bin_range",
          "title": "Delete BIN range",
          "kind": "delete"
        },
        {
          "name": "generate_bin_pans",
          "title": "Generate PANs from BIN",
          "kind": "create"
        },
        {
          "name": "list_terminal_pools",
          "title": "List terminal pools",
          "kind": "read"
        },
        {
          "name": "get_terminal_pool",
          "title": "Get terminal pool",
          "kind": "read"
        },
        {
          "name": "save_terminal_pool",
          "title": "Save terminal pool",
          "kind": "upsert"
        },
        {
          "name": "delete_terminal_pool",
          "title": "Delete terminal pool",
          "kind": "delete"
        },
        {
          "name": "list_test_keys",
          "title": "List test keys",
          "kind": "read"
        },
        {
          "name": "get_test_key",
          "title": "Get test key",
          "kind": "read"
        },
        {
          "name": "save_test_key",
          "title": "Save test key",
          "kind": "upsert"
        },
        {
          "name": "delete_test_key",
          "title": "Delete test key",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Packs",
      "tools": [
        {
          "name": "list_packs",
          "title": "List packs",
          "kind": "read"
        },
        {
          "name": "get_pack",
          "title": "Get pack",
          "kind": "read"
        },
        {
          "name": "install_pack",
          "title": "Install pack",
          "kind": "create"
        }
      ]
    },
    {
      "name": "Run reports",
      "tools": [
        {
          "name": "compare_runs",
          "title": "Compare runs",
          "kind": "read"
        },
        {
          "name": "run_certification",
          "title": "Run certification",
          "kind": "read"
        },
        {
          "name": "run_junit",
          "title": "Run JUnit XML",
          "kind": "read"
        },
        {
          "name": "run_flakiness",
          "title": "Run flakiness report",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Model insights (advisory)",
      "tools": [
        {
          "name": "get_run_insights",
          "title": "Explain run failures",
          "kind": "read"
        },
        {
          "name": "list_insight_predictions",
          "title": "Predict scenario outcomes",
          "kind": "read"
        },
        {
          "name": "train_insights",
          "title": "Retrain insight models",
          "kind": "upsert"
        }
      ]
    },
    {
      "name": "Go/No-Go sign-off",
      "tools": [
        {
          "name": "certify_run",
          "title": "Certify run (Go/No-Go)",
          "kind": "create"
        },
        {
          "name": "list_signoffs",
          "title": "List sign-offs",
          "kind": "read"
        },
        {
          "name": "get_signoff",
          "title": "Get sign-off",
          "kind": "read"
        },
        {
          "name": "approve_signoff",
          "title": "Approve sign-off",
          "kind": "create"
        }
      ]
    },
    {
      "name": "Load testing",
      "tools": [
        {
          "name": "list_load_runs",
          "title": "List load runs",
          "kind": "read"
        },
        {
          "name": "get_load_run",
          "title": "Get load run",
          "kind": "read"
        },
        {
          "name": "compare_load_runs",
          "title": "Compare load runs",
          "kind": "read"
        },
        {
          "name": "start_load_run",
          "title": "Start load run",
          "kind": "run"
        },
        {
          "name": "stop_load_run",
          "title": "Stop load run",
          "kind": "stop"
        },
        {
          "name": "list_load_workers",
          "title": "List load workers",
          "kind": "read"
        },
        {
          "name": "stop_load_worker",
          "title": "Stop load worker",
          "kind": "stop"
        },
        {
          "name": "stop_all_load_workers",
          "title": "Stop all load workers",
          "kind": "stop"
        }
      ]
    },
    {
      "name": "Code execution",
      "tools": [
        {
          "name": "validate_code",
          "title": "Validate code",
          "kind": "read"
        },
        {
          "name": "execute_node",
          "title": "Execute node",
          "kind": "run"
        }
      ]
    },
    {
      "name": "Playground",
      "tools": [
        {
          "name": "playground_targets",
          "title": "List playground targets",
          "kind": "read"
        },
        {
          "name": "playground_execute",
          "title": "Execute against target",
          "kind": "run"
        }
      ]
    },
    {
      "name": "Variables",
      "tools": [
        {
          "name": "get_global_variables",
          "title": "Get global variables",
          "kind": "read"
        },
        {
          "name": "set_global_variables",
          "title": "Set global variables",
          "kind": "upsert"
        },
        {
          "name": "get_project_variables",
          "title": "Get project variables",
          "kind": "read"
        },
        {
          "name": "set_project_variables",
          "title": "Set project variables",
          "kind": "upsert"
        },
        {
          "name": "get_set_variables",
          "title": "Get set variables",
          "kind": "read"
        },
        {
          "name": "set_set_variables",
          "title": "Set set variables",
          "kind": "upsert"
        },
        {
          "name": "resolve_variables",
          "title": "Resolve variables",
          "kind": "read"
        },
        {
          "name": "scenario_variables",
          "title": "Scenario variables",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Data tables",
      "tools": [
        {
          "name": "list_tables",
          "title": "List data tables",
          "kind": "read"
        },
        {
          "name": "runtime_tables",
          "title": "Runtime data tables",
          "kind": "read"
        },
        {
          "name": "get_table",
          "title": "Get data table",
          "kind": "read"
        },
        {
          "name": "save_table",
          "title": "Save data table",
          "kind": "upsert"
        },
        {
          "name": "delete_table",
          "title": "Delete data table",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Starter flows",
      "tools": [
        {
          "name": "list_starter_flows",
          "title": "List starter flows",
          "kind": "read"
        },
        {
          "name": "get_starter_flow",
          "title": "Get starter flow",
          "kind": "read"
        },
        {
          "name": "create_starter_flow",
          "title": "Create starter flow",
          "kind": "create"
        },
        {
          "name": "update_starter_flow",
          "title": "Update starter flow",
          "kind": "upsert"
        },
        {
          "name": "delete_starter_flow",
          "title": "Delete starter flow",
          "kind": "delete"
        }
      ]
    },
    {
      "name": "Projects & search",
      "tools": [
        {
          "name": "list_projects",
          "title": "List projects",
          "kind": "read"
        },
        {
          "name": "get_project",
          "title": "Get project",
          "kind": "read"
        },
        {
          "name": "create_project",
          "title": "Create project",
          "kind": "create"
        },
        {
          "name": "update_project",
          "title": "Update project",
          "kind": "upsert"
        },
        {
          "name": "delete_project",
          "title": "Delete project",
          "kind": "delete"
        },
        {
          "name": "search",
          "title": "Search",
          "kind": "read"
        },
        {
          "name": "list_secrets",
          "title": "List secrets",
          "kind": "read"
        },
        {
          "name": "export_scenario",
          "title": "Export scenario",
          "kind": "read"
        }
      ]
    },
    {
      "name": "Debug session",
      "tools": [
        {
          "name": "debug_set_breakpoints",
          "title": "Set debug breakpoints",
          "kind": "upsert"
        },
        {
          "name": "debug_step",
          "title": "Debug step",
          "kind": "create"
        },
        {
          "name": "debug_continue",
          "title": "Debug continue",
          "kind": "create"
        },
        {
          "name": "debug_pause",
          "title": "Debug pause",
          "kind": "upsert"
        }
      ]
    },
    {
      "name": "Catalog management",
      "tools": [
        {
          "name": "manage_catalog",
          "title": "Manage catalog",
          "kind": "read"
        },
        {
          "name": "save_catalog_target",
          "title": "Save catalog target",
          "kind": "upsert"
        },
        {
          "name": "delete_catalog_target",
          "title": "Delete catalog target",
          "kind": "delete"
        },
        {
          "name": "restore_catalog_target",
          "title": "Restore catalog target",
          "kind": "upsert"
        }
      ]
    }
  ],
  "resources": [
    {
      "uri": "payprobe://catalog",
      "title": "Step catalog",
      "description": "Every step target with its actions, params and response fields."
    },
    {
      "uri": "payprobe://environments",
      "title": "Environments",
      "description": "Environments a run can target (valid environment_name values)."
    },
    {
      "uri": "payprobe://connections",
      "title": "Connections",
      "description": "Saved adapter connections (named endpoints a step can target)."
    },
    {
      "uri": "payprobe://formats",
      "title": "Message formats",
      "description": "Registered ISO 8583 / ISO 20022 message formats."
    },
    {
      "uri": "payprobe://scenarios",
      "title": "Scenarios",
      "description": "All saved scenarios (summaries)."
    },
    {
      "uri": "payprobe://scenarios/{scenario_id}",
      "title": "Scenario",
      "description": "Full scenario document by id."
    },
    {
      "uri": "payprobe://runs",
      "title": "Runs",
      "description": "All runs (summaries), newest first."
    },
    {
      "uri": "payprobe://runs/{run_id}",
      "title": "Run",
      "description": "Full run detail: per-step results + summary."
    }
  ],
  "prompts": [
    {
      "name": "author_scenario",
      "title": "Author a scenario",
      "description": "Draft, validate and save a scenario from a plain-English intent."
    },
    {
      "name": "diagnose_failed_run",
      "title": "Diagnose a failed run",
      "description": "Triage a failed run, check for regression, and propose a fix."
    },
    {
      "name": "decode_iso8583",
      "title": "Decode an ISO 8583 message",
      "description": "Decode a raw wire message field by field."
    },
    {
      "name": "regression_triage",
      "title": "Regression triage",
      "description": "Review pass-rate trend and surface flaky scenarios."
    }
  ]
};
