"""ADR-0012 phase 3: run-level fixtures on POST /runs, flag-gated.

With ``PAYPROBE_RUN_FIXTURES`` off a request naming fixtures is refused (never
silently run without them); with it on, fixture scenarios are fetched by id,
prepared like the run's own scenarios, carried on the run record and handed to
the engine.
"""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from orchestrator.api import main as m  # noqa: E402


def _req(**over):
    return m.CreateRunRequest(environment_name="mock", scenario_ids=["sc-1"], **over)


async def test_fixtures_refused_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(m, "_RUN_FIXTURES", False)
    with pytest.raises(HTTPException) as exc:
        await m._resolve_run_fixtures(_req(fixtures={"before": ["seed"]}), {"adapters": {}})
    assert exc.value.status_code == 400 and "PAYPROBE_RUN_FIXTURES" in exc.value.detail


async def test_no_fixtures_is_none_regardless_of_the_flag(monkeypatch):
    monkeypatch.setattr(m, "_RUN_FIXTURES", False)
    assert await m._resolve_run_fixtures(_req(), {"adapters": {}}) is None
    monkeypatch.setattr(m, "_RUN_FIXTURES", True)
    assert await m._resolve_run_fixtures(_req(fixtures={"before": []}), {"adapters": {}}) is None


async def test_fixtures_are_fetched_prepared_and_grouped(monkeypatch):
    monkeypatch.setattr(m, "_RUN_FIXTURES", True)
    docs = {
        "seed": {"id": "seed", "name": "seed", "steps": []},
        "verify": {"id": "verify", "name": "verify", "steps": []},
        "purge": {"id": "purge", "name": "purge", "steps": []},
    }
    attached: list[str] = []

    async def fake_fetch(ids):
        return [dict(docs[i]) for i in ids if i in docs]

    async def noop_attach(*args, **kwargs):
        attached.append("x")

    monkeypatch.setattr(m, "_fetch_scenarios_by_ids", fake_fetch)
    for name in ("_attach_subflows", "_attach_tables", "_attach_test_data", "_attach_connections", "_attach_groups"):
        monkeypatch.setattr(m, name, noop_attach)

    fx = await m._resolve_run_fixtures(
        _req(fixtures={"before": ["seed"], "after": ["verify", "purge"]}), {"adapters": {}})
    assert [s["id"] for s in fx["before"]] == ["seed"]
    assert [s["id"] for s in fx["after"]] == ["verify", "purge"]  # order preserved
    assert len(attached) == 10  # five attachment steps for each of the two groups

    with pytest.raises(HTTPException) as exc:
        await m._resolve_run_fixtures(_req(fixtures={"before": ["ghost"]}), {"adapters": {}})
    assert exc.value.status_code == 404 and "ghost" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        await m._resolve_run_fixtures(_req(fixtures={"during": ["seed"]}), {"adapters": {}})
    assert exc.value.status_code == 400 and "before" in exc.value.detail


def test_run_fixtures_flag_defaults_on():
    """Phase 4 (2026-09-25): on by default; only an explicit 0/false/no opts out."""
    import os

    if os.environ.get("PAYPROBE_RUN_FIXTURES") is None:
        assert m._RUN_FIXTURES is True
    else:
        pytest.skip("PAYPROBE_RUN_FIXTURES is set in this environment")


def test_run_record_carries_fixtures_for_the_engine():
    rec = m.RunRecord("r1", {"adapters": {}}, [], {"before": [{"id": "seed"}]})
    assert rec.fixtures == {"before": [{"id": "seed"}]}
    assert m.RunRecord("r2", {}, []).fixtures is None
