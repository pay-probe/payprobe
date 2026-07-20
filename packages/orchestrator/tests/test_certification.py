"""Certification report: score a run against a test-case pack."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402

PACK = {
    "id": "switch_settlement", "scheme": "generic", "label": "Switch & Settlement",
    "cases": [
        {"id": "c1", "requirement": "Auth settled downstream", "scenario": {"name": "pack_auth_then_settled"}},
        {"id": "c2", "requirement": "Network echo", "scenario": {"name": "pack_network_echo"}},
    ],
}


def test_certify_full_pass_is_certified():
    detail = {"id": "r1", "summary": {"scenarios": [
        {"name": "pack_auth_then_settled", "status": "passed"},
        {"name": "pack_network_echo", "status": "passed"},
    ]}}
    cert = m._certify(detail, PACK)
    assert cert["certified"] is True
    assert cert["compliance_pct"] == 100.0 and cert["passed"] == 2


def test_certify_partial_and_not_run():
    detail = {"id": "r2", "summary": {"scenarios": [
        {"name": "pack_auth_then_settled", "status": "failed"},
        # network echo not in the run -> not_run
    ]}}
    cert = m._certify(detail, PACK)
    assert cert["certified"] is False
    assert cert["compliance_pct"] == 0.0
    by_id = {c["id"]: c["status"] for c in cert["cases"]}
    assert by_id == {"c1": "failed", "c2": "not_run"}


@pytest.fixture()
def app_module(monkeypatch):
    m.run_store = RunStore(":memory:")

    async def fake_get(url):
        assert url.endswith("/packs/switch_settlement")
        return PACK

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    return m


async def test_certification_endpoint(app_module):
    app_module.run_store.create("rX", "mock", 2)
    app_module.run_store.finish("rX", "passed", {"scenarios": [
        {"name": "pack_auth_then_settled", "status": "passed"},
        {"name": "pack_network_echo", "status": "passed"},
    ]})
    cert = await app_module.run_certification("rX", pack="switch_settlement")
    assert cert["certified"] is True and cert["total"] == 2

    html = await app_module.run_certification_html("rX", pack="switch_settlement")
    assert "CERTIFIED" in html.body.decode() and "Settlement" in html.body.decode()
