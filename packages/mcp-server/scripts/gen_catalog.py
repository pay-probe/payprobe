"""Generate the portal's MCP catalog snapshot from the registry.

The Integrations page renders the full MCP surface — every tool (grouped, with
its behaviour badge), every resource and every prompt. Rather than hand-maintain
that list in TypeScript (which is exactly how it went stale before), we *derive*
it from the same :mod:`mcp_server.registry` and :mod:`mcp_server.prompts` tables
the server registers from, and emit a generated TS module.

Run after changing the tool/resource/prompt tables::

    python -m scripts.gen_catalog          # writes the TS file
    python -m scripts.gen_catalog --check   # exit 1 if the file is stale

``tests/test_catalog_generated.py`` runs ``--check`` so CI fails on drift.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcp_server import prompts, registry

# mcp-server/scripts/gen_catalog.py -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_PATH = (
    _REPO_ROOT
    / "packages/portal/src/app/integrations/mcp-catalog.generated.ts"
)

_HEADER = (
    "// AUTO-GENERATED — do not edit by hand.\n"
    "// Source: packages/mcp-server (registry.py + prompts.py).\n"
    "// Regenerate: cd packages/mcp-server && python -m scripts.gen_catalog\n"
    "// CI guards freshness via tests/test_catalog_generated.py.\n"
)


def build_catalog() -> dict:
    """The catalog payload, as plain JSON-able data."""
    groups = [
        {
            "name": group,
            "tools": [
                {"name": n, "title": t, "kind": registry.kind_of(h)}
                for n, t, h in specs
            ],
        }
        for group, specs in registry.TOOL_GROUPS
    ]
    resources = [
        {"uri": uri, "title": title, "description": desc}
        for uri, _fn, title, desc in registry.RESOURCE_SPECS
    ]
    prompt_specs = [
        {"name": fn.__name__, "title": title, "description": desc}
        for fn, title, desc in prompts.PROMPT_SPECS
    ]
    return {
        "toolCount": len(registry.TOOL_SPECS),
        "groups": groups,
        "resources": resources,
        "prompts": prompt_specs,
    }


def render_ts() -> str:
    catalog = build_catalog()
    body = json.dumps(catalog, indent=2)
    return (
        f"{_HEADER}\n"
        "export interface McpCatalogTool {\n"
        "  name: string;\n"
        '  title: string;\n'
        '  /** read | upsert | create | delete | run | stop */\n'
        "  kind: string;\n"
        "}\n"
        "export interface McpCatalogGroup {\n"
        "  name: string;\n"
        "  tools: McpCatalogTool[];\n"
        "}\n"
        "export interface McpCatalogResource {\n"
        "  uri: string;\n"
        "  title: string;\n"
        "  description: string;\n"
        "}\n"
        "export interface McpCatalogPrompt {\n"
        "  name: string;\n"
        "  title: string;\n"
        "  description: string;\n"
        "}\n"
        "export interface McpCatalog {\n"
        "  toolCount: number;\n"
        "  groups: McpCatalogGroup[];\n"
        "  resources: McpCatalogResource[];\n"
        "  prompts: McpCatalogPrompt[];\n"
        "}\n\n"
        f"export const MCP_CATALOG: McpCatalog = {body};\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the generated file is out of date")
    args = ap.parse_args(argv)

    rendered = render_ts()
    if args.check:
        current = OUT_PATH.read_text() if OUT_PATH.exists() else ""
        if current != rendered:
            print(
                f"{OUT_PATH} is stale — run `python -m scripts.gen_catalog`",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT_PATH} is up to date.")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(rendered)
    print(f"wrote {OUT_PATH} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
