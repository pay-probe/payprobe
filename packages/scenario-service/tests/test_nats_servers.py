"""NatsServerStore (ADR-0006): named broker clusters — persistence + secret
encryption-at-rest round-trip."""
import json

from api.nats_server_store import NatsServerStore, NatsServerDraft
from api.crypto import SecretBox


def _draft(**extra):
    return NatsServerDraft(
        name="PayProbe cluster",
        servers=["nats://nats-1:4222", "nats://nats-2:4223", "nats://nats-3:4224"],
        monitoring=["http://nats-1:8222", "http://nats-2:8222"],
        **extra,
    )


def test_upsert_get_list_slugs_and_preserves_fields():
    s = NatsServerStore(":memory:")
    saved = s.upsert("PayProbe cluster", _draft(description="prod bus"))
    assert saved.key == "payprobe-cluster"      # slugged registry key
    got = s.get("payprobe-cluster")
    assert got is not None
    assert got.servers[0] == "nats://nats-1:4222"
    assert got.monitoring == ["http://nats-1:8222", "http://nats-2:8222"]
    assert got.description == "prod bus"
    assert [x.key for x in s.list()] == ["payprobe-cluster"]


def test_delete_missing_raises_keyerror():
    s = NatsServerStore(":memory:")
    import pytest

    with pytest.raises(KeyError):
        s.delete("ghost")


def test_auth_secrets_encrypted_at_rest(tmp_path):
    """token / nkey_seed ride in auth and are encrypted on disk, plaintext in
    memory (invariant #8) — the same as Connections."""
    f = tmp_path / "nats_servers.json"
    box = SecretBox("k3vHc0k5nJh7wq8m8m6yQ2yq3nZ0m8kz6r7wq8m8m6y=")  # test Fernet key
    s = NatsServerStore(str(f), secret_box=box)
    s.upsert("c1", _draft(auth={"token": "s3cr3t-token", "nkey_seed": "SUAB..."}))

    on_disk = json.loads(f.read_text())["nats_servers"]["c1"]["auth"]
    assert on_disk["token"] != "s3cr3t-token"     # ciphertext on disk
    assert str(on_disk["token"]).startswith("enc:")

    # a fresh store with the same key decrypts back to plaintext
    s2 = NatsServerStore(str(f), secret_box=box)
    assert s2.get_config("c1")["auth"]["token"] == "s3cr3t-token"


def test_seed_default_only_when_empty():
    s = NatsServerStore(":memory:")
    assert s.seed_default(["nats://nats-1:4222"], ["http://nats-1:8222"]) is True
    got = s.get("payprobe-cluster")
    assert got is not None and got.servers == ["nats://nats-1:4222"]
    assert got.monitoring == ["http://nats-1:8222"]
    # second call is a no-op (never clobbers)
    assert s.seed_default(["nats://other:4222"]) is False
    assert s.get("payprobe-cluster").servers == ["nats://nats-1:4222"]


def test_seed_default_noop_with_no_servers():
    s = NatsServerStore(":memory:")
    assert s.seed_default([]) is False
    assert s.names() == []


def test_get_config_returns_raw_doc():
    s = NatsServerStore(":memory:")
    s.upsert("c2", _draft())
    cfg = s.get_config("c2")
    assert cfg["servers"][0] == "nats://nats-1:4222"
    assert s.get_config("missing") is None
