"""Go/No-Go sign-off: immutable snapshots, gates, baseline, approval trail."""
import os
import types

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api.run_store import RunStore  # noqa: E402
from orchestrator.api.signoff_store import SignoffStore  # noqa: E402

PACK = {
    "id": "switch_settlement", "version": "1.0", "scheme": "generic",
    "label": "Switch & Settlement",
    "cases": [
        {"id": "c1", "requirement": "Auth settled", "priority": "P0",
         "scenario": {"name": "pack_auth_then_settled"}},
        {"id": "c2", "requirement": "Network echo", "priority": "P0",
         "scenario": {"name": "pack_network_echo"}},
    ],
}

POLICY = {"tiers": {"P0": 100}, "allowed_environments": ["staging"]}


def _req(sub="david"):
    return types.SimpleNamespace(state=types.SimpleNamespace(auth={"sub": sub}))


def _pass_summary():
    return {"scenarios": [
        {"name": "pack_auth_then_settled", "status": "passed",
         "steps": [{"step_id": "s1", "status": "passed", "assertions": []}]},
        {"name": "pack_network_echo", "status": "passed",
         "steps": [{"step_id": "s1", "status": "passed", "assertions": []}]},
    ]}


# -- store: immutability, approvals, baseline --------------------------------

def test_store_freezes_and_advances_baseline_on_go():
    store = SignoffStore(":memory:")
    doc = store.create({
        "run_id": "r1", "project": "p", "pack": "switch_settlement",
        "environment": "staging", "verdict": "GO", "gate_result": {},
        "provenance": {}, "content_hash": "sha256:x", "summary": {},
    })
    assert doc["frozen"] is True and doc["approvals"] == [] and doc["id"]
    # GO advanced the baseline for (project, pack, env)
    assert store.baseline_run(project="p", pack="switch_settlement",
                              environment="staging") == "r1"
    # a different env has no baseline
    assert store.baseline_run(project="p", pack="switch_settlement",
                              environment="prod") is None


def test_no_go_does_not_advance_baseline():
    store = SignoffStore(":memory:")
    store.create({
        "run_id": "r1", "project": "p", "pack": "k", "environment": "staging",
        "verdict": "NO_GO", "gate_result": {}, "provenance": {},
        "content_hash": "sha256:x", "summary": {},
    })
    assert store.baseline_run(project="p", pack="k", environment="staging") is None


def test_approval_trail_appends_without_touching_evidence():
    store = SignoffStore(":memory:")
    doc = store.create({"run_id": "r1", "verdict": "GO", "gate_result": {},
                        "provenance": {}, "content_hash": "sha256:abc", "summary": {}})
    before = doc["content_hash"]
    updated = store.add_approval(doc["id"], by="alice", note="ship it")
    assert len(updated["approvals"]) == 1
    assert updated["approvals"][0]["by"] == "alice"
    assert updated["content_hash"] == before  # evidence hash unchanged


def test_store_encrypts_secret_fields_at_rest(tmp_path):
    """With a SecretBox, secret-named fields are ciphertext on disk but plaintext
    when read back through the store."""
    pytest.importorskip("cryptography")
    from payprobe_common.crypto import SecretBox

    key = SecretBox.generate_key()
    path = str(tmp_path / "signoffs.json")
    store = SignoffStore(path, box=SecretBox(key))
    doc = store.create({
        "run_id": "r1", "verdict": "GO", "gate_result": {}, "provenance": {},
        "content_hash": "sha256:x",
        # a secret-named field nested in the frozen summary
        "summary": {"scenarios": [], "api_key": "super-secret-value"},
    })
    raw = (tmp_path / "signoffs.json").read_text()
    assert "super-secret-value" not in raw       # not stored in the clear
    assert "enc:v1:" in raw                       # tagged ciphertext present

    reopened = SignoffStore(path, box=SecretBox(key))
    got = reopened.get(doc["id"])
    assert got["summary"]["api_key"] == "super-secret-value"  # decrypts on read


def test_store_persists_to_disk(tmp_path):
    path = str(tmp_path / "signoffs.json")
    store = SignoffStore(path)
    doc = store.create({"run_id": "r1", "project": "p", "pack": "k",
                        "environment": "staging", "verdict": "GO",
                        "gate_result": {}, "provenance": {},
                        "content_hash": "sha256:x", "summary": {}})
    reopened = SignoffStore(path)
    assert reopened.get(doc["id"]) is not None
    assert reopened.baseline_run(project="p", pack="k", environment="staging") == "r1"


# -- endpoints ---------------------------------------------------------------

@pytest.fixture()
def app_module(monkeypatch):
    m.run_store = RunStore(":memory:")
    m.signoff_store = SignoffStore(":memory:")

    async def fake_get(url):
        assert url.endswith("/packs/switch_settlement")
        return PACK

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    return m


def _finish(mod, run_id, summary, env="staging"):
    mod.run_store.create(run_id, env, 2)
    mod.run_store.finish(run_id, "passed", summary)


async def test_certify_full_pass_is_go_and_stamped(app_module):
    _finish(app_module, "rX", _pass_summary())
    body = m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p")
    snap = await app_module.certify_run("rX", body, _req())

    assert snap["verdict"] == "GO" and snap["gate_result"]["blocking"] == []
    assert snap["content_hash"].startswith("sha256:")
    assert snap["provenance"]["pack"] == {"id": "switch_settlement", "version": "1.0"}
    assert snap["created_by"] == "david"
    assert snap["frozen"] is True


async def test_certify_regression_vs_baseline_is_no_go(app_module):
    # first run: GO -> becomes the baseline
    _finish(app_module, "good", _pass_summary())
    await app_module.certify_run(
        "good", m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p"),
        _req())

    # second run: one P0 step now fails -> regression + tier fail
    broken = _pass_summary()
    broken["scenarios"][0]["status"] = "failed"
    broken["scenarios"][0]["steps"][0]["status"] = "failed"
    _finish(app_module, "bad", broken)

    snap = await app_module.certify_run(
        "bad", m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p"),
        _req())
    assert snap["verdict"] == "NO_GO"
    assert "regressions" in snap["gate_result"]["blocking"]
    assert "tier:P0" in snap["gate_result"]["blocking"]
    assert snap["provenance"]["baseline_run_id"] == "good"


async def test_certify_blocks_simulator_environment(app_module):
    _finish(app_module, "rMock", _pass_summary(), env="mock")
    snap = await app_module.certify_run(
        "rMock", m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p"),
        _req())
    assert snap["verdict"] == "NO_GO"
    assert "environment" in snap["gate_result"]["blocking"]


def test_resolved_endpoints_extracts_targets():
    env = {"adapters": {
        "issuer": {"host": "10.0.1.50", "port": 7000},
        "acquirer": {"target": "host.example:6000"},
        "grp": {"kind": "group"},  # no single host -> skipped
    }}
    eps = {e["target"]: e for e in m._resolved_endpoints(env)}
    assert eps["issuer"]["host"] == "10.0.1.50" and eps["issuer"]["port"] == 7000
    assert eps["acquirer"]["port"] == 6000
    assert "grp" not in eps


def test_signoff_endpoints_prefers_summary():
    detail = {"summary": {"endpoints": [{"target": "issuer", "host": "h", "port": 1}]}}
    assert m._signoff_endpoints(detail)[0]["target"] == "issuer"
    # falls back to run-level for older runs
    assert m._signoff_endpoints({"endpoints": [{"target": "x"}]})[0]["target"] == "x"
    assert m._signoff_endpoints({}) == []


async def test_certify_stamps_resolved_endpoints(app_module):
    _finish(app_module, "rX", {
        **_pass_summary(),
        "endpoints": [{"target": "issuer", "host": "10.0.1.50", "port": 7000}],
    })
    snap = await app_module.certify_run(
        "rX", m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p"),
        _req())
    assert snap["provenance"]["endpoints"] == [
        {"target": "issuer", "host": "10.0.1.50", "port": 7000}]


async def test_certify_unknown_run_404(app_module):
    with pytest.raises(m.HTTPException) as exc:
        await app_module.certify_run(
            "nope", m.CertifyRequest(pack="switch_settlement"), _req())
    assert exc.value.status_code == 404


async def test_approve_endpoint_appends_to_trail(app_module):
    _finish(app_module, "rX", _pass_summary())
    snap = await app_module.certify_run(
        "rX", m.CertifyRequest(pack="switch_settlement", policy=POLICY, project="p"),
        _req())
    updated = await app_module.approve_signoff(
        snap["id"], m.ApprovalRequest(decision="approved", note="lgtm"), _req("alice"))
    assert updated["approvals"][-1] == {
        **updated["approvals"][-1],
        "by": "alice", "decision": "approved", "note": "lgtm",
    }
    listed = await app_module.list_signoffs(project="p")
    assert any(s["id"] == snap["id"] for s in listed["signoffs"])
