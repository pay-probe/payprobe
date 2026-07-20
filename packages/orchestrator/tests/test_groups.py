"""Orchestrator: a step targeting a participant group is inlined into the run env."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


async def test_attach_groups_inlines_member_configs(monkeypatch):
    groups = [{
        "id": "issuers", "name": "issuers",
        "members": [{"connection": "a", "weight": 1}, {"connection": "b", "weight": 2}],
        "selection": {"policy": "weighted"},
    }]
    conns = {
        "a": {"name": "a", "adapter": "tcp", "host": "10.0.0.1", "port": 7001,
              "environment_overrides": {"prod": {"host": "x"}}},
        "b": {"name": "b", "adapter": "tcp", "host": "10.0.0.2", "port": 7002},
    }

    async def fake_get(url):
        if url.endswith("/participant-groups"):
            return groups
        if url.endswith("/connections/a"):
            return conns["a"]
        if url.endswith("/connections/b"):
            return conns["b"]
        raise AssertionError(url)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    env = {"adapters": {}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "action", "target": "tcp_iso8583", "action": "send_0200",
         "config": {"connection": "issuers"}}]}]

    await m._attach_groups(env, scs)

    grp = env["adapters"]["issuers"]
    assert grp["adapter"] == "group"
    assert grp["selection"]["policy"] == "weighted"
    assert [mem["connection"] for mem in grp["members"]] == ["a", "b"]
    assert grp["members"][0]["config"]["host"] == "10.0.0.1"
    assert grp["members"][1]["weight"] == 2
    # member configs are stripped of metadata
    assert "name" not in grp["members"][0]["config"]
    assert "environment_overrides" not in grp["members"][0]["config"]
    # the step is pointed at the group
    assert scs[0]["steps"][0]["target"] == "issuers"


async def test_attach_groups_noop_without_group_reference(monkeypatch):
    async def fake_get(url):
        if url.endswith("/participant-groups"):
            return []
        raise AssertionError(url)

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    env = {"adapters": {}}
    scs = [{"id": "sc", "steps": [
        {"id": "s1", "kind": "action", "target": "tcp_iso8583",
         "config": {"connection": "switch_visa"}}]}]
    await m._attach_groups(env, scs)
    assert env["adapters"] == {}  # not a group -> nothing inlined
