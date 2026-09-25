"""ADR-0012 phase 0: the adapters the service side offers must exist in the worker.

The database probe was referenced by the README, the catalog, the assistant, a
pack and the connection allowlist for months while the worker class it pointed
at did not exist; every test ran it through the mock. This test makes the next
phantom impossible: anything the connection allowlist or the step catalog
offers as an adapter-backed target must resolve to a worker adapter class, or be
listed explicitly (and temporarily) in ``PLANNED_ADAPTERS``.
"""

from __future__ import annotations

from api.connection_store import _ALLOWED_ADAPTERS
from models.catalog import STEP_CATALOG

from worker.adapters.registry import ADAPTER_MAP

#: Adapters the product surface may offer before the worker implements them.
#: Emptied by ADR-0012 phase 1; adding a name here is a conscious, reviewed act.
PLANNED_ADAPTERS: set[str] = set()  # ADR-0012 phase 1 built the probe; nothing is planned

#: Catalog targets whose actions run on an adapter (not a code / crypto step) and
#: how they name the worker implementation. Same mapping the worker registry
#: aliases use; ``db_probe_*`` map to themselves (orchestrator ``_target_type_key``).
_CATALOG_TARGET_TO_ADAPTER = {
    "http": "http",
    "insight": "insight",
    "hsm": "hsm",
    "tcp_iso8583": "tcp_iso8583",
    "nats": "nats",
}


def _adapter_backed_catalog_targets() -> set[str]:
    out: set[str] = set()
    for spec in STEP_CATALOG:
        kinds = {(a.behavior or {}).get("kind", "adapter") for a in spec.actions}
        if "adapter" in kinds:
            out.add(_CATALOG_TARGET_TO_ADAPTER.get(spec.target, spec.target))
    return out


#: adapter key -> worker package under ``worker/adapters`` whose ``adapter.py`` is
#: the implementation. Keys not listed map to a package of the same name.
_PACKAGE_FOR = {
    "db_probe_core": "db_probe",
    "db_probe_switch": "db_probe",
    "hsm_client": "hsm",
    "payshield": "hsm",
    "tcp_iso8583": "tcp",
}


def implemented_in_tree(adapter: str) -> bool:
    """The adapter's ``adapter.py`` exists in the worker tree. Distinguishes an
    optional dependency that is simply not installed here (grpc without
    protobuf: the registry skips the import, the module is real) from a
    phantom (the db_probe package that held only a README for months)."""
    import importlib.util

    pkg = _PACKAGE_FOR.get(adapter, adapter)
    try:
        return importlib.util.find_spec(f"worker.adapters.{pkg}.adapter") is not None
    except (ImportError, ValueError):
        return False


def test_every_offered_adapter_is_implemented_or_explicitly_planned():
    offered = set(_ALLOWED_ADAPTERS) | _adapter_backed_catalog_targets()
    missing = {a for a in offered if a not in ADAPTER_MAP}
    # unavailable here only because an optional dependency is absent: not a phantom
    optional_absent = {a for a in missing if implemented_in_tree(a)}
    phantom = missing - PLANNED_ADAPTERS - optional_absent
    assert not phantom, (
        "service surfaces offer adapters the worker cannot instantiate: "
        f"{sorted(phantom)}. Implement them or list them in PLANNED_ADAPTERS with a reason."
    )
    stale = PLANNED_ADAPTERS & set(ADAPTER_MAP)
    assert not stale, f"PLANNED_ADAPTERS lists adapters that now exist: {sorted(stale)}"


def test_optional_dependency_absence_is_not_a_phantom_but_a_missing_module_is():
    # grpc's adapter module is real even where protobuf is not installed
    assert implemented_in_tree("grpc")
    assert implemented_in_tree("db_probe_core")  # phase 1 built it
    # a package with no adapter.py (the pre-ADR-0012 db_probe shape) is a phantom
    assert not implemented_in_tree("no_such_adapter")
