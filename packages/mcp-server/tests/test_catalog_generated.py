"""The portal's generated MCP catalog snapshot stays in sync with the registry.

If this fails, the Integrations page would advertise a tool/resource/prompt set
that differs from what the server registers. Fix by regenerating::

    cd packages/mcp-server && python -m scripts.gen_catalog
"""
from scripts import gen_catalog

from mcp_server import prompts, registry


def test_generated_catalog_is_up_to_date():
    assert gen_catalog.OUT_PATH.exists(), "generated catalog missing — run gen_catalog"
    current = gen_catalog.OUT_PATH.read_text()
    assert current == gen_catalog.render_ts(), (
        "mcp-catalog.generated.ts is stale — run "
        "`python -m scripts.gen_catalog` in packages/mcp-server"
    )


def test_catalog_covers_every_registered_tool():
    catalog = gen_catalog.build_catalog()
    in_catalog = {t["name"] for g in catalog["groups"] for t in g["tools"]}
    registered = {n for n, _, _ in registry.TOOL_SPECS}
    assert in_catalog == registered
    assert catalog["toolCount"] == len(registry.TOOL_SPECS)


def test_catalog_covers_resources_and_prompts():
    catalog = gen_catalog.build_catalog()
    assert {r["uri"] for r in catalog["resources"]} == {
        uri for uri, _, _, _ in registry.RESOURCE_SPECS
    }
    assert {p["name"] for p in catalog["prompts"]} == {
        fn.__name__ for fn, _, _ in prompts.PROMPT_SPECS
    }
