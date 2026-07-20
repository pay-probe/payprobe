"""POST /participant-flows/{id}/duplicate — Save As / rename, with network-flow repoint."""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _flow(name):
    return {
        "name": name,
        "trigger": {"connection": "cn_listener"},
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "r", "kind": "reply", "payload": {"set": {"39": "00"}}},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "r"}],
    }


def test_duplicate_plain_keeps_source(client):
    src = client.post("/participant-flows", json=_flow("orig")).json()
    out = client.post(
        f"/participant-flows/{src['id']}/duplicate", json={"name": "copy"}
    ).json()
    assert out["flow"]["id"] != src["id"]          # fresh unique id
    assert out["flow"]["name"] == "copy"
    assert out["deleted_source"] is False
    assert client.get(f"/participant-flows/{src['id']}").status_code == 200  # kept


def test_rename_repoints_network_flow_and_deletes_source(client):
    src = client.post("/participant-flows", json=_flow("untitled scenario")).json()
    # a network flow whose participant node points at the source flow
    net = client.post("/network-flows", json={
        "name": "main",
        "nodes": [{"id": "n1", "kind": "participant",
                   "config": {"flow_id": src["id"], "instances": 1}}],
    }).json()

    out = client.post(
        f"/participant-flows/{src['id']}/duplicate",
        json={"name": "auth_switch", "replace": True},
    ).json()
    new_id = out["flow"]["id"]

    assert new_id != src["id"] and out["flow"]["name"] == "auth_switch"
    assert out["deleted_source"] is True
    assert net["id"] in out["repointed_network_flows"]
    # source gone, copy present, network flow now points at the copy
    assert client.get(f"/participant-flows/{src['id']}").status_code == 404
    assert client.get(f"/participant-flows/{new_id}").status_code == 200
    reloaded = client.get(f"/network-flows/{net['id']}").json()
    assert reloaded["nodes"][0]["config"]["flow_id"] == new_id


def test_legacy_topologies_api_is_gone(client):
    assert client.get("/topologies").status_code in (404, 405)
