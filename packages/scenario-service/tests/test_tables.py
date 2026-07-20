"""Tests for global named data tables + the lookup step."""

import asyncio
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _table():
    return {
        "description": "card profiles",
        "columns": ["profile", "pan", "expiry"],
        "rows": [
            {"profile": "visa_credit", "pan": "4111111111111111", "expiry": "2412"},
            {"profile": "mc_debit", "pan": "5500000000000004", "expiry": "2511"},
        ],
    }


def test_table_crud_and_runtime(client):
    r = client.put("/tables/card_profiles", json=_table())
    assert r.status_code == 200
    assert r.json()["name"] == "card_profiles"

    names = {t["name"] for t in client.get("/tables").json()}
    assert "card_profiles" in names

    runtime = client.get("/tables/runtime").json()
    assert "card_profiles" in runtime
    assert len(runtime["card_profiles"]) == 2

    assert client.delete("/tables/card_profiles").status_code == 204
    assert client.delete("/tables/card_profiles").status_code == 404


def test_table_name_is_slugged(client):
    r = client.put("/tables/Card Profiles!", json=_table())
    assert r.json()["name"] == "card_profiles"


def test_data_tables_group_in_catalog(client):
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    assert "data_tables" in targets
    names = {a["name"] for a in targets["data_tables"]["actions"]}
    assert {"table_lookup", "table_all"} <= names


def test_lookup_step_reads_injected_table():
    """The lookup snippet finds a row from context['tables']."""
    import sys
    sys.path.insert(0, ".")
    from worker.engine.code_runner import run_code
    from worker.engine.variables import resolve_inputs
    from models.table_catalog import DATA_TABLES_TARGET

    tables = {"card_profiles": [
        {"profile": "visa_credit", "pan": "4111111111111111"},
        {"profile": "mc_debit", "pan": "5500000000000004"},
    ]}
    ctx = {"tables": tables}
    action = next(a for a in DATA_TABLES_TARGET.actions if a.name == "table_lookup")
    tpl = action.behavior["template"]
    res = asyncio.run(
        run_code("python", tpl["code"], ctx, inputs=resolve_inputs(tpl["inputs"], ctx))
    )
    assert res.ok
    assert res.output["found"] is True
    assert res.output["value"] == "4111111111111111"
