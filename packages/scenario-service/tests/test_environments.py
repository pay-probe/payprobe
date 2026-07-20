"""EnvironmentStore: CRUD, slug, secret-at-rest, seeding."""
import json

from api.crypto import SecretBox
from api.environment_store import EnvironmentStore, EnvironmentDraft


def _draft(**kw):
    return EnvironmentDraft(**kw)


def test_upsert_get_list_and_slug():
    s = EnvironmentStore(":memory:")
    s.upsert("Staging EU", _draft(description="eu", adapters={"http": {"base_url": "h"}}))
    names = s.names()
    assert names == ["staging-eu"]  # slugged
    env = s.get("staging-eu")
    assert env.key == "staging-eu"
    assert env.adapters["http"]["base_url"] == "h"
    assert env.description == "eu"


def test_permissive_keeps_arbitrary_config():
    s = EnvironmentStore(":memory:")
    s.upsert("prod", _draft(adapters={"switch": {"host": "h", "port": 7000}},
                            connection_budget={"total": 50}, mode="real"))
    cfg = s.get_config("prod")
    assert cfg["connection_budget"] == {"total": 50}
    assert cfg["mode"] == "real"
    assert cfg["adapters"]["switch"]["port"] == 7000


def test_delete():
    s = EnvironmentStore(":memory:")
    s.upsert("x", _draft())
    s.delete("x")
    assert s.get("x") is None


def test_secret_fields_encrypted_at_rest(tmp_path):
    path = tmp_path / "environments.json"
    box = SecretBox(SecretBox.generate_key())
    s = EnvironmentStore(str(path), secret_box=box)
    s.upsert("prod", _draft(adapters={"http": {"base_url": "h", "api_key": "SH-SECRET"}}))
    on_disk = json.loads(path.read_text())
    stored = on_disk["environments"]["prod"]["adapters"]["http"]
    assert stored["api_key"].startswith("enc:v1:") and "SH-SECRET" not in stored["api_key"]
    assert stored["base_url"] == "h"  # non-secret stays readable
    # reload decrypts back to plaintext for run assembly
    s2 = EnvironmentStore(str(path), secret_box=box)
    assert s2.get_config("prod")["adapters"]["http"]["api_key"] == "SH-SECRET"


def test_seed_from_dir_imports_when_empty(tmp_path):
    seed = tmp_path / "environments"
    seed.mkdir()
    (seed / "mock.json").write_text(json.dumps(
        {"name": "mock", "description": "Mock env", "adapters": {"http": {"mock": True}}}))
    (seed / "grpc.json").write_text(json.dumps({"name": "grpc", "adapters": {}}))
    s = EnvironmentStore(":memory:")
    n = s.seed_from_dir(str(seed))
    assert n == 2 and set(s.names()) == {"mock", "grpc"}
    # idempotent: a second seed is a no-op once populated
    assert s.seed_from_dir(str(seed)) == 0
