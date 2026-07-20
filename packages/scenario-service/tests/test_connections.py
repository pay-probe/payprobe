"""ConnectionStore: persistence + protocol-specific key round-trip."""
import json

from api.connection_store import ConnectionStore, ConnectionDraft


def _draft(**extra):
    return ConnectionDraft(adapter="tcp", protocol="iso8583",
                           host="10.0.1.50", port=7001, **extra)


def test_upsert_get_list_preserves_extra_keys():
    s = ConnectionStore(":memory:")
    s.upsert("switch_visa", _draft(framing={"length_prefix_bytes": 2},
                                   correlation={"match_mti": True}))
    c = s.get("switch_visa")
    assert c is not None
    assert c.name == "switch_visa" and c.protocol == "iso8583"
    doc = c.model_dump()
    assert doc["framing"] == {"length_prefix_bytes": 2}
    assert doc["correlation"] == {"match_mti": True}
    assert [x.name for x in s.list()] == ["switch_visa"]


def test_legacy_listen_port_folds_into_single_port():
    """A legacy inbound connection (port=dial, listen_port=bind) collapses to one
    port: the bind port wins and listen_port is dropped."""
    c = ConnectionDraft(adapter="tcp", protocol="iso8583", host="127.0.0.1",
                        port=8000, mode="inbound", listen_port=9000)
    assert c.port == 9000          # the bind port becomes the single port
    assert c.listen_port is None   # legacy field cleared

    # stored legacy record self-heals on read (list/get build through the model)
    s = ConnectionStore(":memory:")
    s._connections["listener"] = {"adapter": "tcp", "protocol": "iso8583",
                                  "host": "127.0.0.1", "port": 8000,
                                  "mode": "inbound", "listen_port": 9000}
    got = s.get("listener")
    assert got.port == 9000 and got.listen_port is None


def test_name_is_slugged_and_not_stored_in_body():
    s = ConnectionStore(":memory:")
    c = s.upsert("Switch VISA", _draft())
    assert c.name == "switch_visa"  # slugged
    assert "name" not in s._connections["switch_visa"]


def test_header_echo_connection_round_trips():
    s = ConnectionStore(":memory:")
    s.upsert("hsm_primary", ConnectionDraft(
        adapter="tcp", protocol="header_echo", host="10.0.2.10", port=1500,
        header_echo={"header_bytes": 4, "diagnostic_command": "NC"}))
    c = s.get("hsm_primary")
    assert c.protocol == "header_echo"
    assert c.model_dump()["header_echo"]["header_bytes"] == 4


def test_grpc_connection_round_trips():
    s = ConnectionStore(":memory:")
    s.upsert("payments_grpc", ConnectionDraft(
        adapter="grpc", target="localhost:50051",
        descriptor_set_path="bundle.pb", timeout_sec=15,
        metadata={"authorization": "Bearer x"},
        actions={"authorize": {"method": "payments.PaymentService/Authorize"}}))
    c = s.get("payments_grpc")
    assert c is not None and c.adapter == "grpc"
    doc = c.model_dump()
    assert doc["target"] == "localhost:50051"
    assert doc["descriptor_set_path"] == "bundle.pb"
    assert doc["actions"]["authorize"]["method"] == "payments.PaymentService/Authorize"
    # no TCP host/port baked in when only a target was given
    assert "protocol" not in s._connections["payments_grpc"]


def test_rest_connection_round_trips():
    """A REST/http connection registers and keeps its adapter-specific keys, with
    no bogus tcp ``protocol`` baked in (Bucket B extension)."""
    s = ConnectionStore(":memory:")
    s.upsert("acquirer_api", ConnectionDraft(
        adapter="http", base_url="https://api.example.com",
        api_key="SH-SECRET", timeout_ms=5000))
    c = s.get("acquirer_api")
    assert c is not None and c.adapter == "http"
    doc = c.model_dump()
    assert doc["base_url"] == "https://api.example.com"
    assert doc["timeout_ms"] == 5000
    # protocol is a tcp wire concept — not forced onto a REST connection
    assert "protocol" not in s._connections["acquirer_api"]


def test_payshield_and_db_connections_register():
    s = ConnectionStore(":memory:")
    s.upsert("hsm1", ConnectionDraft(
        adapter="payshield", host="10.0.2.10", port=1500, header="HDR1"))
    s.upsert("core_db", ConnectionDraft(
        adapter="db_probe_core", engine="postgresql",
        host="db.internal", dbname="core", user="ro", password="pw"))
    assert s.get("hsm1").adapter == "payshield"
    assert s.get("hsm1").model_dump()["header"] == "HDR1"
    db = s.get("core_db").model_dump()
    assert db["engine"] == "postgresql" and db["dbname"] == "core"


def test_rest_secret_encrypted_at_rest(tmp_path):
    from api.crypto import SecretBox
    path = tmp_path / "connections.json"
    box = SecretBox(SecretBox.generate_key())
    s = ConnectionStore(str(path), secret_box=box)
    s.upsert("acquirer_api", ConnectionDraft(
        adapter="http", base_url="https://api.example.com", api_key="SH-SECRET"))
    on_disk = json.loads(path.read_text())["connections"]["acquirer_api"]
    assert on_disk["api_key"].startswith("enc:v1:") and "SH-SECRET" not in on_disk["api_key"]
    assert on_disk["base_url"] == "https://api.example.com"  # non-secret stays readable


# -- default connection (one per adapter type) --------------------------------

def test_default_flag_round_trips_and_is_queryable():
    s = ConnectionStore(":memory:")
    s.upsert("primary_switch", ConnectionDraft(
        adapter="tcp", protocol="iso8583", host="10.0.1.50", port=7001, default=True))
    c = s.get("primary_switch")
    assert c.default is True
    d = s.get_default("tcp", "iso8583")
    assert d is not None and d.name == "primary_switch"
    assert [x.name for x in s.defaults()] == ["primary_switch"]


def test_setting_a_second_default_demotes_the_first():
    s = ConnectionStore(":memory:")
    s.upsert("switch_a", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                         host="h1", port=7001, default=True))
    s.upsert("switch_b", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                         host="h2", port=7002, default=True))
    # only the most recent default of this type survives
    assert s.get("switch_a").default is False
    assert s.get("switch_b").default is True
    assert s.get_default("tcp", "iso8583").name == "switch_b"


def test_distinct_types_each_keep_their_own_default():
    s = ConnectionStore(":memory:")
    s.upsert("iso_def", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                        host="h", port=7001, default=True))
    s.upsert("hsm_def", ConnectionDraft(adapter="tcp", protocol="header_echo",
                                        host="h", port=1500, default=True))
    s.upsert("rest_def", ConnectionDraft(adapter="http", base_url="https://x", default=True))
    # tcp/iso8583, tcp/header_echo and http are distinct type slots
    assert s.get("iso_def").default is True
    assert s.get("hsm_def").default is True
    assert s.get("rest_def").default is True
    assert s.get_default("tcp", "header_echo").name == "hsm_def"
    assert s.get_default("http").name == "rest_def"
    assert {x.name for x in s.defaults()} == {"iso_def", "hsm_def", "rest_def"}


def test_edit_without_default_field_preserves_it():
    s = ConnectionStore(":memory:")
    s.upsert("sw", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                   host="h", port=7001, default=True))
    # an edit that doesn't mention `default` must not silently un-set it
    s.upsert("sw", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                   host="h2", port=7001))
    assert s.get("sw").default is True
    # explicitly clearing it works
    s.upsert("sw", ConnectionDraft(adapter="tcp", protocol="iso8583",
                                   host="h2", port=7001, default=False))
    assert s.get("sw").default is False
    assert s.get_default("tcp", "iso8583") is None


def test_seed_from_dir_imports_connections_map(tmp_path):
    seed = tmp_path / "connections"
    seed.mkdir()
    (seed / "bundled.json").write_text(json.dumps({"connections": {
        "switch_visa": {"adapter": "tcp", "protocol": "iso8583", "host": "10.0.1.50", "port": 7001},
        "payments_grpc": {"adapter": "grpc", "target": "localhost:50051"},
        "payshield_hsm": {"adapter": "payshield", "host": "127.0.0.1", "port": 1500},
    }}))
    s = ConnectionStore(":memory:")
    n = s.seed_from_dir(str(seed))
    assert n == 3
    assert {c.name for c in s.list()} == {"switch_visa", "payments_grpc", "payshield_hsm"}
    assert s.get("payments_grpc").adapter == "grpc"
    # idempotent: a second seed is a no-op once populated
    assert s.seed_from_dir(str(seed)) == 0


def test_seed_from_dir_accepts_flat_single_connection(tmp_path):
    seed = tmp_path / "connections"
    seed.mkdir()
    (seed / "edge_switch.json").write_text(json.dumps(
        {"adapter": "tcp", "protocol": "iso8583", "host": "h", "port": 7000}))
    s = ConnectionStore(":memory:")
    assert s.seed_from_dir(str(seed)) == 1
    assert s.get("edge_switch").host == "h"  # keyed by filename stem


def test_rename_and_delete():
    s = ConnectionStore(":memory:")
    s.upsert("a", _draft())
    s.rename("a", "b")
    assert s.get("a") is None and s.get("b") is not None
    s.delete("b")
    assert s.get("b") is None


def test_validation_rejects_bad_config():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConnectionDraft(adapter="tcp", protocol="iso8583", host="h", port=70000)
    with pytest.raises(ValidationError):
        ConnectionDraft(adapter="tcp", protocol="smtp", host="h", port=7001)
    with pytest.raises(ValidationError):
        ConnectionDraft(adapter="carrierpigeon", host="h", port=7001)
    with pytest.raises(ValidationError):
        ConnectionDraft(adapter="tcp", protocol="iso8583", host="h", port=7001,
                        framing={"length_prefix_bytes": 0})
    # valid passes
    ok = ConnectionDraft(adapter="tcp", protocol="header_echo", host="h", port=1500,
                         framing={"length_prefix_bytes": 2})
    assert ok.port == 1500


def test_persists_to_file(tmp_path):
    path = str(tmp_path / "connections.json")
    s1 = ConnectionStore(path)
    s1.upsert("switch_visa", _draft(framing={"length_prefix_bytes": 2}))
    # reopen -> survives "restart"
    s2 = ConnectionStore(path)
    assert s2.get("switch_visa").model_dump()["framing"] == {"length_prefix_bytes": 2}
    # stored under the documented shape
    raw = json.loads((tmp_path / "connections.json").read_text())
    assert "connections" in raw and "switch_visa" in raw["connections"]
