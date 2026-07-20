"""Test-case packs — curated, installable regression suites per scheme/processor.

A *pack* bundles a set of cases, each mapping a certification requirement to a
runnable scenario. Installing a pack imports its scenarios into a new project so
you can run them as a suite; a certification report (in the orchestrator) then
scores a run against the pack's cases.

The built-in packs here use the same mock-friendly targets as the bundled
example scenarios, so they pass end-to-end in mock mode out of the box — and
therefore make a meaningful (runnable) certification baseline.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PackCase(BaseModel):
    id: str                      # stable case id (== scenario name)
    requirement: str = ""        # human requirement this case proves
    scenario: dict               # a full scenario document (ScenarioDraft shape)


class Pack(BaseModel):
    id: str
    scheme: str = "generic"      # visa | mastercard | generic | <processor>
    label: str
    description: str = ""
    version: str = "1"
    cases: list[PackCase] = Field(default_factory=list)


def _assert(field: str, operator: str, expected=None) -> dict:
    a = {"field": field, "operator": operator}
    if expected is not None:
        a["expected"] = expected
    return a


def _step(sid: str, target: str, action: str, payload: dict | None = None,
          assertions: list | None = None) -> dict:
    return {"id": sid, "kind": "action", "target": target, "action": action,
            "payload": payload or {}, "assertions": assertions or []}


def _chain(*ids: str) -> list[dict]:
    return [{"source": a, "source_port": "out", "target": b}
            for a, b in zip(ids, ids[1:])]


# --------------------------------------------------------------------------- #
# Built-in packs
# --------------------------------------------------------------------------- #

_AUTH_SETTLE = {
    "name": "pack_auth_then_settled", "test_class": "e2e",
    "description": "Authorization is reflected in the downstream record.",
    "steps": [
        _step("auth", "http", "send_auth_request", {"amount": 10000},
              [_assert("response_code", "eq", "00")]),
        _step("settle", "db_probe_core", "query_transaction",
              {"rrn": "${auth.response.rrn}"},
              [_assert("status", "eq", "APPROVED"), _assert("amount", "eq", 10000)]),
    ],
    "edges": _chain("auth", "settle"),
}

_ECHO = {
    "name": "pack_network_echo", "test_class": "component",
    "description": "Host reachable — network echo succeeds.",
    "steps": [
        _step("echo", "http", "echo_test", {}, [_assert("status", "eq", "ok")]),
    ],
}


BUILTIN_PACKS: list[Pack] = [
    Pack(
        id="switch_settlement",
        scheme="generic",
        label="Switch & Settlement",
        description="Authorization reaches the switch and settles downstream; "
                    "host reachability.",
        cases=[
            PackCase(id="pack_auth_then_settled",
                     requirement="Authorization reflected in downstream settlement",
                     scenario=_AUTH_SETTLE),
            PackCase(id="pack_network_echo",
                     requirement="Network echo / host reachable",
                     scenario=_ECHO),
        ],
    ),
]


def list_packs() -> list[Pack]:
    return list(BUILTIN_PACKS)


def get_pack(pack_id: str) -> Pack | None:
    return next((p for p in BUILTIN_PACKS if p.id == pack_id), None)
