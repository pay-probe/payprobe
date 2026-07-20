"""AdapterRegistry: named instances, implementation selection, config merge.

Run from the packages/ directory:
    cd packages && python -m pytest worker/tests/test_adapter_registry.py -v
"""

import json
import pathlib

import pytest

from worker.adapters.registry import AdapterRegistry, _deep_merge
from worker.adapters.mock.adapter import MockAdapter
from worker.adapters.tcp.adapter import TcpAdapter

ENV = {
    "mode": "real",
    "adapter_defaults": {
        "tcp": {
            "framing": {"length_prefix_bytes": 2, "encoding": "ascii"},
            "response_timeout_sec": 20,
        }
    },
    "connection_budget": {"adapters": {"switch_visa": 200}},
    "adapters": {
        "switch_visa": {
            "adapter": "tcp",
            "protocol": "iso8583",
            "host": "10.0.1.50",
            "port": 7001,
            "correlation": {"match_mti": True},
        },
        "switch_mc": {"adapter": "tcp", "protocol": "iso8583", "host": "10.0.1.50", "port": 7002},
        "hsm_primary": {
            "adapter": "tcp",
            "protocol": "header_echo",
            "host": "10.0.2.10",
            "port": 1500,
            "header_echo": {"header_bytes": 4},
            "response_timeout_sec": 5,
        },
        "hsm_backup": {"extends": "hsm_primary", "host": "10.0.2.11"},
    },
}


def test_deep_merge_is_recursive_and_non_destructive():
    base = {"framing": {"a": 1, "b": 2}, "x": 1}
    out = _deep_merge(base, {"framing": {"b": 9, "c": 3}, "y": 2})
    assert out == {"framing": {"a": 1, "b": 9, "c": 3}, "x": 1, "y": 2}
    assert base == {"framing": {"a": 1, "b": 2}, "x": 1}  # untouched


def test_named_instances_resolve_to_impl_not_name():
    reg = AdapterRegistry(ENV)
    # instance names aren't in ADAPTER_MAP — the 'adapter' key selects the class
    assert reg._resolve_class("switch_visa") is TcpAdapter
    assert reg._resolve_class("switch_mc") is TcpAdapter
    assert reg._resolve_class("hsm_primary") is TcpAdapter


def test_adapter_defaults_underlay_instance_config():
    reg = AdapterRegistry(ENV)
    cfg = reg._config_for("switch_mc")
    # framing + response_timeout come from adapter_defaults["tcp"]
    assert cfg["framing"]["length_prefix_bytes"] == 2
    assert cfg["response_timeout_sec"] == 20
    # instance-specific values present
    assert cfg["host"] == "10.0.1.50" and cfg["port"] == 7002
    assert cfg["protocol"] == "iso8583"


def test_instance_config_overrides_defaults():
    reg = AdapterRegistry(ENV)
    cfg = reg._config_for("hsm_primary")
    # instance response_timeout (5) wins over the tcp default (20)
    assert cfg["response_timeout_sec"] == 5
    # nested merge keeps the default framing while adding header_echo
    assert cfg["framing"]["encoding"] == "ascii"
    assert cfg["header_echo"]["header_bytes"] == 4


def test_extends_inherits_then_overrides():
    reg = AdapterRegistry(ENV)
    cfg = reg._config_for("hsm_backup")
    assert cfg["host"] == "10.0.2.11"  # overridden
    assert cfg["port"] == 1500  # inherited from hsm_primary
    assert cfg["protocol"] == "header_echo"  # inherited
    assert cfg["header_echo"]["header_bytes"] == 4
    assert cfg["response_timeout_sec"] == 5  # inherited (instance, not default)
    assert reg._resolve_class("hsm_backup") is TcpAdapter


def test_mock_mode_overrides_real_impl():
    reg = AdapterRegistry({**ENV, "mode": "mock"})
    assert reg._resolve_class("switch_visa") is MockAdapter
    assert reg._resolve_class("hsm_backup") is MockAdapter


def test_per_adapter_mock_flag():
    reg = AdapterRegistry(
        {
            "mode": "real",
            "adapters": {"switch_x": {"adapter": "tcp", "mock": True}},
        }
    )
    assert reg._resolve_class("switch_x") is MockAdapter


def test_backwards_compatible_name_as_impl():
    # no 'adapter' key -> instance name is the implementation key
    reg = AdapterRegistry({"mode": "real", "adapters": {"switch": {"host": "h", "port": 1}}})
    assert reg._resolve_class("switch") is TcpAdapter


def test_unknown_impl_raises():
    reg = AdapterRegistry({"mode": "real", "adapters": {"weird": {"adapter": "nope"}}})
    with pytest.raises(ValueError, match="Unknown adapter 'nope'"):
        reg._resolve_class("weird")


def test_circular_extends_raises():
    reg = AdapterRegistry(
        {
            "mode": "real",
            "adapters": {"a": {"extends": "b"}, "b": {"extends": "a"}},
        }
    )
    with pytest.raises(ValueError, match="Circular 'extends'"):
        reg._config_for("a")


def test_pool_size_from_budget_then_config():
    reg = AdapterRegistry(ENV)
    # budget wins when present
    assert reg.budget.get("switch_visa") == 200
    # config-level pool_size used when no budget entry
    reg2 = AdapterRegistry(
        {
            "mode": "real",
            "adapters": {"s": {"adapter": "tcp", "pool_size": 42, "host": "h", "port": 1}},
        }
    )
    assert reg2._config_for("s")["pool_size"] == 42


def test_registry_first_import_still_registers_hsm_client():
    """Importing the registry as the process's FIRST worker import must still
    register the payShield client. (Regression: an eager import in
    ``worker.adapters.hsm.__init__`` created a cycle — registry → hsm package →
    payshield → commands → worker.engine → registry — whose ImportError the
    optional-dep guard swallowed, silently dropping ``payshield``/``hsm_client``
    from ADAPTER_MAP for the whole process.)"""
    import subprocess
    import sys

    code = (
        "import worker.adapters.registry as r; "
        "assert 'payshield' in r.ADAPTER_MAP, sorted(r.ADAPTER_MAP); "
        "assert 'hsm_client' in r.ADAPTER_MAP"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_example_connection_seed_is_valid():
    """The bundled connection examples (the modern shape of the old multi-instance
    named adapters) resolve in the registry, and `extends` still works."""
    path = pathlib.Path(__file__).resolve().parents[3] / "examples" / "connections" / "bundled.json"
    conns = json.loads(path.read_text())["connections"]
    # the TCP instances (formerly multi-instance/switch_* + hsm_*) resolve to TcpAdapter
    tcp = {n: c for n, c in conns.items() if c.get("adapter", "tcp") == "tcp"}
    reg = AdapterRegistry({"adapters": tcp})
    for name in tcp:
        assert reg._resolve_class(name) is TcpAdapter
    # hsm_backup inherits the primary's port (1500), overrides host
    assert reg._config_for("hsm_backup")["port"] == 1500
    assert reg._config_for("hsm_backup")["host"] == "10.0.2.11"
