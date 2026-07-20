"""Connection / Environment migration — Phase 0 + Phase 1.

See ``docs/history/CONNECTION-ENV-MIGRATION-PLAN.md`` for the full plan.

Goal: an Environment should be a *selector*, not an adapter owner. Per-environment
network values (host/port/creds) belong on each connection's
``environment_overrides`` matrix, not duplicated in ``environments[].adapters``.

This module implements the **additive, reversible half**:

* **Phase 0 — classify.** :func:`classify_adapters` splits an environment's
  ``adapters`` block into *connection-backed* (the key matches a registered
  connection) and *standalone* (hsm, db_probe, …) entries.
* **Phase 1 — backfill.** :func:`plan_backfill` computes a non-destructive plan
  that moves the *differing* per-env values out of each connection-backed env
  adapter into ``connection.environment_overrides[env]``. :func:`apply_backfill`
  writes that plan into the :class:`ConnectionStore`.

Nothing here deletes or rewrites ``environments[].adapters`` — that is Phase 5,
done only after precedence is flipped (Phase 4). Until then both sources coexist
and the environment still wins at runtime, so behaviour is unchanged. The plan is
idempotent: re-running after an apply produces an empty plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Connection-doc keys that are registry *metadata*, not worker adapter config.
#: Mirrors the orchestrator's ``_SKIP_CONN_KEYS`` so a connection's base is
#: compared against an env adapter block on equal terms (pure adapter config).
NON_ADAPTER_KEYS = frozenset(
    {
        "name",
        "mode",
        "listen_port",
        "environments",
        "environment_overrides",
        "description",
        "disabled",
        "default",
    }
)

#: Keys that mark an adapter block as a *network endpoint* (i.e. it looks like a
#: connection). Used only to flag standalone-but-connection-shaped entries so a
#: human can decide whether to register them as connections.
_NETWORK_HINT_KEYS = frozenset({"host", "port", "base_url", "endpoints", "target"})


def connection_base(conn_doc: dict[str, Any]) -> dict[str, Any]:
    """The worker-shaped adapter config of a connection (metadata stripped)."""
    return {k: v for k, v in conn_doc.items() if k not in NON_ADAPTER_KEYS}


def classify_adapters(
    env_adapters: dict[str, Any], connection_names: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Phase 0. Partition an env's ``adapters`` into (connection_backed, standalone).

    A key is *connection-backed* when it names a registered connection; everything
    else (crypto/HSM tools, db probes, …) is *standalone* and stays
    in the environment.
    """
    backed: dict[str, Any] = {}
    standalone: dict[str, Any] = {}
    for key, cfg in (env_adapters or {}).items():
        (backed if key in connection_names else standalone)[key] = cfg
    return backed, standalone


def override_diff(env_cfg: Any, base: dict[str, Any]) -> dict[str, Any]:
    """Keys in ``env_cfg`` whose value differs from (or is absent in) ``base``.

    The result is the *minimal* per-env override — only what actually changes —
    so the matrix stays diff-shaped, not a full copy of the connection.
    """
    if not isinstance(env_cfg, dict):
        return {}
    return {k: v for k, v in env_cfg.items() if base.get(k) != v}


def _looks_like_network(cfg: Any) -> bool:
    return isinstance(cfg, dict) and any(k in cfg for k in _NETWORK_HINT_KEYS)


@dataclass
class BackfillPlan:
    """A non-destructive plan to backfill connection override matrices.

    ``set_overrides`` maps ``connection -> env_key -> diff`` to write. ``unmatched``
    lists env adapters with no registered connection (so they're left untouched),
    tagged with whether they *look* like a network endpoint that probably wants a
    connection. ``already_current`` counts connection-backed env adapters that are
    already represented (nothing to do) — used to prove idempotency.
    """

    set_overrides: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    already_current: int = 0

    @property
    def is_empty(self) -> bool:
        return not any(self.set_overrides.values())

    def summary(self) -> dict[str, Any]:
        connections = sorted(self.set_overrides)
        writes = sum(len(envs) for envs in self.set_overrides.values())
        needs_connection = [u for u in self.unmatched if u.get("looks_network")]
        return {
            "is_empty": self.is_empty,
            "connections_touched": connections,
            "override_writes": writes,
            "already_current": self.already_current,
            "unmatched": self.unmatched,
            "needs_connection": [
                {"environment": u["environment"], "adapter": u["adapter"]}
                for u in needs_connection
            ],
            "set_overrides": self.set_overrides,
        }


def plan_backfill(
    environments: list[tuple[str, dict[str, Any]]],
    connections: dict[str, dict[str, Any]],
) -> BackfillPlan:
    """Phase 1. Compute the backfill without mutating anything.

    ``environments`` is a list of ``(env_key, adapters_dict)``. ``connections`` maps
    connection name -> full connection doc (including any existing
    ``environment_overrides``). Only env adapters that match a *registered*
    connection are backfilled; unregistered ones are reported, never auto-created
    (auto-creating from an env block is ambiguous — which env is the base? — so it
    is a deliberate human step, not part of this reversible phase).
    """
    conn_names = set(connections)
    bases = {n: connection_base(d) for n, d in connections.items()}
    existing_ov = {
        n: dict(d.get("environment_overrides") or {}) for n, d in connections.items()
    }

    plan = BackfillPlan()
    for env_key, adapters in environments:
        backed, standalone = classify_adapters(adapters, conn_names)
        for akey, cfg in standalone.items():
            plan.unmatched.append(
                {
                    "environment": env_key,
                    "adapter": akey,
                    "looks_network": _looks_like_network(cfg),
                }
            )
        for akey, cfg in backed.items():
            diff = override_diff(cfg, bases.get(akey, {}))
            if not diff:
                plan.already_current += 1
                continue
            # Idempotency: if the connection already records exactly this override
            # for this env, there is nothing to write.
            if existing_ov.get(akey, {}).get(env_key) == diff:
                plan.already_current += 1
                continue
            plan.set_overrides.setdefault(akey, {})[env_key] = diff
    return plan


def effective_under_env(conn_doc: dict[str, Any], env_key: str) -> dict[str, Any]:
    """The adapter config a connection resolves to under an environment —
    ``base ⊕ environment_overrides[env]`` — i.e. exactly what the worker will get
    once precedence is flipped (Phase 4). Mirrors the orchestrator's
    ``_attach_connections`` merge so the lint and runtime agree.
    """
    base = connection_base(conn_doc)
    overrides = (conn_doc.get("environment_overrides") or {}).get(env_key) or {}
    return {**base, **overrides} if isinstance(overrides, dict) else dict(base)


@dataclass
class Collision:
    """A connection-backed env adapter whose value would CHANGE when precedence
    flips: the environment asserts a value the connection does not yet resolve to.
    ``differing_keys`` lists the asserted env keys that disagree with the resolved
    connection config. Zero collisions across all environments = safe to flip.
    """

    environment: str
    adapter: str
    differing_keys: list[str]
    env_value: dict[str, Any]
    resolved_value: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "adapter": self.adapter,
            "differing_keys": self.differing_keys,
            "env_value": {k: self.env_value.get(k) for k in self.differing_keys},
            "resolved_value": {k: self.resolved_value.get(k) for k in self.differing_keys},
        }


def find_collisions(
    environments: list[tuple[str, dict[str, Any]]],
    connections: dict[str, dict[str, Any]],
) -> list[Collision]:
    """Phase 3. Flag every connection-backed env adapter whose asserted value
    differs from what the connection resolves to under that environment.

    Compares only the keys the *environment* asserts: extra keys the connection
    adds (e.g. a base ``protocol`` the env omitted) are additions, not collisions.
    After a clean Phase 1 backfill there are no collisions, so the flip is
    behaviour-preserving and safe.
    """
    conn_names = set(connections)
    out: list[Collision] = []
    for env_key, adapters in environments:
        backed, _ = classify_adapters(adapters, conn_names)
        for akey, env_cfg in backed.items():
            if not isinstance(env_cfg, dict):
                continue
            resolved = effective_under_env(connections[akey], env_key)
            differing = [k for k in env_cfg if resolved.get(k) != env_cfg.get(k)]
            if differing:
                out.append(Collision(env_key, akey, differing, env_cfg, resolved))
    return out


@dataclass
class SlimPlan:
    """Phase 5. Which connection-backed adapter entries can be removed from each
    environment because the connection now fully provides them (collision-clean).

    ``remove`` maps ``env_key -> [adapter keys to drop]``. ``blocked`` lists
    connection-backed entries that still differ from the connection (a backfill is
    incomplete — removing them would change behaviour). Standalone adapters are
    always kept.
    """

    remove: dict[str, list[str]] = field(default_factory=dict)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    kept_standalone: int = 0

    @property
    def is_empty(self) -> bool:
        return not any(self.remove.values())

    def summary(self) -> dict[str, Any]:
        return {
            "is_empty": self.is_empty,
            "remove": self.remove,
            "removals": sum(len(v) for v in self.remove.values()),
            "blocked": self.blocked,
            "kept_standalone": self.kept_standalone,
        }


def plan_slim(
    environments: list[tuple[str, dict[str, Any]]],
    connections: dict[str, dict[str, Any]],
    catalog_targets: set[str] | None = None,
) -> SlimPlan:
    """Plan removal of env adapter entries a connection now owns.

    Two ownership cases, both removable only when collision-clean under the
    environment (every key the env asserts already matches what the connection
    resolves to, so dropping the inline copy changes nothing):

    * **name-matched** — the env adapter's key is a registered connection name
      (connection-wins makes the inline copy dead); and
    * **type-default** (Phase E of the default-connection model) — the env
      adapter's key is a *catalog target* whose adapter type has a **default
      connection**, so unbound steps now route to that default instead of the
      inline adapter. Requires ``catalog_targets`` (else this case is skipped).

    Anything still differing is reported in ``blocked`` and left in place.
    """
    conn_names = set(connections)
    catalog_targets = catalog_targets or set()
    defaults_by_type = {
        _adapter_type_of(d): d for d in connections.values() if d.get("default")
    }
    plan = SlimPlan()
    for env_key, adapters in environments:
        backed, standalone = classify_adapters(adapters, conn_names)
        for akey, env_cfg in backed.items():
            if not isinstance(env_cfg, dict):
                continue
            resolved = effective_under_env(connections[akey], env_key)
            differing = [k for k in env_cfg if resolved.get(k) != env_cfg.get(k)]
            if differing:
                plan.blocked.append(
                    {"environment": env_key, "adapter": akey, "differing_keys": differing}
                )
            else:
                plan.remove.setdefault(env_key, []).append(akey)
        for akey, env_cfg in standalone.items():
            default_doc = (
                defaults_by_type.get(_adapter_type_of(env_cfg))
                if akey in catalog_targets and isinstance(env_cfg, dict)
                else None
            )
            if default_doc is None:
                plan.kept_standalone += 1
                continue
            resolved = effective_under_env(default_doc, env_key)
            differing = [k for k in env_cfg if resolved.get(k) != env_cfg.get(k)]
            if differing:
                plan.blocked.append(
                    {
                        "environment": env_key,
                        "adapter": akey,
                        "differing_keys": differing,
                        "owned_by_default": default_doc.get("name"),
                    }
                )
            else:
                plan.remove.setdefault(env_key, []).append(akey)
    return plan


def apply_slim(plan: SlimPlan, envs: "EnvironmentStore") -> int:  # noqa: F821
    """Remove the planned adapter entries from the environment store. Returns the
    number of environments changed. Only ``adapters`` keys are removed; every other
    environment field (connection_budget, mode, standalone adapters, …) is
    preserved.
    """
    from .environment_store import EnvironmentDraft

    changed = 0
    for env_key, adapter_keys in plan.remove.items():
        env = envs.get(env_key)
        if env is None:
            continue
        data = env.model_dump(exclude_unset=False)
        data.pop("key", None)
        adapters = dict(data.get("adapters") or {})
        removed = False
        for ak in adapter_keys:
            if adapters.pop(ak, _SENTINEL) is not _SENTINEL:
                removed = True
        if removed:
            data["adapters"] = adapters
            envs.upsert(env_key, EnvironmentDraft(**data))
            changed += 1
    return changed


_SENTINEL = object()


# -- Phase B: seed default connections ----------------------------------------

def _adapter_type_of(cfg: dict[str, Any]) -> str:
    from .connection_store import type_key

    return type_key(cfg.get("adapter") or "tcp", cfg.get("protocol") or "iso8583")


@dataclass
class SeedPlan:
    """Phase B (default-connection model). Default connections to create, one per
    adapter type, from the environments' catalog-target-named adapters. ``create``
    maps catalog target -> ``{type, base, base_env, overrides}``. Types that already
    have a default connection are left alone and reported in ``skipped_existing``.
    """

    create: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_existing: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.create

    def summary(self) -> dict[str, Any]:
        return {
            "is_empty": self.is_empty,
            "create": {
                t: {
                    "type": s["type"],
                    "base_env": s["base_env"],
                    "override_envs": sorted(s["overrides"]),
                }
                for t, s in self.create.items()
            },
            "skipped_existing_default": self.skipped_existing,
        }


def plan_seed_defaults(
    environments: list[tuple[str, dict[str, Any]]],
    connections: dict[str, dict[str, Any]],
    catalog_targets: set[str],
) -> SeedPlan:
    """Plan a default connection per adapter type from each environment's
    catalog-target-named adapters (e.g. ``tcp_iso8583``, ``http``, ``hsm``).

    Only env adapters whose key is a catalog target are candidates — instance-named
    adapters (``switch_visa``) are not defaults. The base config comes from the
    first environment (sorted) that defines the target; other environments'
    differences become the new connection's override matrix. A type that already
    has a default connection is skipped (never clobbers a user's choice).
    """
    existing_types = {
        _adapter_type_of(d) for d in connections.values() if d.get("default")
    }
    by_target: dict[str, dict[str, dict[str, Any]]] = {}
    for env_key, adapters in environments:
        for key, cfg in (adapters or {}).items():
            if key in catalog_targets and isinstance(cfg, dict):
                by_target.setdefault(key, {})[env_key] = cfg

    plan = SeedPlan()
    for target in sorted(by_target):
        per_env = by_target[target]
        envs_sorted = sorted(per_env)
        base_env = envs_sorted[0]
        base = dict(per_env[base_env])
        tk = _adapter_type_of(base)
        if tk in existing_types:
            plan.skipped_existing.append({"target": target, "type": tk})
            continue
        overrides: dict[str, dict[str, Any]] = {}
        for e in envs_sorted[1:]:
            diff = override_diff(per_env[e], base)
            if diff:
                overrides[e] = diff
        plan.create[target] = {
            "type": tk, "base": base, "base_env": base_env, "overrides": overrides,
        }
        existing_types.add(tk)  # don't create two defaults of the same type
    return plan


def apply_seed_defaults(
    plan: SeedPlan, conns: "ConnectionStore"  # noqa: F821
) -> tuple[list[str], list[dict[str, Any]]]:
    """Create the planned default connections. Returns (created, failed). A target
    whose config can't be a connection (e.g. a mock-only ``terminal_sim`` adapter
    not in the connection allowlist) is skipped and reported, never raised.
    """
    from pydantic import ValidationError

    from .connection_store import ConnectionDraft

    created: list[str] = []
    failed: list[dict[str, Any]] = []
    for target, spec in plan.create.items():
        data = dict(spec["base"])
        data["default"] = True
        data["environment_overrides"] = spec["overrides"]
        try:
            conns.upsert(target, ConnectionDraft(**data))
            created.append(target)
        except ValidationError as exc:
            failed.append({"target": target, "error": str(exc).splitlines()[0]})
    return created, failed


def apply_backfill(plan: BackfillPlan, conns: "ConnectionStore") -> int:  # noqa: F821
    """Write a plan's overrides into the connection store. Returns connections
    written. Additive: merges each env diff onto any existing override for that env
    (diff wins), never touches other connection fields or the environment store.
    """
    # imported lazily to keep this module import-light / framework-free
    from .connection_store import ConnectionDraft

    written = 0
    for conn_name, env_diffs in plan.set_overrides.items():
        conn = conns.get(conn_name)
        if conn is None:  # connection vanished between plan and apply — skip
            continue
        data = conn.model_dump(exclude_unset=True)
        data.pop("name", None)
        overrides = dict(data.get("environment_overrides") or {})
        for env_key, diff in env_diffs.items():
            overrides[env_key] = {**(overrides.get(env_key) or {}), **diff}
        data["environment_overrides"] = overrides
        conns.upsert(conn_name, ConnectionDraft(**data))
        written += 1
    return written
