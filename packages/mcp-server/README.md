# PayProbe MCP server

Exposes PayProbe to AI agents over the **Model Context Protocol** — so a client
like Claude Desktop can discover what test steps exist and author / run
scenarios, with full knowledge of the live catalog.

## MCP capabilities

The server exposes three MCP surfaces, not just tools:

- **Tools** — 248 operations (2026-10-11) over the scenario-service,
  orchestrator, insight-service and agent-hub, in the groups of
  `registry.TOOL_GROUPS` (the portal's Integrations page lists them). Every
  tool carries a human-friendly **title** and **behaviour annotations**
  (`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`) so
  clients know which calls are safe to retry and which (`delete_*`, `stop_*`,
  `cancel_*`) should prompt for confirmation. Titles + hints live in
  `mcp_server/registry.py`.
- **Prompts** — reusable domain workflows a client can offer as templates:
  `author_scenario`, `diagnose_failed_run`, `decode_iso8583`,
  `regression_triage`. See `mcp_server/prompts.py`.
- **Resources** — read-only JSON views addressable by URI:
  `payprobe://catalog`, `payprobe://environments`, `payprobe://connections`,
  `payprobe://formats`, `payprobe://scenarios` (+ `…/{scenario_id}`),
  `payprobe://runs` (+ `…/{run_id}`).

## Tools

| Tool | What it does |
|---|---|
| `list_step_catalog` | every target + action (with params & response fields) — **start here** |
| `list_connections` / `list_message_formats` | saved connections / ISO formats |
| `list_scenarios` / `get_scenario` | browse saved scenarios |
| `assist_scenario` | draft/extend a flow from a sentence (grounded in the catalog) |
| `validate_scenario` | structure + reference check before saving |
| `create_scenario` | persist a new scenario |
| `run_scenario` / `get_run` | start a run and read its per-step results |
| `run_trend` | regression-health pass-rate over time |
| `create_participant_flow` / `save_participant_flow` | author a listening flow (trigger → logic → reply); also get, versions, duplicate/rename, delete |
| `debug_participant_flow` | run a flow once against a sample message, no listener |
| `delete_scenario` / `restore_scenario_version` / `import_scenario` | scenario lifecycle: also versions, duplicate, move; sets via `*_scenario_set` |
| `nats_cluster_health` / `list_nats_streams` | NATS cluster health and JetStream admin; `save_nats_server` / `delete_nats_server` |

**Left out on purpose** (the REST endpoints exist; there is no tool):

- Reading NATS broker entries (`GET /nats-servers`): the API returns their
  `auth` secrets in plaintext, and a read tool would hand them to the model.
  Add `list_nats_servers` once the API masks them.
- Deciding an approval, reverting a heartbeat, setting the agent pause: a
  person's act in the portal (ADR-0010). Reads (`get_approval`,
  `get_agent_pause`, `list_agent_plans`) are tools.
- Key and assistant-key material, auth/users, the assistant's own chat
  endpoints, one-shot `/admin/migrate/*`, probes, streams and HTML renders.

## Install & run

```bash
pip install -e "packages/mcp-server"          # pulls the mcp SDK
SCENARIO_API_URL=http://localhost:8000 \
RUN_API_URL=http://localhost:8001 \
  python -m mcp_server                          # stdio server
```

## Register with Claude Desktop

Add to `claude_desktop_config.json`:

```jsonc
{
  "mcpServers": {
    "payprobe": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "SCENARIO_API_URL": "http://localhost:8000",
        "RUN_API_URL": "http://localhost:8001"
      }
    }
  }
}
```

Then ask the agent things like *“list the PayProbe step catalog”*, *“build a
contactless decline scenario and validate it”*, or *“run the mock suite and show
the failures”*. The tool bodies live in `mcp_server/tools.py` (plain stdlib
HTTP) and are unit-tested independently of the MCP SDK.
