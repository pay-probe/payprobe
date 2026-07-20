"""Projects, named sets, classification, and global search."""

import json
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app

from .test_scenario_service import create, draft, step


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def make_project(client, name="Project Alpha", description=""):
    res = client.post("/projects", json={"name": name, "description": description})
    assert res.status_code == 201
    return res.json()


def make_set(client, project_id, name="smoke", scenario_ids=None):
    res = client.post(
        f"/projects/{project_id}/sets",
        json={"name": name, "scenario_ids": scenario_ids or []},
    )
    assert res.status_code == 201
    return res.json()


# -- projects -----------------------------------------------------------------


def test_default_project_exists(client):
    projects = client.get("/projects").json()
    ids = {p["id"] for p in projects}
    assert "prj-default" in ids
    default = next(p for p in projects if p["id"] == "prj-default")
    assert default["scenario_count"] >= 2  # seeded examples land here


def test_project_crud(client):
    p = make_project(client, "RestPay Staging", "staging regression")
    assert p["scenario_count"] == 0 and p["set_count"] == 0

    res = client.put(f"/projects/{p['id']}", json={"name": "RestPay Stg", "description": "x"})
    assert res.status_code == 200 and res.json()["name"] == "RestPay Stg"

    assert client.delete(f"/projects/{p['id']}").status_code == 204
    assert client.get(f"/projects/{p['id']}").status_code == 404


def test_delete_nonempty_project_refused(client):
    p = make_project(client)
    res = client.post(
        "/scenarios", json={"scenario": draft(name="case"), "project_id": p["id"]}
    )
    assert res.status_code == 201
    assert client.delete(f"/projects/{p['id']}").status_code == 409


def test_scenario_created_in_project(client):
    p = make_project(client)
    sc = client.post(
        "/scenarios", json={"scenario": draft(name="in alpha"), "project_id": p["id"]}
    ).json()
    assert sc["project_id"] == p["id"]
    listed = client.get("/scenarios", params={"project_id": p["id"]}).json()
    assert [s["id"] for s in listed] == [sc["id"]]
    # default project listing does not contain it
    other = client.get("/scenarios", params={"project_id": "prj-default"}).json()
    assert sc["id"] not in {s["id"] for s in other}


def test_move_scenario_between_projects(client):
    p1, p2 = make_project(client, "P1"), make_project(client, "P2")
    sc = client.post(
        "/scenarios", json={"scenario": draft(name="mover"), "project_id": p1["id"]}
    ).json()
    s = make_set(client, p1["id"], scenario_ids=[sc["id"]])
    res = client.put(
        f"/scenarios/{sc['id']}",
        json={"scenario": draft(name="mover"), "project_id": p2["id"]},
    )
    assert res.status_code == 200
    assert res.json()["project_id"] == p2["id"]
    # membership in the old project's set is dropped
    assert client.get(f"/sets/{s['id']}").json()["scenario_ids"] == []


# -- classification -------------------------------------------------------------


def test_test_class_default_and_filter(client):
    p = make_project(client)
    d = draft(name="component case")
    d["test_class"] = "component"
    client.post("/scenarios", json={"scenario": d, "project_id": p["id"]})
    client.post(
        "/scenarios", json={"scenario": draft(name="e2e case"), "project_id": p["id"]}
    )
    comp = client.get(
        "/scenarios", params={"project_id": p["id"], "test_class": "component"}
    ).json()
    assert [s["name"] for s in comp] == ["component case"]
    e2e = client.get(
        "/scenarios", params={"project_id": p["id"], "test_class": "e2e"}
    ).json()
    assert [s["name"] for s in e2e] == ["e2e case"]


def test_invalid_test_class_rejected(client):
    d = draft(name="bad class")
    d["test_class"] = "fuzz"
    assert client.post("/scenarios", json={"scenario": d}).status_code == 422


def test_test_class_survives_export_import(client):
    d = draft(name="classed", steps=[step("step_001")])
    d["test_class"] = "integration"
    sc = client.post("/scenarios", json={"scenario": d}).json()
    exported = client.get(f"/scenarios/{sc['id']}/export", params={"format": "json"}).text
    clone = client.post("/scenarios/import", content=exported).json()
    assert clone["test_class"] == "integration"


# -- named sets -------------------------------------------------------------------


def test_set_crud_and_membership(client):
    p = make_project(client)
    a = client.post(
        "/scenarios", json={"scenario": draft(name="a"), "project_id": p["id"]}
    ).json()
    b = client.post(
        "/scenarios", json={"scenario": draft(name="b"), "project_id": p["id"]}
    ).json()

    s = make_set(client, p["id"], name="nightly", scenario_ids=[a["id"]])
    assert s["scenario_ids"] == [a["id"]]

    # a case can be in multiple sets
    s2 = make_set(client, p["id"], name="smoke", scenario_ids=[a["id"], b["id"]])
    assert sorted(s2["scenario_ids"]) == sorted([a["id"], b["id"]])

    # incremental add / remove
    res = client.put(f"/sets/{s['id']}/scenarios/{b['id']}")
    assert res.status_code == 200 and len(res.json()["scenario_ids"]) == 2
    res = client.delete(f"/sets/{s['id']}/scenarios/{a['id']}")
    assert res.json()["scenario_ids"] == [b["id"]]

    # filter scenario list by set
    listed = client.get("/scenarios", params={"set_id": s["id"]}).json()
    assert [x["id"] for x in listed] == [b["id"]]

    assert client.delete(f"/sets/{s['id']}").status_code == 204
    assert client.get(f"/sets/{s['id']}").status_code == 404
    # deleting a set never deletes cases
    assert client.get(f"/scenarios/{b['id']}").status_code == 200


def test_set_rejects_foreign_scenario(client):
    p1, p2 = make_project(client, "P1"), make_project(client, "P2")
    foreign = client.post(
        "/scenarios", json={"scenario": draft(name="foreign"), "project_id": p2["id"]}
    ).json()
    res = client.post(
        f"/projects/{p1['id']}/sets",
        json={"name": "bad", "scenario_ids": [foreign["id"]]},
    )
    assert res.status_code == 422


def test_deleted_scenario_leaves_sets(client):
    p = make_project(client)
    a = client.post(
        "/scenarios", json={"scenario": draft(name="gone"), "project_id": p["id"]}
    ).json()
    s = make_set(client, p["id"], scenario_ids=[a["id"]])
    client.delete(f"/scenarios/{a['id']}")
    assert client.get(f"/sets/{s['id']}").json()["scenario_ids"] == []


# -- global search ------------------------------------------------------------------


def test_search_mixed_results(client):
    p = make_project(client, "Payment Gateway", "gateway tests")
    make_set(client, p["id"], name="gateway-smoke")
    client.post(
        "/scenarios",
        json={"scenario": draft(name="gateway timeout case"), "project_id": p["id"]},
    )
    res = client.get("/search", params={"q": "gateway"})
    assert res.status_code == 200
    body = res.json()
    assert [x["name"] for x in body["projects"]] == ["Payment Gateway"]
    assert [x["name"] for x in body["sets"]] == ["gateway-smoke"]
    assert [x["name"] for x in body["scenarios"]] == ["gateway timeout case"]


def test_search_scoped_to_project(client):
    p1, p2 = make_project(client, "S1"), make_project(client, "S2")
    client.post(
        "/scenarios", json={"scenario": draft(name="shared name"), "project_id": p1["id"]}
    )
    client.post(
        "/scenarios", json={"scenario": draft(name="shared name"), "project_id": p2["id"]}
    )
    body = client.get(
        "/search", params={"q": "shared", "project_id": p1["id"]}
    ).json()
    assert len(body["scenarios"]) == 1
    assert body["scenarios"][0]["project_id"] == p1["id"]


def test_search_by_tag_and_empty_query(client):
    sc = draft(name="tagged")
    sc["tags"] = ["emv", "nightly-only"]
    client.post("/scenarios", json={"scenario": sc})
    body = client.get("/search", params={"q": "nightly-only"}).json()
    assert [x["name"] for x in body["scenarios"]] == ["tagged"]
    empty = client.get("/search", params={"q": "  "}).json()
    assert empty["projects"] == [] and empty["scenarios"] == []


# -- move & duplicate -----------------------------------------------------------


def test_move_endpoint(client):
    p1, p2 = make_project(client, "M1"), make_project(client, "M2")
    sc = client.post(
        "/scenarios", json={"scenario": draft(name="movable"), "project_id": p1["id"]}
    ).json()
    s = make_set(client, p1["id"], scenario_ids=[sc["id"]])
    v_before = sc["version"]

    res = client.post(f"/scenarios/{sc['id']}/move", json={"project_id": p2["id"]})
    assert res.status_code == 200
    moved = res.json()
    assert moved["project_id"] == p2["id"]
    assert moved["version"] == v_before  # no new version for a move
    assert client.get(f"/sets/{s['id']}").json()["scenario_ids"] == []

    bad = client.post(f"/scenarios/{sc['id']}/move", json={"project_id": "prj-nope"})
    assert bad.status_code == 404


def test_duplicate_endpoint(client):
    p = make_project(client, "Dup")
    d = draft(name="original", steps=[step("step_001")])
    d["test_class"] = "integration"
    sc = client.post("/scenarios", json={"scenario": d, "project_id": p["id"]}).json()
    s = make_set(client, p["id"], name="smoke", scenario_ids=[sc["id"]])

    res = client.post(f"/scenarios/{sc['id']}/duplicate")
    assert res.status_code == 201
    copy = res.json()
    assert copy["id"] != sc["id"]
    assert copy["name"] == "original (copy)"
    assert copy["project_id"] == p["id"]
    assert copy["test_class"] == "integration"
    assert copy["steps"] == sc["steps"]
    assert copy["version"] == 1
    # set membership copied
    members = client.get(f"/sets/{s['id']}").json()["scenario_ids"]
    assert set(members) == {sc["id"], copy["id"]}

    assert client.post("/scenarios/nope/duplicate").status_code == 404
