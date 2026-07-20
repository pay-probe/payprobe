"""Run registry: persistence + detailed report endpoints."""
import pytest

from worker.engine import InMemoryStreamBackbone
from orchestrator.api.run_store import RunStore

MOCK_ENV = {
    "name": "mock",
    "mode": "mock",
    "adapters": {
        "terminal_sim": {"mock": True, "latency_ms": 0},
        "http": {"mock": True, "latency_ms": 0},
    },
}

SCENARIO = {
    "id": "sc-1", "name": "tap", "test_class": "e2e",
    "steps": [
        {"id": "s1", "target": "terminal_sim", "action": "tap_nfc",
         "payload": {}, "assertions": [
             {"field": "status", "operator": "eq", "expected": "tapped"}]},
        {"id": "s2", "target": "http", "action": "send_auth_request",
         "payload": {"amount": 2000}, "assertions": [
             {"field": "response_code", "operator": "eq", "expected": "00"}]},
    ],
}


# -- RunStore unit tests ------------------------------------------------------

def test_run_store_create_finish_list_get():
    store = RunStore(":memory:")
    store.create("r1", "mock", 2)

    # appears in the registry immediately as pending
    listed = store.list()
    assert listed[0]["id"] == "r1"
    assert listed[0]["status"] == "pending"
    assert listed[0]["scenario_count"] == 2

    summary = {
        "status": "passed",
        "phases": {"phase_3": {"status": "passed"}},
        "scenarios": [
            {"scenario_id": "sc-1", "name": "tap", "status": "passed",
             "steps": [{"step_id": "s1", "status": "passed"}]},
            {"scenario_id": "sc-2", "name": "x", "status": "failed", "steps": []},
        ],
    }
    store.finish("r1", "failed", summary)

    row = store.list()[0]
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert row["scenarios"] == {"total": 2, "passed": 1, "failed": 1, "blocked": 0}

    detail = store.get("r1")
    assert detail["summary"]["scenarios"][0]["steps"][0]["step_id"] == "s1"
    assert store.get("missing") is None


def test_run_store_persists_project_id():
    store = RunStore(":memory:")
    store.create("r1", "mock", 1, label="project run", project_id="prj-abc")
    store.create("r2", "mock", 1, label="ad-hoc")  # no project

    by_id = {r["id"]: r for r in store.list()}
    assert by_id["r1"]["project_id"] == "prj-abc"
    # ad-hoc runs carry a null project (empty string folds to NULL on write)
    assert by_id["r2"]["project_id"] is None

    assert store.get("r1")["project_id"] == "prj-abc"
    assert store.get("r2")["project_id"] is None


def test_junit_export_shape():
    from orchestrator.api.main import _junit_xml

    detail = {
        "id": "r1", "label": "nightly",
        "summary": {"scenarios": [
            {"name": "auth", "steps": [
                {"step_id": "s1", "status": "passed", "duration_ms": 12},
                {"step_id": "s2", "status": "failed", "duration_ms": 5,
                 "error": "response_code ne 00"},
            ]},
        ]},
    }
    xml = _junit_xml(detail)
    assert xml.startswith("<?xml")
    # report_service's generator also emits skipped= / time= attributes
    assert '<testsuite name="auth" tests="2" failures="1" errors="0"' in xml
    assert '<testcase classname="auth" name="s2"' in xml
    assert "<failure message=" in xml


def test_html_report_shape():
    from orchestrator.api.main import _html_report

    detail = {
        "id": "r1", "label": "nightly", "status": "failed", "environment": "mock",
        "summary": {"scenarios": [
            {"name": "auth", "status": "failed", "steps": [
                {"step_id": "s1", "target": "http", "action": "auth",
                 "status": "failed", "duration_ms": 7, "error": "boom",
                 "assertions": [{"field": "rc", "operator": "eq",
                                 "expected": "00", "actual": "05", "passed": False}]},
            ]},
        ]},
    }
    html = _html_report(detail)
    assert html.startswith("<!doctype html>")
    assert "PayProbe Run Report" in html and "auth" in html
    assert 'class="b failed"' in html
    assert "boom" in html and "✗" in html


async def test_compare_detects_regressions_and_fixes(app_module):
    base_summary = {"status": "failed", "scenarios": [
        {"name": "auth", "status": "failed", "steps": [
            {"step_id": "s1", "status": "passed"},
            {"step_id": "s2", "status": "failed"},
        ]},
    ]}
    cur_summary = {"status": "failed", "scenarios": [
        {"name": "auth", "status": "failed", "steps": [
            {"step_id": "s1", "status": "failed"},   # regressed
            {"step_id": "s2", "status": "passed"},   # fixed
        ]},
    ]}
    app_module.run_store.create("base", "mock", 1, label="nightly")
    app_module.run_store.finish("base", "failed", base_summary)
    app_module.run_store.create("cur", "mock", 1, label="nightly")
    app_module.run_store.finish("cur", "failed", cur_summary)

    res = await app_module.compare_run("cur")
    assert res["base_run_id"] == "base"
    assert res["summary"]["regressed"] == 1
    assert res["summary"]["fixed"] == 1
    kinds = {(c["step"], c["kind"]) for c in res["changes"]}
    assert ("s1", "regressed") in kinds
    assert ("s2", "fixed") in kinds


def test_run_store_persists_to_file(tmp_path):
    db = str(tmp_path / "runs.db")
    s1 = RunStore(db)
    s1.create("rA", "mock", 1)
    s1.finish("rA", "passed", {"status": "passed", "scenarios": []})
    s1.close()

    # reopen: the run is still there (survives "restart")
    s2 = RunStore(db)
    assert s2.get("rA")["status"] == "passed"


# -- orchestrator integration -------------------------------------------------

@pytest.fixture()
def app_module():
    from orchestrator.api import main as m
    m.backbone = InMemoryStreamBackbone()
    m.run_store = RunStore(":memory:")
    m.RUNS.clear()
    return m


async def test_run_by_scenario_ids_fetches_and_labels(app_module, monkeypatch):
    async def fake_fetch(ids):
        assert ids == ["abc"]
        return [SCENARIO]

    monkeypatch.setattr(app_module, "_fetch_scenarios_by_ids", fake_fetch)
    resp = await app_module.create_run(
        app_module.CreateRunRequest(
            scenario_ids=["abc"], label="My case", environment=MOCK_ENV
        )
    )
    await app_module.RUNS[resp.run_id].task

    detail = await app_module.get_run(resp.run_id)
    assert detail["label"] == "My case"
    assert detail["summary"]["scenarios"][0]["name"] == "tap"


async def test_run_by_project_resolves_ids_then_fetches(app_module, monkeypatch):
    async def fake_ids(project_id=None, set_id=None):
        assert project_id == "prj-1"
        return ["id-1"]

    async def fake_fetch(ids):
        assert ids == ["id-1"]
        return [SCENARIO]

    monkeypatch.setattr(app_module, "_fetch_scenario_ids", fake_ids)
    monkeypatch.setattr(app_module, "_fetch_scenarios_by_ids", fake_fetch)
    resp = await app_module.create_run(
        app_module.CreateRunRequest(project_id="prj-1", environment=MOCK_ENV)
    )
    await app_module.RUNS[resp.run_id].task

    entry = next(r for r in await app_module.list_runs() if r["id"] == resp.run_id)
    assert entry["label"] == "project run"
    assert entry["scenarios"]["total"] == 1


def test_expand_dataset_merges_rows_into_variables(app_module):
    sc = {"id": "sc-1", "name": "tap", "variables": {"currency": "USD"}, "steps": []}
    rows = [{"amount": "100"}, {"amount": "200"}, {"amount": "300"}]
    out = app_module._expand_dataset([sc], rows)
    assert len(out) == 3
    # row columns merge over the base variables; base vars preserved
    assert out[0]["variables"] == {"currency": "USD", "amount": "100"}
    assert out[2]["variables"]["amount"] == "300"
    # distinct names + ids per instance
    assert [s["name"] for s in out] == ["tap · row 1", "tap · row 2", "tap · row 3"]
    assert len({s["id"] for s in out}) == 3


async def test_attach_subflows_fetches_referenced_scenarios(app_module, monkeypatch):
    sub = {"id": "sub-9", "name": "setup", "steps": [], "edges": []}

    async def fake_get(url):
        assert url.endswith("/scenarios/sub-9")
        return sub

    monkeypatch.setattr(app_module, "_http_get_json", fake_get)
    parent = {
        "id": "p", "name": "p",
        "steps": [{"id": "c", "kind": "call", "config": {"scenario_id": "sub-9"}}],
    }
    await app_module._attach_subflows([parent])
    assert parent["subflows"]["sub-9"]["name"] == "setup"


async def test_run_with_inline_dataset_fans_out(app_module):
    resp = await app_module.create_run(
        app_module.CreateRunRequest(
            environment=MOCK_ENV, scenarios=[SCENARIO],
            dataset=[{"amount": "10"}, {"amount": "20"}],
        )
    )
    await app_module.RUNS[resp.run_id].task
    detail = await app_module.get_run(resp.run_id)
    assert len(detail["summary"]["scenarios"]) == 2
    assert detail["label"].endswith("× 2 rows")
    assert detail["summary"]["scenarios"][0]["name"].endswith("row 1")


async def test_create_run_registers_and_reports_detail(app_module):
    resp = await app_module.create_run(
        app_module.CreateRunRequest(environment=MOCK_ENV, scenarios=[SCENARIO])
    )
    run_id = resp.run_id
    await app_module.RUNS[run_id].task  # wait for completion

    # registry lists it with rolled-up counts
    listing = await app_module.list_runs()
    assert any(r["id"] == run_id for r in listing)
    entry = next(r for r in listing if r["id"] == run_id)
    assert entry["status"] == "passed"
    assert entry["environment"] == "mock"
    assert entry["scenarios"]["total"] == 1 and entry["scenarios"]["passed"] == 1

    # detail carries each case's per-step results for the report view
    detail = await app_module.get_run(run_id)
    steps = detail["summary"]["scenarios"][0]["steps"]
    assert [s["step_id"] for s in steps] == ["s1", "s2"]
    s2 = steps[1]
    assert s2["target"] == "http" and s2["status"] == "passed"
    assert s2["assertions"][0]["field"] == "response_code"
    assert s2["assertions"][0]["passed"] is True
    assert "response" in s2 and "request" in s2
