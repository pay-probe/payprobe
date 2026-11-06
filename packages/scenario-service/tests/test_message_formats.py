"""Message Format ``definition.bitmap_encoding`` (a raw-binary bitmap under an
otherwise-ASCII dialect — the BPC SmartVista / SVFE POS shape): accepted and
persisted on save, folded into the wire encoding, and a misspelt value is a
422 rather than an ASCII bind."""

import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app
from payprobe_common.iso8583 import definition_encoding


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _draft(f: dict, **definition_changes) -> dict:
    d = {k: f[k] for k in ("protocol", "name", "version", "description", "definition")}
    d["definition"] = {**d["definition"], **definition_changes}
    return d


def test_bitmap_encoding_round_trips_on_a_cloned_ascii_dialect(client):
    cloned = client.post("/formats/iso8583-1987/clone",
                         json={"name": "BPC8583POS / SVFE", "version": "1987"})
    assert cloned.status_code == 201
    f = cloned.json()
    fid = f["id"]
    assert f["definition"]["encoding"] == "ascii"

    r = client.put(f"/formats/{fid}", json=_draft(f, bitmap_encoding="binary"))
    assert r.status_code == 200, r.text
    stored = client.get(f"/formats/{fid}").json()["definition"]
    assert stored["encoding"] == "ascii" and stored["bitmap_encoding"] == "binary"
    # what a bound simulator / connection packs and decodes with
    assert definition_encoding(stored) == {
        "bitmap": "binary", "numeric": "ascii", "text": "ascii", "binary": "hex", "length": "ascii",
    }
    assert client.delete(f"/formats/{fid}").status_code == 204


def test_bad_bitmap_encoding_is_refused_on_save(client):
    cloned = client.post("/formats/iso8583-1987/clone", json={"name": "typo", "version": "1"})
    f = cloned.json()
    r = client.put(f"/formats/{f['id']}", json=_draft(f, bitmap_encoding="raw"))
    assert r.status_code == 422
    assert "bitmap_encoding cannot be 'raw'" in r.text
    assert "bitmap_encoding" not in client.get(f"/formats/{f['id']}").json()["definition"]
