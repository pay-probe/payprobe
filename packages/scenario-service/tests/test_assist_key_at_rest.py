"""LLM key + NATS auth at rest (invariant 8): SecretBox in the stores.

The assistant's API key (Settings → AI assistant) and NATS cluster auth were
the last secret-bearing docs; the key is now ``enc:v1:`` on disk when
``PAYPROBE_SECRET_KEY`` is set, self-heals from legacy plaintext, and both
stores expose masked ``secret_refs()`` for the Secrets Vault inventory.
"""
import json
import os

os.environ.setdefault("DATABASE_URL", ":memory:")

from api.assist_store import AssistConfigDraft, AssistConfigStore
from api.crypto import SecretBox, fingerprint
from api.nats_server_store import NatsServerDraft, NatsServerStore


def _box() -> SecretBox:
    return SecretBox(SecretBox.generate_key())


# -- assist store: encrypted at rest -----------------------------------------

def test_assist_key_encrypted_at_rest_plaintext_in_memory(tmp_path):
    path = tmp_path / "assist.json"
    box = _box()
    s = AssistConfigStore(str(path), secret_box=box)
    s.save(AssistConfigDraft(enabled=True, api_key="sk-secret-1234"))

    on_disk = json.loads(path.read_text())
    assert on_disk["api_key"].startswith("enc:v1:")     # ciphertext on disk
    assert "sk-secret-1234" not in path.read_text()
    assert s.raw()["api_key"] == "sk-secret-1234"       # plaintext for runtime
    assert s.public()["key_hint"] == "…1234"            # reads stay masked
    assert "api_key" not in s.public()

    s2 = AssistConfigStore(str(path), secret_box=box)   # reload round-trip
    assert s2.raw()["api_key"] == "sk-secret-1234"


def test_assist_legacy_plaintext_file_self_heals_on_load(tmp_path):
    path = tmp_path / "assist.json"
    path.write_text(json.dumps({
        "provider": "openai", "enabled": True, "base_url": "", "model": "",
        "api_key": "sk-legacy-9999"}))
    s = AssistConfigStore(str(path), secret_box=_box())
    assert s.raw()["api_key"] == "sk-legacy-9999"       # still usable
    assert json.loads(path.read_text())["api_key"].startswith("enc:v1:")
    assert "sk-legacy-9999" not in path.read_text()     # healed at load


def test_assist_no_box_key_keeps_prior_plaintext_behaviour(tmp_path):
    path = tmp_path / "assist.json"
    s = AssistConfigStore(str(path), secret_box=SecretBox(""))  # disabled box
    s.save(AssistConfigDraft(api_key="sk-plain-0000"))
    assert json.loads(path.read_text())["api_key"] == "sk-plain-0000"
    s2 = AssistConfigStore(str(path), secret_box=SecretBox(""))
    assert s2.raw()["api_key"] == "sk-plain-0000"


def test_assist_clear_and_partial_update_still_work(tmp_path):
    path = tmp_path / "assist.json"
    box = _box()
    s = AssistConfigStore(str(path), secret_box=box)
    s.save(AssistConfigDraft(api_key="sk-secret-1234"))
    s.save(AssistConfigDraft(model="gpt-4o", api_key=None))  # None = keep
    assert s.raw()["api_key"] == "sk-secret-1234"
    s.save(AssistConfigDraft(api_key=""))                    # "" = clear
    assert s.raw()["api_key"] == "" and s.public()["key_set"] is False
    # on disk the cleared value is "" or a token decrypting to "" (encrypt_doc
    # wraps every secret-named str, empty included — same as the other stores)
    cleared = json.loads(path.read_text())["api_key"]
    assert cleared == "" or SecretBox.is_encrypted(cleared)
    s2 = AssistConfigStore(str(path), secret_box=box)        # reload: still cleared
    assert s2.raw()["api_key"] == "" and s2.public()["key_set"] is False


# -- vault inventory refs (masked) -------------------------------------------

def test_assist_secret_refs_masked():
    s = AssistConfigStore(":memory:")
    assert s.secret_refs() == []
    s.save(AssistConfigDraft(api_key="sk-abc-7777"))
    (ref,) = s.secret_refs()
    assert ref["field"] == "api_key"
    assert ref["fingerprint"] == fingerprint("sk-abc-7777")
    assert "sk-abc-7777" not in str(s.secret_refs())


def test_nats_secret_refs_masked_auth_fields_only():
    s = NatsServerStore(":memory:")
    s.upsert("payprobe-cluster", NatsServerDraft(
        name="c", servers=["nats://n:4222"],
        auth={"token": "T0K-SECRET", "user": "admin"}))
    refs = s.secret_refs()
    assert refs == [{"owner": "payprobe-cluster", "field": "auth.token",
                     "fingerprint": fingerprint("T0K-SECRET")}]
    assert "T0K-SECRET" not in str(refs)
