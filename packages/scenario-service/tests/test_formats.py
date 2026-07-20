"""Tests for the Message Format registry (versioned ISO8583 / ISO20022 specs)."""

import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_builtin_formats_present(client):
    ids = {f["id"] for f in client.get("/formats").json()}
    assert {
        "iso8583-1987", "iso8583-1993", "visa-base1",
        "iso20022-pacs008",
    } <= ids


def test_iso8583_dialects_carry_mti_lists(client):
    """Each ISO 8583 dialect declares its MTI menu, and the version generation is
    reflected: 1987/VISA use 0xxx, 1993 uses 1xxx — so the step's MTI list
    switches with the chosen dialect."""
    formats = {f["id"]: f for f in client.get("/formats").json()}
    for fid in ("iso8583-1987", "visa-base1"):
        mti = formats[fid]["definition"]["mti"]
        assert mti and all(m.startswith("0") for m in mti), fid
    mti = formats["iso8583-1993"]["definition"]["mti"]
    assert mti and all(m.startswith("1") for m in mti)
    # 1993 network management is 1804.
    assert "1804" in mti


def test_tcp_iso8583_step_has_dialect_picker(client):
    """The tcp_iso8583 send_message action exposes a Message Format (dialect)
    picker and an MTI enum whose options follow the selected dialect."""
    cat = client.get("/catalog").json()
    tgt = next(t for t in cat if t["target"] == "tcp_iso8583")
    send = next(a for a in tgt["actions"] if a["name"] == "send_message")
    params = {p["name"]: p for p in send["params"]}
    assert params["message_format_id"]["type"] == "format"
    assert params["message_format_id"]["protocol"] == "iso8583"
    assert params["mti"]["options_from_format"] is True


def test_visa_base1_field_table_and_presence(client):
    """The visa-base1 builtin carries the VISA simulator's DE table — the ISO
    8583:1987 core plus the VISA private fields its flows touch — with the
    framing the simulator expects, and a presence matrix for its flows.

    These DE framings mirror ``worker.adapters.scheme.visa.VISA_FIELDS`` (the
    simulator's offline fallback); keep the two in sync."""
    fmt = next(f for f in client.get("/formats").json() if f["id"] == "visa-base1")
    assert fmt["protocol"] == "iso8583"
    fields = fmt["definition"]["fields"]
    expected = {
        "43": ("fixed", 40), "44": ("llvar", 25), "48": ("lllvar", 999),
        "54": ("lllvar", 120), "60": ("lllvar", 999), "62": ("lllvar", 999),
        "63": ("lllvar", 999), "90": ("fixed", 42), "95": ("fixed", 42),
        "2": ("llvar", 19), "39": ("fixed", 2), "55": ("lllvar", 999),
    }
    for de, (lt, ln) in expected.items():
        assert de in fields, f"DE {de} missing from visa-base1"
        assert fields[de]["len_type"] == lt, de
        assert fields[de]["length"] == ln, de
    assert {"0100", "0200", "0400", "0420", "0800"} <= set(fmt["definition"]["presence"])


def test_iso8583_1993_reflects_1993_changes(client):
    fmt = next(f for f in client.get("/formats").json() if f["id"] == "iso8583-1993")
    assert fmt["protocol"] == "iso8583"
    assert fmt["version"] == "1993"
    fields = fmt["definition"]["fields"]
    # DE 39 is the 3-char Action Code in 1993 (vs 2-char Response Code in 1987)
    assert fields["39"]["length"] == 3
    # DE 22 is the 12-char POS Data Code in 1993
    assert fields["22"]["length"] == 12
    # 1993 additions present
    assert "56" in fields and "95" in fields


def test_filter_by_protocol(client):
    iso = client.get("/formats?protocol=iso8583").json()
    assert iso and all(f["protocol"] == "iso8583" for f in iso)
    assert "fields" in iso[0]["definition"]


def test_clone_edit_delete_cycle(client):
    cloned = client.post(
        "/formats/iso8583-1987/clone",
        json={"name": "Acme Acquirer", "version": "2.1"},
    )
    assert cloned.status_code == 201
    f = cloned.json()
    assert f["builtin"] is False
    fid = f["id"]

    # edit: add a data element
    f["definition"]["fields"]["48"] = {
        "name": "Additional Data", "len_type": "lllvar", "length": 999, "type": "ans",
    }
    draft = {k: f[k] for k in ("protocol", "name", "version", "description", "definition")}
    assert client.put(f"/formats/{fid}", json=draft).status_code == 200
    assert "48" in client.get(f"/formats/{fid}").json()["definition"]["fields"]

    assert client.delete(f"/formats/{fid}").status_code == 204


def test_builtin_cannot_be_deleted(client):
    assert client.delete("/formats/iso8583-1987").status_code == 400


def test_custom_format_group_roundtrip(client):
    r = client.post("/formats", json={
        "protocol": "iso8583",
        "name": "Acme Auth",
        "version": "1",
        "group": "Acme Acquirer",
        "definition": {"encoding": "ascii", "fields": {}},
    })
    assert r.status_code == 201
    fid = r.json()["id"]
    assert r.json()["group"] == "Acme Acquirer"
    # survives a fetch
    assert client.get(f"/formats/{fid}").json()["group"] == "Acme Acquirer"
    # built-ins are filed under the Standard group
    standard = {f["id"]: f for f in client.get("/formats").json()}
    assert standard["iso8583-1987"]["group"] == "Standard"


def test_clone_preserves_group(client):
    client.post("/formats", json={
        "protocol": "iso8583", "name": "Grouped", "version": "1",
        "group": "Beta Bank", "definition": {"encoding": "ascii", "fields": {}},
    })
    src = next(f for f in client.get("/formats").json() if f["name"] == "Grouped")
    cloned = client.post(f"/formats/{src['id']}/clone", json={"name": "Grouped copy"})
    assert cloned.status_code == 201
    assert cloned.json()["group"] == "Beta Bank"


def test_create_custom_format(client):
    r = client.post("/formats", json={
        "protocol": "iso20022",
        "name": "Custom pain.001",
        "version": "001.09",
        "definition": {"message_type": "pain.001.001.09"},
    })
    assert r.status_code == 201
    assert r.json()["protocol"] == "iso20022"
    assert r.json()["builtin"] is False
