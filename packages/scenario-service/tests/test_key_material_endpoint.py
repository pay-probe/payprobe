"""The service-gated key-material endpoint (`${key.NAME}` resolver source).

The vault rule is "reads mask" — for *people*. The orchestrator's run-build
resolver needs the actual hex, so /test-data/keys/{name}/material exists but
only answers service credentials (static bearer / minted service JWT / open
dev). A logged-in user's JWT gets a 403 and the masked view stays masked.
"""
import os
import time

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app

MATERIAL = "0123456789ABCDEF0123456789ABCDEF"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed(client) -> None:
    r = client.put("/test-data/keys/visa_cvk",
                   json={"description": "cvk", "type": "generic", "value": MATERIAL})
    assert r.status_code == 200


def test_material_flow_by_credential_kind(client, monkeypatch):
    pyjwt = pytest.importorskip("jwt")
    _seed(client)

    # open dev/test mode (conftest): the internal resolver path works locally
    r = client.get("/test-data/keys/visa_cvk/material")
    assert r.status_code == 200 and r.json()["value"] == MATERIAL

    # configured JWT auth: a USER token must NOT reveal material…
    monkeypatch.setenv("AUTH_JWT_SECRET", "s3cret")
    now = int(time.time())
    user = pyjwt.encode({"sub": "alice", "roles": ["admin"],
                         "iat": now, "exp": now + 60}, "s3cret", algorithm="HS256")
    r = client.get("/test-data/keys/visa_cvk/material",
                   headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 403

    # …while the masked view still works for that user (no value field)
    r = client.get("/test-data/keys/visa_cvk",
                   headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 200 and "value" not in r.json()

    # a minted service JWT (svc claim, as the orchestrator sends) gets material
    svc = pyjwt.encode({"sub": "orchestrator", "svc": "orchestrator",
                        "iat": now, "exp": now + 60}, "s3cret", algorithm="HS256")
    r = client.get("/test-data/keys/visa_cvk/material",
                   headers={"Authorization": f"Bearer {svc}"})
    assert r.status_code == 200 and r.json()["value"] == MATERIAL

    # static bearer (the other service credential) also gets material
    monkeypatch.delenv("AUTH_JWT_SECRET")
    monkeypatch.setenv("API_TOKEN", "svc-token")
    r = client.get("/test-data/keys/visa_cvk/material",
                   headers={"Authorization": "Bearer svc-token"})
    assert r.status_code == 200 and r.json()["value"] == MATERIAL


def test_material_unknown_key_404(client):
    assert client.get("/test-data/keys/nope/material").status_code == 404
