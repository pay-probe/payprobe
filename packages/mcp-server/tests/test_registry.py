"""Registration metadata: annotations, prompts and resources are wired up and
stay in sync with the tool module."""
import inspect

import pytest

from mcp_server import prompts, registry, tools

mcp_sdk = pytest.importorskip("mcp", reason="mcp SDK not installed")


# -- registry table integrity (no SDK needed) ---------------------------------

def test_every_spec_names_a_real_tool():
    for name, title, hints in registry.TOOL_SPECS:
        assert hasattr(tools, name), f"{name} not in tools.py"
        assert callable(getattr(tools, name))
        assert title and title[0].isupper(), f"{name} title looks unset"


def test_no_duplicate_tool_specs():
    names = [n for n, _, _ in registry.TOOL_SPECS]
    assert len(names) == len(set(names))


def test_every_public_tool_is_registered():
    """Guards against adding a tool to tools.py but forgetting the registry."""
    public = {
        n for n, fn in vars(tools).items()
        if inspect.isfunction(fn)
        and not n.startswith("_")
        and fn.__module__ == tools.__name__
    }
    registered = {n for n, _, _ in registry.TOOL_SPECS}
    assert public - registered == set(), f"unregistered tools: {public - registered}"


def test_hint_keys_are_valid():
    allowed = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    for name, _, hints in registry.TOOL_SPECS:
        assert set(hints) <= allowed, f"{name} has unknown hint keys"


def test_read_tools_are_not_marked_destructive():
    for name, _, hints in registry.TOOL_SPECS:
        if hints.get("readOnlyHint"):
            assert not hints.get("destructiveHint"), f"{name} is read-only but destructive"


def test_delete_tools_are_destructive():
    for name, _, hints in registry.TOOL_SPECS:
        if name.startswith(("delete_", "stop_", "cancel_")):
            assert hints.get("destructiveHint") is True, f"{name} should be destructive"


# -- end-to-end registration through FastMCP ----------------------------------

@pytest.fixture()
def server():
    from mcp_server.server import build_server
    return build_server()


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_all_tools_have_title_and_annotations(server):
    listed = _run(server.list_tools())
    assert len(listed) == len(registry.TOOL_SPECS)
    for t in listed:
        assert t.annotations is not None, f"{t.name} missing annotations"
        assert t.annotations.title, f"{t.name} missing title"


def test_destructive_hint_surfaces_on_a_delete_tool(server):
    by_name = {t.name: t for t in _run(server.list_tools())}
    assert by_name["delete_format"].annotations.destructiveHint is True
    assert by_name["get_scenario"].annotations.readOnlyHint is True


def test_prompts_registered(server):
    names = {p.name for p in _run(server.list_prompts())}
    assert names == {fn.__name__ for fn, _, _ in prompts.PROMPT_SPECS}


def test_resources_and_templates_registered(server):
    static = {r.name for r in _run(server.list_resources())}
    templates = {r.uriTemplate for r in _run(server.list_resource_templates())}
    assert "Step catalog" in static and "Scenarios" in static
    assert "payprobe://scenarios/{scenario_id}" in templates
    assert "payprobe://runs/{run_id}" in templates
