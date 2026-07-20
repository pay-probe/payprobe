"""Encryption-at-rest for stored connection secrets."""
import json

from api.crypto import SecretBox, is_secret_key
from api.connection_store import ConnectionStore, ConnectionDraft


def _key() -> str:
    return SecretBox.generate_key()


# -- SecretBox ---------------------------------------------------------------

def test_disabled_box_is_passthrough():
    box = SecretBox(key=None)
    assert not box.enabled
    assert box.encrypt("hunter2") == "hunter2"
    assert box.decrypt("hunter2") == "hunter2"


def test_roundtrip_and_tagging():
    box = SecretBox(key=_key())
    assert box.enabled
    ct = box.encrypt("hunter2")
    assert ct.startswith("enc:v1:")
    assert ct != "hunter2"
    assert box.decrypt(ct) == "hunter2"
    # idempotent: never double-wrap
    assert box.encrypt(ct) == ct


def test_secret_key_detection():
    assert is_secret_key("password")
    assert is_secret_key("api_key")
    assert is_secret_key("client_secret")
    assert is_secret_key("zmk_secret")
    assert is_secret_key("private_key")
    # not secret
    assert not is_secret_key("host")
    assert not is_secret_key("port")
    assert not is_secret_key("public_key")
    assert not is_secret_key("key")  # too generic → left readable


def test_encrypt_doc_only_touches_secret_fields():
    box = SecretBox(key=_key())
    doc = {"host": "10.0.0.1", "port": 9000,
           "sign_on": {"password": "p@ss", "user": "acq1"},
           "framing": {"length_prefix_bytes": 2}}
    enc = box.encrypt_doc(doc)
    assert enc["host"] == "10.0.0.1"           # untouched
    assert enc["port"] == 9000
    assert enc["sign_on"]["user"] == "acq1"    # non-secret untouched
    assert enc["sign_on"]["password"].startswith("enc:v1:")
    # decrypt restores
    assert box.decrypt_doc(enc) == doc


# -- ConnectionStore at rest -------------------------------------------------

def test_connection_file_is_encrypted_but_api_is_plaintext(tmp_path):
    f = tmp_path / "connections.json"
    box = SecretBox(key=_key())
    store = ConnectionStore(str(f), secret_box=box)
    store.upsert("acq", ConnectionDraft(host="10.0.0.1", port=9000,
                                        api_key="super-secret"))  # type: ignore[call-arg]

    # on disk: the secret is ciphertext
    raw = json.loads(f.read_text())["connections"]["acq"]
    assert raw["api_key"].startswith("enc:v1:")
    assert "super-secret" not in f.read_text()

    # via the API: plaintext is restored
    got = store.get("acq")
    assert got.api_key == "super-secret"  # type: ignore[attr-defined]

    # a fresh store (reload from disk) also decrypts
    store2 = ConnectionStore(str(f), secret_box=box)
    assert store2.get("acq").api_key == "super-secret"  # type: ignore[attr-defined]


def test_legacy_plaintext_file_still_loads(tmp_path):
    f = tmp_path / "connections.json"
    f.write_text(json.dumps({"connections": {"acq": {
        "host": "10.0.0.1", "port": 9000, "api_key": "legacy-plain"}}}))
    box = SecretBox(key=_key())
    store = ConnectionStore(str(f), secret_box=box)
    # reads legacy plaintext fine
    assert store.get("acq").api_key == "legacy-plain"  # type: ignore[attr-defined]
    # and migrates to ciphertext on next save
    store.upsert("acq2", ConnectionDraft(host="h", port=1, password="x"))  # type: ignore[call-arg]
    raw = json.loads(f.read_text())["connections"]
    assert raw["acq"]["api_key"].startswith("enc:v1:")
    assert raw["acq2"]["password"].startswith("enc:v1:")


def test_no_key_keeps_plaintext(tmp_path):
    f = tmp_path / "connections.json"
    store = ConnectionStore(str(f), secret_box=SecretBox(key=None))
    store.upsert("acq", ConnectionDraft(host="h", port=1, password="x"))  # type: ignore[call-arg]
    raw = json.loads(f.read_text())["connections"]["acq"]
    assert raw["password"] == "x"  # unchanged when encryption disabled
