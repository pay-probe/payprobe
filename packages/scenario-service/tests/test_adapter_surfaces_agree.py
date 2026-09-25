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


def test_every_offered_adapter_is_implemented_or_explicitly_planned():
    offered = set(_ALLOWED_ADAPTERS) | _adapter_backed_catalog_targets()
    missing = {a for a in offered if a not in ADAPTER_MAP}
    phantom = missing - PLANNED_ADAPTERS
    assert not phantom, (
        "service surfaces offer adapters the worker cannot instantiate: "
        f"{sorted(phantom)}. Implement them or list them in PLANNED_ADAPTERS with a reason."
    )
    stale = PLANNED_ADAPTERS & set(ADAPTER_MAP)
    assert not stale, f"PLANNED_ADAPTERS lists adapters that now exist: {sorted(stale)}"
