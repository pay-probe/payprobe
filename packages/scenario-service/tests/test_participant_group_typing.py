"""A participant group is typed: all members must share one adapter family."""
import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _conn(client, name, adapter, protocol):
    r = client.put(
        f"/connections/{name}",
        json={"adapter": adapter, "protocol": protocol, "host": "127.0.0.1", "port": 7000},
    )
    assert r.status_code in (200, 201)


def test_group_rejects_mixed_adapter_types(client):
    _conn(client, "iso_a", "tcp", "iso8583")
    _conn(client, "hsm_a", "tcp", "header_echo")
    r = client.post("/participant-groups", json={
        "name": "mix",
        "members": [{"connection": "iso_a", "weight": 1},
                    {"connection": "hsm_a", "weight": 1}],
    })
    assert r.status_code == 400
    assert "single adapter type" in r.json()["detail"]


def test_group_accepts_one_type_and_stamps_adapter_type(client):
    _conn(client, "iso_a", "tcp", "iso8583")
    _conn(client, "iso_b", "tcp", "iso8583")
    r = client.post("/participant-groups", json={
        "name": "issuers",
        "members": [{"connection": "iso_a", "weight": 1},
                    {"connection": "iso_b", "weight": 1}],
    })
    assert r.status_code == 201
    assert r.json()["adapter_type"] == "iso8583"   # stamped from members


def test_group_rejects_unknown_member(client):
    r = client.post("/participant-groups", json={
        "name": "bad", "members": [{"connection": "nope", "weight": 1}],
    })
    assert r.status_code == 400
