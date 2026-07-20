# PayProbe MCP server

Exposes PayProbe to AI agents over the **Model Context Protocol** — so a client
like Claude Desktop can discover what test steps exist and author / run
scenarios, with full knowledge of the live catalog.

## MCP capabilities

The server exposes three MCP surfaces, not just tools:

- **Tools** — ~100 operations over the scenario-service / orchestrator. Every
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
