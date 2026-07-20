"""Built-in "Data Tables" palette group — read rows from global named tables.

Global tables (Manage → Tables) are injected into every run as
``context["tables"]`` (``{name: [row, ...]}``). This group provides an ergonomic
lookup step; you can also reach a whole table with ``${tables.NAME}`` or, in a
code node, ``context["tables"]["NAME"]``.
"""
from __future__ import annotations

from .catalog import ActionSpec, TargetSpec

_LOOKUP = r'''# Look up a row in a global named table (Manage -> Tables).
# Inputs come from the step's Input rows and may reference earlier steps.
table = inputs.get("table", "")
where_column = inputs.get("where_column", "")
where_value = inputs.get("where_value", "")
select_column = inputs.get("select_column", "")

rows = (context.get("tables") or {}).get(table) or []
match = next((r for r in rows if str(r.get(where_column)) == str(where_value)), None)
return {
    "found": match is not None,
    "row": match or {},
    "value": (match or {}).get(select_column) if select_column else None,
    "row_count": len(rows),
}
'''

_ALL = r'''# Return every row of a global named table.
table = inputs.get("table", "")
rows = (context.get("tables") or {}).get(table) or []
return {"rows": rows, "row_count": len(rows)}
'''


def _i(name, value):
    return {"name": name, "value": value}


def _code_action(name, label, response_fields, code, inputs) -> ActionSpec:
    return ActionSpec(
        name=name, label=label, payload_hint={}, response_fields=response_fields,
        behavior={"kind": "code", "template": {
            "language": "python", "code": code, "inputs": inputs,
        }},
    )


DATA_TABLES_TARGET = TargetSpec(
    target="data_tables",
    label="Data Tables",
    category="Data Tables",
    color="#3fb950",
    icon="table",
    description="Look up rows from named global data tables to drive steps "
                "with reference data (BINs, currencies, response codes…).",
    custom=False,
    actions=[
        _code_action(
            "table_lookup", "Table row lookup",
            ["found", "row", "value", "row_count"], _LOOKUP,
            [_i("table", "card_profiles"), _i("where_column", "profile"),
             _i("where_value", "visa_credit"), _i("select_column", "pan")],
        ),
        _code_action(
            "table_all", "Table — all rows", ["rows", "row_count"], _ALL,
            [_i("table", "card_profiles")],
        ),
    ],
)
