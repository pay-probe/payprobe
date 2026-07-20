"""Tests for the editable step catalog (custom targets, overrides, hide)."""

import os

os.environ["DATABASE_URL"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _custom_http_target():
    return {
        "target": "my_api",
        "label": "My API",
        "category": "Custom",
        "color": "#7ee787",
        "custom": True,
        "actions": [
            {
                "name": "ping",
                "label": "Ping",
                "payload_hint": {"host": "string"},
                "response_fields": ["status"],
                "behavior": {
                    "kind": "http",
                    "template": {"method": "GET", "url": "https://${host}/ping"},
                },
            }
        ],
    }


def test_catalog_lists_builtins(client):
    targets = {t["target"] for t in client.get("/catalog").json()}
    assert {"http", "hsm"} <= targets


def test_emv_tools_group_is_code_backed(client):
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    assert "emv_tools" in targets
    emv = targets["emv_tools"]
    names = {a["name"] for a in emv["actions"]}
    assert {"tlv_parse", "tlv_build", "track2_parse"} <= names
    tlv = next(a for a in emv["actions"] if a["name"] == "tlv_parse")
    assert tlv["behavior"]["kind"] == "code"
    assert "def _parse" in tlv["behavior"]["template"]["code"]


def test_code_tool_modes_offer_enum_options(client):
    """Encode/decode-style inputs ship dropdown options instead of free text."""
    targets = {t["target"]: t for t in client.get("/catalog").json()}

    def _inputs(target, action):
        a = next(x for x in targets[target]["actions"] if x["name"] == action)
        return {r["name"]: r for r in a["behavior"]["template"]["inputs"]}

    hex_ascii = _inputs("emv_tools", "hex_ascii")["mode"]
    assert hex_ascii["options"] == ["decode", "encode"]

    bcd = _inputs("emv_tools", "hex_dec_bcd")["mode"]
    assert "dec2bcd" in bcd["options"]

    bitmap = _inputs("emv_tools", "iso_bitmap")["mode"]
    assert bitmap["options"] == ["decode", "build"]

    mti = _inputs("iso_messaging", "iso8583_pack")["mti"]
    assert "0200" in mti["options"] and "0810" in mti["options"]
    # non-enum inputs stay free-text (no options key)
    assert "options" not in _inputs("emv_tools", "hex_ascii")["value"]


def test_emv_dol_builder_present_and_runs():
    """dol_build concatenates DOL values in order → ARQC data (run the snippet)."""
    import sys
    sys.path.insert(0, ".")
    from models.emv_catalog import EMV_TOOLS_TARGET

    a = next(x for x in EMV_TOOLS_TARGET.actions if x.name == "dol_build")
    assert a.behavior["kind"] == "code"
    tpl = a.behavior["template"]
    inputs = {r["name"]: r["value"] for r in tpl["inputs"]}
    src = "def _ud(inputs, context):\n" + "".join(
        "    " + ln + "\n" for ln in tpl["code"].split("\n")
    )
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - trusted template
    out = ns["_ud"](inputs, {})
    # 10 elements summing to 33 bytes (66 hex chars), nothing missing
    assert out["length"] == 33
    assert out["data"].startswith("000000001000")  # amount 9F02 first
    assert out["missing_tags"] == []


def test_iso_messaging_group_present(client):
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    assert "iso_messaging" in targets
    names = {a["name"] for a in targets["iso_messaging"]["actions"]}
    assert {"iso8583_parse", "iso8583_pack", "iso20022_build"} <= names


def test_iso8583_steps_expose_format_picker_input(client):
    """ISO8583 pack/parse carry a `fields` input wired to the format registry."""
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    for action in ("iso8583_parse", "iso8583_pack"):
        a = next(x for x in targets["iso_messaging"]["actions"] if x["name"] == action)
        rows = {r["name"]: r for r in a["behavior"]["template"]["inputs"]}
        fields = rows["fields"]
        assert fields["format"] == "iso8583"
        assert fields["formatId"] == "iso8583-1987"
        # value is the snapshotted DE table (JSON), not embedded in the code
        assert '"2"' in fields["value"]
        # the code reads FIELDS from the input rather than a hard-coded literal
        assert 'inputs.get("fields")' in a["behavior"]["template"]["code"]


def test_emv_crypto_group_is_crypto_backed(client):
    """The EMV Crypto pack ships crypto-behaviour steps for the cryptogram flow."""
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    assert "emv_crypto" in targets
    actions = {a["name"]: a for a in targets["emv_crypto"]["actions"]}
    assert {
        "derive_icc_mk", "derive_session_key",
        "generate_arqc", "verify_arqc", "generate_arpc", "verify_arpc",
    } <= set(actions)
    gen = actions["generate_arpc"]
    assert gen["behavior"]["kind"] == "crypto"
    assert gen["behavior"]["template"]["operation"] == "arpc"
    # verify steps preset an (empty) expected field so they verify, not generate
    assert "expected" in actions["verify_arqc"]["behavior"]["template"]
    # PIN block encode + decode round out the HSM pack
    assert actions["pin_block"]["behavior"]["template"]["operation"] == "pin_block_encode"
    assert actions["pin_block_decode"]["behavior"]["template"]["operation"] == "pin_block_decode"


def test_add_custom_target(client):
    r = client.put("/catalog/targets/my_api", json=_custom_http_target())
    assert r.status_code == 200
    assert r.json()["custom"] is True

    merged = {t["target"]: t for t in client.get("/catalog").json()}
    assert "my_api" in merged
    action = merged["my_api"]["actions"][0]
    assert action["behavior"]["kind"] == "http"


def test_builtin_actions_expose_typed_params(client):
    targets = {t["target"]: t for t in client.get("/catalog").json()}
    auth = next(
        a for a in targets["http"]["actions"] if a["name"] == "send_auth_request"
    )
    params = {p["name"]: p for p in auth["params"]}
    assert params["amount"]["type"] == "number"
    assert params["amount"]["required"] is True
    assert params["currency"]["type"] == "enum"
    assert "USD" in params["currency"]["options"]


def test_custom_target_params_roundtrip(client):
    spec = _custom_http_target()
    spec["actions"][0]["params"] = [
        {
            "name": "method",
            "label": "HTTP method",
            "type": "enum",
            "options": ["GET", "POST"],
            "required": True,
            "default": "GET",
        },
        {"name": "retries", "label": "Retries", "type": "number", "default": 3},
    ]
    assert client.put("/catalog/targets/my_api", json=spec).status_code == 200

    merged = {t["target"]: t for t in client.get("/catalog").json()}
    params = {p["name"]: p for p in merged["my_api"]["actions"][0]["params"]}
    assert params["method"]["type"] == "enum"
    assert params["method"]["options"] == ["GET", "POST"]
    assert params["retries"]["type"] == "number"
    assert params["retries"]["default"] == 3


def test_target_id_must_match_url(client):
    spec = _custom_http_target()
    r = client.put("/catalog/targets/other_id", json=spec)
    assert r.status_code == 400


def test_target_needs_an_action(client):
    spec = _custom_http_target()
    spec["actions"] = []
    r = client.put("/catalog/targets/my_api", json=spec)
    assert r.status_code == 422


def test_override_and_restore_builtin(client):
    builtins = {t["target"]: t for t in client.get("/catalog").json()}
    http = builtins["http"]
    http["label"] = "RestPay (edited)"
    assert client.put("/catalog/targets/http", json=http).status_code == 200

    merged = {t["target"]: t for t in client.get("/catalog").json()}
    assert merged["http"]["label"] == "RestPay (edited)"
    assert merged["http"]["custom"] is True

    # manage view flags it as an overridden built-in
    manage = {t["target"]: t for t in client.get("/catalog/manage").json()["targets"]}
    assert manage["http"]["builtin"] is True
    assert manage["http"]["overridden"] is True

    client.post("/catalog/targets/http/restore")
    restored = {t["target"]: t for t in client.get("/catalog").json()}
    assert restored["http"]["label"] == "HTTP / REST"


def test_hide_and_restore_builtin(client):
    assert client.delete("/catalog/targets/hsm").status_code == 204
    assert "hsm" not in {t["target"] for t in client.get("/catalog").json()}

    manage = {t["target"]: t for t in client.get("/catalog/manage").json()["targets"]}
    assert manage["hsm"]["hidden"] is True

    client.post("/catalog/targets/hsm/restore")
    assert "hsm" in {t["target"] for t in client.get("/catalog").json()}


def test_delete_custom_target(client):
    client.put("/catalog/targets/my_api", json=_custom_http_target())
    assert client.delete("/catalog/targets/my_api").status_code == 204
    assert "my_api" not in {t["target"] for t in client.get("/catalog").json()}
    # deleting a non-existent custom target 404s
    assert client.delete("/catalog/targets/nope").status_code == 404


def test_nats_target_in_catalog(client):
    """NATS steps (ADR-0006) are offered in the palette: request / publish /
    js_publish, each with typed params and the reply exposed under `data`."""
    cat = client.get("/catalog").json()
    nats = next((t for t in cat if t["target"] == "nats"), None)
    assert nats is not None, "nats target missing from /catalog"
    actions = {a["name"] for a in nats["actions"]}
    assert actions == {"request", "publish", "js_publish"}
    req = next(a for a in nats["actions"] if a["name"] == "request")
    assert "data" in req["response_fields"]
    assert {p["name"] for p in req["params"]} == {"subject", "body", "timeout"}
