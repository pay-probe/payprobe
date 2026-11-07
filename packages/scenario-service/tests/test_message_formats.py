"""Message Format ``definition.bitmap_encoding`` (a raw-binary bitmap under an
otherwise-ASCII dialect — the shape some POS hosts use): accepted and
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
                         json={"name": "POS host dialect", "version": "1987"})
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


def test_inspector_builds_through_a_registered_format_with_a_binary_bitmap(client):
    cloned = client.post("/formats/iso8583-1987/clone", json={"name": "pos", "version": "1"})
    f = cloned.json()
    fields = {**f["definition"]["fields"],
              "24": {"name": "NII", "len_type": "fixed", "length": 3, "type": "n"}}
    d = {k: f[k] for k in ("protocol", "name", "version", "description")}
    d["definition"] = {**f["definition"], "fields": fields, "bitmap_encoding": "binary"}
    assert client.put(f"/formats/{f['id']}", json=d).status_code == 200
    values = {"2": "5488350005706204", "3": "000000", "4": "000000000200", "11": "000005",
              "24": "200", "35": ";5488350005706204=2912101?", "42": "MERCHANT01     ", "49": "981"}
    r = client.post("/iso8583/build", json={"mti": "0200", "values": values, "format_id": f["id"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["errors"] == []
    wire = bytes.fromhex(out["message"])
    assert wire[:4] == b"0200" and wire[4:12] == bytes.fromhex("7020010020408000")  # DE 2,3,4 · 11 · 24 · 35 · 42 · 49
    assert wire[12:] == (b"165488350005706204" b"000000" b"000000000200" b"000005" b"200"
                         b"26;5488350005706204=2912101?" b"MERCHANT01     " b"981")
    # the bitmap axis on its own, with inline fields, no format
    r2 = client.post("/iso8583/build", json={"mti": "0200", "values": values, "fields": fields,
                                             "bitmap_encoding": "binary"})
    assert r2.status_code == 200 and r2.json()["message"] == out["message"]
    # and the message analyzes back under the same dialect
    a = client.post("/iso8583/analyze", json={"message": out["message"], "format_id": f["id"]})
    assert a.status_code == 200, a.text
    assert a.json()["mti"] == "0200"
    assert client.post("/iso8583/build", json={"mti": "0200", "values": values,
                                               "format_id": "nope"}).status_code == 404
    assert client.post("/iso8583/build", json={"mti": "0200", "values": values,
                                               "bitmap_encoding": "raw"}).status_code == 400
    client.delete(f"/formats/{f['id']}")
