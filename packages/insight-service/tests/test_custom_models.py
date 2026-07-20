"""Custom datasets + model training + artifact persistence (ADR-0005 ext)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insight_service import main as main_mod
from insight_service.categorize import HAVE_SKLEARN
from insight_service.ingest import Ingestor
from insight_service.rest import OrchestratorReader
from insight_service.store import InsightStore

from insight_fixtures import FakeOrchestrator, build_history

needs_sklearn = pytest.mark.skipif(not HAVE_SKLEARN,
                                   reason="scikit-learn not installed (optional)")


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("INSIGHT_DATA_DIR", str(tmp_path / "models"))
    fake = FakeOrchestrator(build_history())
    store = InsightStore(":memory:")
    reader = OrchestratorReader(base_url="http://fake", fetch=fake.fetch)
    main_mod.app.state.store = store
    main_mod.app.state.reader = reader
    main_mod.app.state.ingestor = Ingestor(store, reader)
    return TestClient(main_mod.app)


def _training_rows() -> list[dict]:
    rows = []
    for i in range(15):
        rows.append({"text": f"response_code 91 issuer inoperative try {i}",
                     "label": "issuer-down"})
        rows.append({"text": f"response_code 51 insufficient funds acct {i}",
                     "label": "nsf-decline"})
        rows.append({"text": f"payShield LMK check failed slot {i}",
                     "label": "hsm-key-problem"})
    return rows


# -- datasets -------------------------------------------------------------------

def test_dataset_upload_json_and_csv(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "declines",
                                   "rows": _training_rows()}).json()
    assert ds["row_count"] == 45 and set(ds["labels"]) == {
        "issuer-down", "nsf-decline", "hsm-key-problem"}
    csv_doc = "text,label\nresponse_code 05 do not honor,decline\ntimed out,slow\n"
    ds2 = c.post("/datasets", json={"name": "csv-set", "csv": csv_doc}).json()
    assert ds2["row_count"] == 2
    listed = c.get("/datasets").json()["datasets"]
    assert len(listed) == 2
    # the list carries each dataset's label breakdown (Model Studio cards +
    # wizard review count labels from the list, not the detail read)
    by_id = {d["id"]: d for d in listed}
    assert set(by_id[ds["id"]]["labels"]) == {
        "issuer-down", "nsf-decline", "hsm-key-problem"}
    assert by_id[ds2["id"]]["labels"] == {"decline": 1, "slow": 1}
    assert c.delete(f"/datasets/{ds2['id']}").json()["deleted"] == ds2["id"]


def test_dataset_rows_viewer(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "declines",
                                   "rows": _training_rows()}).json()
    # plain page
    r = c.get(f"/datasets/{ds['id']}/rows?limit=10").json()
    assert r["total"] == 45 and len(r["rows"]) == 10
    assert set(r["rows"][0]) == {"id", "text", "label"}
    # label filter narrows the total
    r = c.get(f"/datasets/{ds['id']}/rows?label=issuer-down").json()
    assert r["total"] == ds["labels"]["issuer-down"]
    assert all(x["label"] == "issuer-down" for x in r["rows"])
    # substring search + pagination window
    r = c.get(f"/datasets/{ds['id']}/rows?q=issuer&limit=5&offset=5").json()
    assert len(r["rows"]) <= 5 and r["offset"] == 5
    assert c.get("/datasets/nope/rows").status_code == 404


def test_dataset_row_editing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "edit-me", "rows": [
        {"text": "issuer inoperative", "label": "issuer-down"},
        {"text": "insufficient funds", "label": "nsf-declne"},  # typo'd label
        {"text": "not sufficient funds", "label": "nsf-declne"},
    ]}).json()
    rows = c.get(f"/datasets/{ds['id']}/rows").json()["rows"]
    assert all("id" in r for r in rows)

    # relabel one row in place
    fix = c.patch(f"/datasets/{ds['id']}/rows/{rows[0]['id']}",
                  json={"label": "issuer-unavailable"}).json()
    assert fix["label"] == "issuer-unavailable"
    assert fix["dataset"]["labels"]["issuer-unavailable"] == 1

    # bulk-rename the typo'd label across the dataset
    ren = c.post(f"/datasets/{ds['id']}/rename-label",
                 json={"from_label": "nsf-declne",
                       "to_label": "nsf-decline"}).json()
    assert ren["renamed"] == 2
    assert ren["dataset"]["labels"] == {"issuer-unavailable": 1,
                                        "nsf-decline": 2}

    # delete a row; row_count follows
    gone = c.delete(f"/datasets/{ds['id']}/rows/{rows[1]['id']}").json()
    assert gone["dataset"]["row_count"] == 2
    assert c.delete(f"/datasets/{ds['id']}/rows/999999").status_code == 404
    assert c.patch(f"/datasets/{ds['id']}/rows/999999",
                   json={"label": "x"}).status_code == 404
    assert c.patch(f"/datasets/{ds['id']}/rows/{rows[2]['id']}",
                   json={"label": "  "}).status_code == 422


def test_dataset_rows_are_normalized_pan_safe(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "x", "rows": [
        {"text": "declined card 4111111111111111", "label": "decline"}]}).json()
    store: InsightStore = main_mod.app.state.store
    rows = store.dataset_rows([ds["id"]])
    assert "4111" not in rows[0]["text"]


def test_dataset_rejects_empty(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.post("/datasets", json={"name": "e", "rows": []}).status_code == 422
    assert c.post("/datasets", json={"name": "e", "csv": "\n\n"}).status_code == 422


# -- training + activation -------------------------------------------------------

@needs_sklearn
def test_train_activate_and_categorize_with_custom_labels(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "d", "rows": _training_rows()}).json()
    r = c.post("/models/train", json={"name": "my-taxonomy",
                                      "dataset_ids": [ds["id"]],
                                      "activate": True}).json()
    assert r["active"] is True
    assert r["metrics"]["holdout_accuracy"] is not None
    assert set(r["metrics"]["labels"]) == {"issuer-down", "nsf-decline",
                                           "hsm-key-problem"}
    # status reports the active custom model
    st = c.get("/status").json()
    assert st["categorizer"]["custom"]["name"] == "my-taxonomy"
    # ingested failures now carry the user's labels where the model matches
    ing = main_mod.app.state.ingestor
    from insight_service.categorize import categorize
    cat = categorize("response_code 91 issuer inoperative try 99",
                     ing.learned, ing.custom)
    assert cat["category"] == "issuer-down" and cat.get("custom") is True
    assert cat["heuristic"]  # heuristic floor still reported


@needs_sklearn
def test_train_validates_dataset_size(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "tiny", "rows": [
        {"text": "a b c", "label": "x"}, {"text": "d e f", "label": "y"}]}).json()
    r = c.post("/models/train", json={"name": "m", "dataset_ids": [ds["id"]]})
    assert r.status_code == 422 and "at least" in r.json()["detail"]


@needs_sklearn
def test_activation_is_exclusive_and_deactivatable(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "d", "rows": _training_rows()}).json()
    m1 = c.post("/models/train", json={"name": "m1", "dataset_ids": [ds["id"]],
                                       "activate": True}).json()
    m2 = c.post("/models/train", json={"name": "m2", "dataset_ids": [ds["id"]],
                                       "activate": True}).json()
    models = {m["id"]: m for m in c.get("/models").json()["models"]}
    assert models[m2["id"]]["active"] and not models[m1["id"]]["active"]
    assert c.post("/models/none/activate").json()["active"] is None
    assert main_mod.app.state.ingestor.custom is None


@needs_sklearn
def test_artifact_persistence_survives_restart(monkeypatch, tmp_path):
    """The whole point: a new process (fresh state) reloads the active model
    and the auto-trained cluster model from disk."""
    db = str(tmp_path / "insight.db")
    monkeypatch.setenv("INSIGHT_DB", db)
    monkeypatch.setenv("INSIGHT_DATA_DIR", str(tmp_path / "models"))

    fake = FakeOrchestrator(build_history())

    def fresh_app_state():
        main_mod.app.state.store = InsightStore(db)
        main_mod.app.state.reader = OrchestratorReader(
            base_url="http://fake", fetch=fake.fetch)
        main_mod.app.state.ingestor = Ingestor(
            main_mod.app.state.store, main_mod.app.state.reader)
        main_mod._load_artifacts(main_mod.app)
        return TestClient(main_mod.app)

    c = fresh_app_state()
    ds = c.post("/datasets", json={"name": "d", "rows": _training_rows()}).json()
    c.post("/models/train", json={"name": "keeper", "dataset_ids": [ds["id"]],
                                  "activate": True})
    # "restart": rebuild everything from the file-backed store + artifacts
    c2 = fresh_app_state()
    st = c2.get("/status").json()
    assert st["categorizer"]["custom"]["name"] == "keeper"
    ing = main_mod.app.state.ingestor
    assert ing.custom is not None
    hit = ing.custom.predict("payShield LMK check failed slot 9")
    assert hit["category"] == "hsm-key-problem"


@needs_sklearn
def test_infer_with_pinned_inactive_model(monkeypatch, tmp_path):
    """A scenario can pin a specific registered model even when another (or
    none) is active — the /infer model_id path."""
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "d", "rows": _training_rows()}).json()
    m = c.post("/models/train", json={"name": "pinned", "dataset_ids": [ds["id"]],
                                      "activate": False}).json()
    out = c.post("/infer", json={
        "kind": "categorize", "model_id": m["id"],
        "text": "payShield LMK check failed slot 3"}).json()
    assert out["category"] == "hsm-key-problem"
    assert out["model_version"].startswith(m["id"])
    # without the pin, the (inactive) model is NOT consulted
    out2 = c.post("/infer", json={
        "kind": "categorize",
        "text": "payShield LMK check failed slot 3"}).json()
    assert out2["model_version"] != out["model_version"]


def test_train_without_sklearn_or_artifacts_fails_clean(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ds = c.post("/datasets", json={"name": "d", "rows": _training_rows()}).json()
    if not HAVE_SKLEARN:
        r = c.post("/models/train", json={"name": "m", "dataset_ids": [ds["id"]]})
        assert r.status_code == 422 and "scikit-learn" in r.json()["detail"]
    monkeypatch.delenv("INSIGHT_DATA_DIR")
    monkeypatch.setenv("INSIGHT_DB", ":memory:")
    r = c.post("/models/train", json={"name": "m", "dataset_ids": [ds["id"]]})
    assert r.status_code in (409, 422)  # no artifact dir (or no sklearn)