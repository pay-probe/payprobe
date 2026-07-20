"""Orchestrator wiring: saved connections -> run environment + step targets."""
import pytest

from orchestrator.api import main as m


@pytest.fixture()
def conns(monkeypatch):
    """Stub the scenario-service /connections fetch."""
    docs = [
        {"name": "switch_visa", "adapter": "tcp", "protocol": "iso8583",
         "host": "10.0.1.50", "port": 7001, "correlation": {"match_mti": True}},
        {"name": "hsm_primary", "adapter": "tcp", "protocol": "header_echo",
         "host": "10.0.2.10", "port": 1500},
    ]

    async def fake_get(url):
        assert url.endswith("/connections")
        return docs

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    return docs


def _scn(*connections):
    """A scenario whose steps reference the given connection names."""
    steps = [{"id": f"s{i}", "kind": "action", "target": "tcp_iso8583",
              "action": "send_0200", "config": {"connection": c}}
             for i, c in enumerate(connections)]
    return [{"id": "sc", "steps": steps}]


async def test_only_referenced_connections_merged(conns):
    env = {"mode": "real", "adapters": {}}
    await m._attach_connections(env, _scn("switch_visa"))
    # the referenced one is injected...
    assert env["adapters"]["switch_visa"]["host"] == "10.0.1.50"
    assert env["adapters"]["switch_visa"]["correlation"] == {"match_mti": True}
    assert "name" not in env["adapters"]["switch_visa"]
    # ...but an unused saved connection is NOT pulled in (no warmup to its host)
    assert "hsm_primary" not in env["adapters"]


async def test_no_referenced_connections_leaves_env_untouched(conns):
    env = {"mode": "real", "adapters": {}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "action", "target": "tcp_iso8583", "config": {}}]}]
    await m._attach_connections(env, scs)
    assert env["adapters"] == {}


async def test_connection_wins_over_inline_env_adapter_by_default(conns):
    """Migration complete: the connection's resolved config wins over a same-named
    inline env adapter, with no flag needed (connection-wins is the default)."""
    env = {"mode": "real", "adapters": {"switch_visa": {"host": "STALE", "port": 1}}}
    await m._attach_connections(env, _scn("switch_visa"))
    assert env["adapters"]["switch_visa"]["host"] == "10.0.1.50"  # connection wins
    assert env["adapters"]["switch_visa"]["port"] == 7001


async def test_legacy_env_adapter_wins_when_opted_out(conns, monkeypatch):
    """Escape hatch: PAYPROBE_CONNECTION_OVERRIDE_WINS=0 restores the legacy
    behaviour where an inline env adapter wins over the connection."""
    monkeypatch.setattr(m, "_CONNECTION_OVERRIDE_WINS", False)
    env = {"mode": "real", "adapters": {"switch_visa": {"host": "OVERRIDE", "port": 1}}}
    await m._attach_connections(env, _scn("switch_visa"))
    assert env["adapters"]["switch_visa"]["host"] == "OVERRIDE"


async def test_flip_leaves_standalone_env_adapters_untouched(conns, monkeypatch):
    """The flip only affects connection-backed keys; a non-connection adapter the
    env defines (e.g. a mock terminal_sim) is never overwritten."""
    monkeypatch.setattr(m, "_CONNECTION_OVERRIDE_WINS", True)
    env = {"mode": "real", "adapters": {
        "terminal_sim": {"terminal_mode": "simulated"},   # not a registered connection
        "switch_visa": {"host": "STALE"},
    }}
    await m._attach_connections(env, _scn("switch_visa"))
    assert env["adapters"]["terminal_sim"] == {"terminal_mode": "simulated"}
    assert env["adapters"]["switch_visa"]["host"] == "10.0.1.50"


async def test_flip_with_env_override_resolves_full_matrix(monkeypatch):
    """With the flag on and a named environment, the connection's
    base ⊕ environment_overrides[env] is what lands — beating any inline value."""
    _ovr_conns(monkeypatch)
    monkeypatch.setattr(m, "_CONNECTION_OVERRIDE_WINS", True)
    env = {"mode": "real", "adapters": {"switch_visa": {"host": "STALE", "port": 1}}}
    await m._attach_connections(env, _scn("switch_visa"), "staging")
    merged = env["adapters"]["switch_visa"]
    assert merged["host"] == "stg.host" and merged["port"] == 9001


async def test_step_target_repointed_to_chosen_connection(conns):
    env = {"mode": "real", "adapters": {}}
    scs = [{
        "id": "sc", "steps": [
            {"id": "s1", "kind": "action", "target": "tcp_iso8583",
             "action": "send_0200", "config": {"connection": "switch_visa"}},
            {"id": "s2", "kind": "action", "target": "tcp_iso8583",
             "action": "send_0800", "config": {}},  # no binding -> unchanged
        ],
    }]
    await m._attach_connections(env, scs)
    assert scs[0]["steps"][0]["target"] == "switch_visa"  # repointed
    assert scs[0]["steps"][0]["action"] == "send_0200"     # action preserved
    # original catalog target retained for the report (step type + connection)
    assert scs[0]["steps"][0]["config"]["_catalog_target"] == "tcp_iso8583"
    assert scs[0]["steps"][1]["target"] == "tcp_iso8583"   # untouched
    assert "_catalog_target" not in scs[0]["steps"][1].get("config", {})


def _ovr_conns(monkeypatch):
    """A connection carrying per-environment parameter overrides."""
    docs = [
        {"name": "switch_visa", "adapter": "tcp", "protocol": "iso8583",
         "host": "base.host", "port": 7001,
         "environment_overrides": {
             "staging": {"host": "stg.host", "port": 9001},
             "prod": {"host": "prod.host"},
         }},
    ]

    async def fake_get(url):
        return docs

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    return docs


async def test_env_override_applied_for_active_environment(monkeypatch):
    """The active environment's overrides win over the connection's base
    config; override metadata never leaks into the adapter block."""
    _ovr_conns(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    await m._attach_connections(env, _scn("switch_visa"), "staging")
    merged = env["adapters"]["switch_visa"]
    assert merged["host"] == "stg.host"  # overridden
    assert merged["port"] == 9001        # overridden
    assert "environment_overrides" not in merged
    assert "name" not in merged


async def test_env_override_partial_keeps_base_for_unset_keys(monkeypatch):
    """A partial override only replaces the keys it sets (prod overrides host
    but not port, so port falls back to the base)."""
    _ovr_conns(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    await m._attach_connections(env, _scn("switch_visa"), "prod")
    merged = env["adapters"]["switch_visa"]
    assert merged["host"] == "prod.host"  # overridden
    assert merged["port"] == 7001         # base retained


async def test_no_override_for_unconfigured_or_unnamed_env(monkeypatch):
    """An environment with no overrides (or an unnamed run) uses the base
    config, and the metadata is still stripped."""
    _ovr_conns(monkeypatch)
    for env_name in ("dev", None):
        env = {"mode": "real", "adapters": {}}
        await m._attach_connections(env, _scn("switch_visa"), env_name)
        merged = env["adapters"]["switch_visa"]
        assert merged["host"] == "base.host"
        assert merged["port"] == 7001
        assert "environment_overrides" not in merged


# -- Phase C: default-connection resolution for unbound steps -----------------

def _default_conns(monkeypatch):
    """A default connection for tcp/iso8583, named differently from the catalog
    target to prove resolution is by adapter TYPE, not by name."""
    docs = [
        {"name": "primary_switch", "adapter": "tcp", "protocol": "iso8583",
         "host": "default.host", "port": 7000, "default": True,
         "environment_overrides": {"staging": {"host": "stg.host"}}},
        {"name": "other", "adapter": "tcp", "protocol": "iso8583",
         "host": "x", "port": 1},  # not a default
    ]

    async def fake_get(url):
        return docs

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    return docs


def _bare(*targets):
    """A scenario whose steps name a catalog target but NO connection."""
    steps = [{"id": f"s{i}", "kind": "action", "target": t,
              "action": "send_0200", "config": {}}
             for i, t in enumerate(targets)]
    return [{"id": "sc", "steps": steps}]


async def test_unbound_step_resolves_to_type_default_by_default(monkeypatch):
    # default resolution is now ON by default — no flag needed
    _default_conns(monkeypatch)
    env = {"mode": "real", "adapters": {}}
    scs = _bare("tcp_iso8583")
    await m._attach_connections(env, scs)
    # repointed to the default connection of its type (by type, not name)
    assert scs[0]["steps"][0]["target"] == "primary_switch"
    assert env["adapters"]["primary_switch"]["host"] == "default.host"
    assert scs[0]["steps"][0]["config"]["_catalog_target"] == "tcp_iso8583"


async def test_unbound_default_applies_env_override(monkeypatch):
    _default_conns(monkeypatch)
    env = {"adapters": {}}
    await m._attach_connections(env, (scs := _bare("tcp_iso8583")), "staging")
    assert env["adapters"]["primary_switch"]["host"] == "stg.host"  # matrix applied
    assert scs[0]["steps"][0]["target"] == "primary_switch"


async def test_no_default_for_type_falls_back_to_env_adapter(monkeypatch):
    # a type with no default degrades gracefully — the fallback is retained
    _default_conns(monkeypatch)  # only a tcp/iso8583 default exists
    env = {"adapters": {"http": {"base_url": "x"}}}
    scs = _bare("http")  # no default for http -> untouched, hits env adapter
    await m._attach_connections(env, scs)
    assert scs[0]["steps"][0]["target"] == "http"
    assert "_catalog_target" not in scs[0]["steps"][0].get("config", {})


async def test_default_resolution_opt_out_leaves_unbound_steps(monkeypatch):
    # escape hatch: PAYPROBE_DEFAULT_CONNECTION_RESOLUTION=0 turns it off entirely
    _default_conns(monkeypatch)
    monkeypatch.setattr(m, "_DEFAULT_CONNECTION_RESOLUTION", False)
    env = {"adapters": {}}
    scs = _bare("tcp_iso8583")
    await m._attach_connections(env, scs)
    assert scs[0]["steps"][0]["target"] == "tcp_iso8583"  # unchanged
    assert env["adapters"] == {}


async def test_bound_step_keeps_its_connection_over_the_default(monkeypatch):
    _default_conns(monkeypatch)
    env = {"adapters": {}}
    scs = _scn("other")  # explicitly bound to 'other', not the default
    await m._attach_connections(env, scs)
    assert scs[0]["steps"][0]["target"] == "other"
    assert "primary_switch" not in env["adapters"]


async def test_missing_connections_service_is_best_effort(monkeypatch):
    async def boom(url):
        raise RuntimeError("service down")

    monkeypatch.setattr(m, "_http_get_json", boom)
    env = {"adapters": {}}
    # a step references a connection, so the (failing) fetch IS attempted
    scs = _scn("switch_visa")
    await m._attach_connections(env, scs)  # must not raise
    assert env["adapters"] == {}              # fetch failed -> nothing merged
    assert scs[0]["steps"][0]["target"] == "switch_visa"  # repoint still applied
