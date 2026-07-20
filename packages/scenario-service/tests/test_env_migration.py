"""Connection/Environment migration — Phase 0 classify + Phase 1 backfill.

Covers the additive/reversible half: partitioning, minimal override diffs,
idempotency, standalone-vs-needs-connection reporting, and a non-destructive
apply into the ConnectionStore. See docs/history/CONNECTION-ENV-MIGRATION-PLAN.md.
"""
from api.connection_store import ConnectionDraft, ConnectionStore
from api.environment_store import EnvironmentDraft, EnvironmentStore
from api.env_migration import (
    apply_backfill,
    apply_seed_defaults,
    apply_slim,
    classify_adapters,
    connection_base,
    effective_under_env,
    find_collisions,
    override_diff,
    plan_backfill,
    plan_seed_defaults,
    plan_slim,
)


# -- Phase 0: classify --------------------------------------------------------

def test_classify_splits_connection_backed_from_standalone():
    env_adapters = {
        "switch": {"host": "h", "port": 7000},
        "terminal_sim": {"terminal_mode": "simulated"},
        "hsm": {"pkcs11_lib_path": "/lib"},
    }
    backed, standalone = classify_adapters(env_adapters, {"switch"})
    assert set(backed) == {"switch"}
    assert set(standalone) == {"terminal_sim", "hsm"}


def test_connection_base_strips_metadata():
    doc = {
        "name": "switch", "mode": "outbound", "description": "x", "disabled": False,
        "environment_overrides": {"prod": {"host": "p"}},
        "host": "h", "port": 7000, "protocol": "iso8583",
    }
    assert connection_base(doc) == {"host": "h", "port": 7000, "protocol": "iso8583"}


# -- override diff ------------------------------------------------------------

def test_override_diff_keeps_only_differing_keys():
    base = {"host": "base", "port": 7000, "protocol": "iso8583"}
    env_cfg = {"host": "envhost", "port": 7000, "protocol": "iso8583"}
    # only host differs
    assert override_diff(env_cfg, base) == {"host": "envhost"}


def test_override_diff_includes_keys_absent_from_base():
    assert override_diff({"host": "h", "extra": 1}, {"host": "h"}) == {"extra": 1}


# -- Phase 1: plan ------------------------------------------------------------

def test_plan_backfills_differing_values_into_matrix():
    connections = {"switch": {"name": "switch", "host": "base", "port": 7000}}
    environments = [
        ("prod", {"switch": {"host": "prod-host", "port": 7000}}),
        ("dev", {"switch": {"host": "dev-host", "port": 7000}}),
    ]
    plan = plan_backfill(environments, connections)
    assert plan.set_overrides == {
        "switch": {"prod": {"host": "prod-host"}, "dev": {"host": "dev-host"}}
    }
    assert plan.summary()["override_writes"] == 2
    assert not plan.is_empty


def test_plan_is_idempotent_against_existing_overrides():
    connections = {
        "switch": {
            "name": "switch", "host": "base", "port": 7000,
            "environment_overrides": {"prod": {"host": "prod-host"}},
        }
    }
    environments = [("prod", {"switch": {"host": "prod-host", "port": 7000}})]
    plan = plan_backfill(environments, connections)
    assert plan.is_empty
    assert plan.already_current == 1


def test_plan_skips_when_env_matches_base():
    connections = {"switch": {"name": "switch", "host": "h", "port": 7000}}
    environments = [("prod", {"switch": {"host": "h", "port": 7000}})]
    plan = plan_backfill(environments, connections)
    assert plan.is_empty and plan.already_current == 1


def test_plan_reports_unmatched_and_flags_network_shaped():
    connections = {}  # nothing registered
    environments = [
        ("prod", {
            "terminal_sim": {"terminal_mode": "simulated"},   # standalone, not network
            "switch": {"host": "h", "port": 7000},            # looks like a connection
        }),
    ]
    plan = plan_backfill(environments, connections)
    assert plan.is_empty  # nothing backfilled — no registered connection
    summary = plan.summary()
    assert {"environment": "prod", "adapter": "switch"} in summary["needs_connection"]
    assert {"environment": "prod", "adapter": "terminal_sim"} not in summary["needs_connection"]


# -- Phase 1: apply -----------------------------------------------------------

def test_apply_writes_overrides_and_is_non_destructive():
    conns = ConnectionStore(":memory:")
    conns.upsert("switch", ConnectionDraft(
        host="base", port=7000, protocol="iso8583", description="primary switch"))

    environments = [("prod", {"switch": {"host": "prod-host", "port": 7000}})]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    plan = plan_backfill(environments, connections)
    written = apply_backfill(plan, conns)

    assert written == 1
    after = conns.get("switch")
    assert after.environment_overrides == {"prod": {"host": "prod-host"}}
    # base config + unrelated fields untouched
    assert after.host == "base" and after.port == 7000
    assert after.description == "primary switch"

    # re-planning against the updated store yields nothing to do (idempotent)
    connections2 = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    assert plan_backfill(environments, connections2).is_empty


def test_apply_merges_onto_existing_override_for_other_env():
    conns = ConnectionStore(":memory:")
    conns.upsert("switch", ConnectionDraft(
        host="base", port=7000,
        environment_overrides={"dev": {"host": "dev-host"}}))

    environments = [("prod", {"switch": {"host": "prod-host", "port": 7000}})]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    apply_backfill(plan_backfill(environments, connections), conns)

    ov = conns.get("switch").environment_overrides
    assert ov == {"dev": {"host": "dev-host"}, "prod": {"host": "prod-host"}}


def test_apply_encrypts_secret_override_at_rest(tmp_path):
    from api.crypto import SecretBox
    import json

    path = tmp_path / "connections.json"
    box = SecretBox(SecretBox.generate_key())
    conns = ConnectionStore(str(path), secret_box=box)
    conns.upsert("rest", ConnectionDraft(adapter="tcp", host="base"))

    environments = [("prod", {"rest": {"host": "p", "api_key": "SECRET-KEY"}})]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    apply_backfill(plan_backfill(environments, connections), conns)

    on_disk = json.loads(path.read_text())["connections"]["rest"]
    stored = on_disk["environment_overrides"]["prod"]["api_key"]
    assert stored.startswith("enc:v1:") and "SECRET-KEY" not in stored
    # in-memory / reloaded value is plaintext for run assembly
    conns2 = ConnectionStore(str(path), secret_box=box)
    assert conns2.get("rest").environment_overrides["prod"]["api_key"] == "SECRET-KEY"


# -- Phase 3: collision lint --------------------------------------------------

def test_effective_under_env_merges_override_over_base():
    doc = {"name": "switch", "host": "base", "port": 7000,
           "environment_overrides": {"prod": {"host": "prod-host"}}}
    assert effective_under_env(doc, "prod") == {"host": "prod-host", "port": 7000}
    assert effective_under_env(doc, "dev") == {"host": "base", "port": 7000}


def test_collision_flagged_when_env_disagrees_with_connection():
    connections = {"switch": {"name": "switch", "host": "base", "port": 7000}}
    environments = [("prod", {"switch": {"host": "prod-host", "port": 7000}})]
    cols = find_collisions(environments, connections)
    assert len(cols) == 1
    c = cols[0].as_dict()
    assert c["differing_keys"] == ["host"]
    assert c["env_value"] == {"host": "prod-host"}      # today's behaviour
    assert c["resolved_value"] == {"host": "base"}       # post-flip behaviour


def test_extra_connection_keys_are_not_collisions():
    # env omits protocol; connection base adds it -> addition, not a change
    connections = {"switch": {"name": "switch", "host": "h", "protocol": "iso8583"}}
    environments = [("prod", {"switch": {"host": "h"}})]
    assert find_collisions(environments, connections) == []


def test_no_collisions_after_backfill_is_applied():
    """The load-bearing invariant: a clean backfill makes the flip safe."""
    conns = ConnectionStore(":memory:")
    conns.upsert("switch", ConnectionDraft(host="base", port=7000))
    environments = [
        ("prod", {"switch": {"host": "prod-host", "port": 7000}}),
        ("dev", {"switch": {"host": "dev-host", "port": 7000}}),
    ]

    # before: both envs disagree with the connection base
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    assert len(find_collisions(environments, connections)) == 2

    # apply the backfill, then re-check
    apply_backfill(plan_backfill(environments, connections), conns)
    connections2 = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    assert find_collisions(environments, connections2) == []  # safe to flip


# -- Phase 5: slim environments -----------------------------------------------

def test_slim_removes_only_collision_clean_connection_adapters():
    connections = {
        "switch": {
            "name": "switch", "host": "base", "port": 7000,
            "environment_overrides": {"prod": {"host": "prod-host"}},
        }
    }
    environments = [
        # prod: env value matches the connection's resolved config -> removable
        ("prod", {"switch": {"host": "prod-host", "port": 7000},
                  "terminal_sim": {"terminal_mode": "simulated"}}),
        # dev: connection has no dev override, env asserts a different host -> blocked
        ("dev", {"switch": {"host": "dev-host", "port": 7000}}),
    ]
    plan = plan_slim(environments, connections)
    assert plan.remove == {"prod": ["switch"]}
    assert plan.kept_standalone == 1  # terminal_sim never considered for removal
    assert {"environment": "dev", "adapter": "switch",
            "differing_keys": ["host"]} in plan.blocked


def test_apply_slim_drops_entry_and_preserves_other_env_fields():
    conns = ConnectionStore(":memory:")
    conns.upsert("switch", ConnectionDraft(
        host="prod-host", port=7000,
        environment_overrides={"prod": {}}))
    envs = EnvironmentStore(":memory:")
    envs.upsert("prod", EnvironmentDraft(
        name="prod",
        adapters={"switch": {"host": "prod-host", "port": 7000},
                  "terminal_sim": {"terminal_mode": "simulated"}},
        connection_budget={"total": 50}, mode="real"))

    environments = [(e.key, e.adapters or {}) for e in envs.list()]
    connections = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    changed = apply_slim(plan_slim(environments, connections), envs)

    assert changed == 1
    after = envs.get("prod")
    assert "switch" not in after.adapters            # connection now owns it
    assert "terminal_sim" in after.adapters          # standalone kept
    cfg = envs.get_config("prod")
    assert cfg["connection_budget"] == {"total": 50}  # untouched
    assert cfg["mode"] == "real"


# -- Phase B: seed default connections ----------------------------------------

CATALOG = {"tcp_iso8583", "http", "hsm", "db_probe_core"}


def test_seed_creates_one_default_per_type_with_matrix():
    environments = [
        ("dev", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                 "host": "dev.sw", "port": 7000}}),
        ("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                  "host": "prod.sw", "port": 7000}}),
    ]
    plan = plan_seed_defaults(environments, {}, CATALOG)
    spec = plan.create["tcp_iso8583"]
    assert spec["type"] == "tcp/iso8583"
    assert spec["base_env"] == "dev"               # first env, sorted
    assert spec["overrides"] == {"prod": {"host": "prod.sw"}}  # only the diff


def test_seed_ignores_instance_named_adapters():
    # switch_visa is not a catalog target — it's a named instance, not a default
    environments = [("prod", {"switch_visa": {"adapter": "tcp", "protocol": "iso8583",
                                              "host": "h", "port": 7001}})]
    plan = plan_seed_defaults(environments, {}, CATALOG)
    assert plan.is_empty


def test_seed_skips_type_with_existing_default():
    connections = {"my_switch": {"name": "my_switch", "adapter": "tcp",
                                 "protocol": "iso8583", "host": "h", "default": True}}
    environments = [("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                              "host": "h2", "port": 7000}})]
    plan = plan_seed_defaults(environments, connections, CATALOG)
    assert plan.is_empty
    assert {"target": "tcp_iso8583", "type": "tcp/iso8583"} in plan.skipped_existing


def test_apply_seed_creates_default_connection():
    conns = ConnectionStore(":memory:")
    environments = [
        ("dev", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                 "host": "dev.sw", "port": 7000}}),
        ("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                  "host": "prod.sw", "port": 7000}}),
    ]
    plan = plan_seed_defaults(environments, {}, CATALOG)
    created, failed = apply_seed_defaults(plan, conns)
    assert created == ["tcp_iso8583"] and failed == []
    d = conns.get_default("tcp", "iso8583")
    assert d is not None and d.name == "tcp_iso8583" and d.default is True
    assert d.host == "dev.sw"  # base
    assert d.environment_overrides == {"prod": {"host": "prod.sw"}}  # matrix
    # idempotent: re-planning now sees an existing default and skips
    conns2 = {c.name: c.model_dump(exclude_unset=True) for c in conns.list()}
    assert plan_seed_defaults(environments, conns2, CATALOG).is_empty


# -- Phase E: slim env adapters owned by a TYPE default -----------------------

def test_slim_removes_catalog_adapter_owned_by_a_differently_named_default():
    # default connection for tcp/iso8583 is named 'primary_switch' (not the target)
    connections = {
        "primary_switch": {
            "name": "primary_switch", "adapter": "tcp", "protocol": "iso8583",
            "host": "10.0.1.50", "port": 7000, "default": True,
        }
    }
    environments = [
        ("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                  "host": "10.0.1.50", "port": 7000}}),
    ]
    catalog = {"tcp_iso8583", "http", "hsm"}
    # without catalog_targets the catalog adapter is just standalone (kept)
    assert plan_slim(environments, connections).is_empty
    # with catalog_targets it's recognised as owned by the type default → removable
    plan = plan_slim(environments, connections, catalog)
    assert plan.remove == {"prod": ["tcp_iso8583"]}


def test_slim_blocks_catalog_adapter_when_default_differs():
    connections = {
        "primary_switch": {
            "name": "primary_switch", "adapter": "tcp", "protocol": "iso8583",
            "host": "DEFAULT.host", "port": 7000, "default": True,
        }
    }
    environments = [
        ("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                  "host": "ENV.host", "port": 7000}}),
    ]
    plan = plan_slim(environments, connections, {"tcp_iso8583"})
    assert plan.is_empty  # not removed
    assert plan.blocked[0]["adapter"] == "tcp_iso8583"
    assert plan.blocked[0]["owned_by_default"] == "primary_switch"


def test_slim_keeps_catalog_adapter_with_no_type_default():
    connections = {  # a connection of the type, but NOT marked default
        "primary_switch": {"name": "primary_switch", "adapter": "tcp",
                           "protocol": "iso8583", "host": "h", "port": 7000},
    }
    environments = [("prod", {"tcp_iso8583": {"adapter": "tcp", "protocol": "iso8583",
                                              "host": "h", "port": 7000}})]
    plan = plan_slim(environments, connections, {"tcp_iso8583"})
    assert plan.is_empty and plan.kept_standalone == 1


def test_full_cutover_backfill_then_flip_clean_then_slim():
    """End-to-end: backfill -> collisions clean -> slim leaves the env with only
    standalone adapters, and the connection still resolves the right host per env."""
    conns = ConnectionStore(":memory:")
    conns.upsert("switch", ConnectionDraft(host="base", port=7000))
    envs = EnvironmentStore(":memory:")
    envs.upsert("prod", EnvironmentDraft(
        name="prod",
        adapters={"switch": {"host": "prod-host", "port": 7000},
                  "terminal_sim": {"terminal_mode": "simulated"}}))

    def snapshot():
        e = [(x.key, x.adapters or {}) for x in envs.list()]
        c = {x.name: x.model_dump(exclude_unset=True) for x in conns.list()}
        return e, c

    # 1. backfill
    e, c = snapshot()
    apply_backfill(plan_backfill(e, c), conns)
    # 2. collisions clean -> safe to flip
    e, c = snapshot()
    assert find_collisions(e, c) == []
    # 3. slim
    apply_slim(plan_slim(e, c), envs)

    after = envs.get("prod")
    assert set(after.adapters) == {"terminal_sim"}            # only standalone left
    assert conns.get("switch").environment_overrides["prod"] == {"host": "prod-host"}
    # the matrix is now the single source of the prod host
    assert effective_under_env(
        conns.get("switch").model_dump(exclude_unset=True), "prod")["host"] == "prod-host"
