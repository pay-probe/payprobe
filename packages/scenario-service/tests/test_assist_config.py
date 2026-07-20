"""AI-assistant LLM config store + endpoints (key masked, never returned)."""
import os

os.environ["DATABASE_URL"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

import api.main as m  # noqa: E402
from api.assist_store import AssistConfigStore, AssistConfigDraft  # noqa: E402


def test_store_masks_key_and_keeps_on_partial_update():
    s = AssistConfigStore(":memory:")
    assert s.public()["key_set"] is False

    s.save(AssistConfigDraft(enabled=True, model="gpt-4o", api_key="sk-secret-1234"))
    pub = s.public()
    assert pub["enabled"] and pub["model"] == "gpt-4o"
    assert pub["key_set"] is True and pub["key_hint"] == "…1234"
    assert "api_key" not in pub                     # never exposed
    assert s.raw()["api_key"] == "sk-secret-1234"   # but stored for server use

    # api_key=None leaves the stored key unchanged
    s.save(AssistConfigDraft(enabled=False, model="gpt-4o-mini", api_key=None))
    assert s.raw()["api_key"] == "sk-secret-1234"
    # api_key="" clears it
    s.save(AssistConfigDraft(enabled=False, api_key=""))
    assert s.raw()["api_key"] == "" and s.public()["key_set"] is False


def test_config_endpoints_round_trip():
    m.app.state.__dict__.setdefault("assist", AssistConfigStore(":memory:"))
    m.app.state.assist = AssistConfigStore(":memory:")
    with TestClient(m.app) as c:
        assert c.get("/assist/config").json()["key_set"] is False
        r = c.put("/assist/config", json={
            "enabled": True, "model": "gpt-4o", "base_url": "https://x/y",
            "api_key": "sk-abcd1234"})
        assert r.status_code == 200 and r.json()["key_hint"] == "…1234"
        # GET never returns the raw key
        got = c.get("/assist/config").json()
        assert got["enabled"] and got["model"] == "gpt-4o" and "api_key" not in got
        # update without api_key keeps it
        c.put("/assist/config", json={"enabled": False, "model": "gpt-4o-mini"})
        assert m.app.state.assist.raw()["api_key"] == "sk-abcd1234"
