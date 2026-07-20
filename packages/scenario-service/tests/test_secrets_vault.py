"""Secrets Vault: masked secret_refs + the /secrets aggregator."""
import os

os.environ["DATABASE_URL"] = ":memory:"  # in-memory store for the lifespan

from fastapi.testclient import TestClient

from api.connection_store import ConnectionStore, ConnectionDraft
from api.crypto import fingerprint
from api.main import app
from api.variable_store import VariableStore


# -- store-level secret_refs (masking) ---------------------------------------

def test_connection_secret_refs_mask_values():
    s = ConnectionStore(":memory:")
    s.upsert("switch", ConnectionDraft(host="h", port=1, password="SECRET123",
                                       description="x"))
    refs = s.secret_refs()
    assert refs == [{"owner": "switch", "field": "password",
                     "fingerprint": fingerprint("SECRET123")}]
    # no raw value leaks
    assert all("SECRET123" not in str(v) for r in refs for v in r.values())


def test_connection_secret_refs_skip_non_secret_fields():
    s = ConnectionStore(":memory:")
    s.upsert("c", ConnectionDraft(host="h", port=1))  # host/port are not secret
    assert s.secret_refs() == []


def test_variable_secret_refs_across_scopes():
    vs = VariableStore(":memory:")
    vs.set_global({"api_token": "T", "plain": "P"}, secrets=["api_token"])
    vs.set_project("proj1", {"db_pw": "D"}, secrets=["db_pw"])
    refs = vs.secret_refs()
    owners = {(r["scope"], r["scope_id"], r["name"]) for r in refs}
    assert ("global", "", "api_token") in owners
    assert ("project", "proj1", "db_pw") in owners
    # the non-secret global var is not listed
    assert all(r["name"] != "plain" for r in refs)
    # fingerprints, not values
    assert all("T" != r["fingerprint"] and "D" != r["fingerprint"] for r in refs)


# -- /secrets aggregator -----------------------------------------------------

def test_secrets_endpoint_aggregates_and_masks():
    with TestClient(app) as c:
        c.put("/connections/vault_conn",
              json={"host": "h", "port": 1, "password": "p@ss"})
        c.put("/variables/global",
              json={"variables": {"vault_tok": "S3cretValue"}, "secrets": ["vault_tok"]})
        c.put("/test-data/keys/vault_key",
              json={"type": "bdk", "value": "0123456789ABCDEF"})

        body = c.get("/secrets").json()

    assert isinstance(body["status"]["encrypted_at_rest"], bool)
    entries = body["entries"]
    by = {(e["source"], e["owner"], e["field"]) for e in entries}
    assert ("connection", "vault_conn", "password") in by
    assert ("variable", "global", "vault_tok") in by
    assert ("key", "vault_key", "(material)") in by
    # never reveal: no plaintext anywhere in the response
    blob = str(body)
    for secret in ("p@ss", "S3cretValue", "0123456789ABCDEF"):
        assert secret not in blob
    # every entry carries a fingerprint + encrypted flag
    assert all("fingerprint" in e and "encrypted" in e for e in entries)


def test_secrets_endpoint_lists_assist_llm_key_and_nats_auth():
    with TestClient(app) as c:
        c.put("/assist/config", json={"enabled": True, "api_key": "sk-vault-4321"})
        c.put("/nats-servers/vault-nats",
              json={"name": "vault-nats", "servers": ["nats://n:4222"],
                    "auth": {"token": "NATS-T0K"}})
        body = c.get("/secrets").json()

    by = {(e["source"], e["field"]) for e in body["entries"]}
    assert ("assistant", "api_key") in by
    assert ("nats", "auth.token") in by
    # never reveal: no plaintext anywhere in the response
    blob = str(body)
    assert "sk-vault-4321" not in blob and "NATS-T0K" not in blob
