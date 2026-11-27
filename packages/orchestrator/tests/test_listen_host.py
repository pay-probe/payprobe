"""PAYPROBE_LISTEN_HOST decides where a participant listener binds when its
inbound connection names no host; PAYPROBE_ADVERTISED_HOST is the dial host the
portal pre-fills, read from /system. Unset, both keep the loopback default."""
import os

import pytest
from fastapi.testclient import TestClient

os.environ["DISABLE_SCHEDULER"] = "1"

from orchestrator.api import main as m  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PAYPROBE_LISTEN_HOST", raising=False)
    monkeypatch.delenv("PAYPROBE_ADVERTISED_HOST", raising=False)


def _connection(monkeypatch, doc):
    async def fake_get(url):
        return doc

    monkeypatch.setattr(m, "_http_get_json", fake_get)


FLOW = {"trigger": {"connection": "cn_in"}}


async def test_listener_defaults_to_loopback(monkeypatch):
    _connection(monkeypatch, {"name": "cn_in", "adapter": "tcp", "port": 9001})
    assert (await m._participant_listen_config(FLOW, None))["host"] == "127.0.0.1"
    assert (await m._participant_listen_config({}, None))["host"] == "127.0.0.1"
    assert await m._flow_listen_target(FLOW) == ("127.0.0.1", 9001)


async def test_listener_without_a_host_binds_the_listen_host(monkeypatch):
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
    _connection(monkeypatch, {"name": "cn_in", "adapter": "tcp", "port": 9001})
    assert (await m._participant_listen_config(FLOW, None))["host"] == "0.0.0.0"
    assert (await m._participant_listen_config({}, None))["host"] == "0.0.0.0"
    assert await m._flow_listen_target(FLOW) == ("0.0.0.0", 9001)
    assert await m._flow_listen_target({}) == ("0.0.0.0", None)


async def test_a_host_named_in_the_connection_wins(monkeypatch):
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
    _connection(monkeypatch, {"name": "cn_in", "adapter": "tcp",
                              "host": "10.0.0.7", "port": 9001})
    assert (await m._participant_listen_config(FLOW, None))["host"] == "10.0.0.7"
    assert await m._flow_listen_target(FLOW) == ("10.0.0.7", 9001)


def test_system_reports_listen_and_advertised_hosts(monkeypatch):
    with TestClient(m.app) as client:
        assert client.get("/system").json()["network"] == {
            "listen_host": "127.0.0.1", "advertised_host": "127.0.0.1"}
        monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
        monkeypatch.setenv("PAYPROBE_ADVERTISED_HOST", "payprobe.example.test")
        assert client.get("/system").json()["network"] == {
            "listen_host": "0.0.0.0", "advertised_host": "payprobe.example.test"}
