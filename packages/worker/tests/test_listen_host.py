"""PAYPROBE_LISTEN_HOST / PAYPROBE_ADVERTISED_HOST: where a listener whose config
names no host binds, and the host the portal suggests dialing. Unset, both keep
the loopback default; a host named in a config always wins."""

import pytest

from payprobe_common.network import advertised_host, listen_host
from worker.adapters.http.responder import HttpResponder
from worker.adapters.tcp.responder import TcpResponder


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PAYPROBE_LISTEN_HOST", raising=False)
    monkeypatch.delenv("PAYPROBE_ADVERTISED_HOST", raising=False)


def test_both_default_to_loopback(monkeypatch):
    assert listen_host() == "127.0.0.1"
    assert advertised_host() == "127.0.0.1"
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "  ")
    monkeypatch.setenv("PAYPROBE_ADVERTISED_HOST", "")
    assert listen_host() == "127.0.0.1"
    assert advertised_host() == "127.0.0.1"


def test_both_follow_the_environment(monkeypatch):
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("PAYPROBE_ADVERTISED_HOST", "payprobe.example.test")
    assert listen_host() == "0.0.0.0"
    assert advertised_host() == "payprobe.example.test"


def _bound_host(responder) -> str:
    if isinstance(responder, TcpResponder):
        return responder._server.sockets[0].getsockname()[0]
    return responder._site._server.sockets[0].getsockname()[0]


@pytest.mark.parametrize("cls", [TcpResponder, HttpResponder])
async def test_listener_without_a_host_binds_loopback_by_default(cls):
    r = cls({"port": 0})
    await r.start()
    try:
        assert _bound_host(r) == "127.0.0.1"
    finally:
        await r.stop()


@pytest.mark.parametrize("cls", [TcpResponder, HttpResponder])
async def test_listener_without_a_host_binds_the_listen_host(cls, monkeypatch):
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
    r = cls({"port": 0})
    await r.start()
    try:
        assert _bound_host(r) == "0.0.0.0"
    finally:
        await r.stop()


@pytest.mark.parametrize("cls", [TcpResponder, HttpResponder])
async def test_a_host_named_in_the_config_wins(cls, monkeypatch):
    monkeypatch.setenv("PAYPROBE_LISTEN_HOST", "0.0.0.0")
    r = cls({"host": "127.0.0.1", "port": 0})
    await r.start()
    try:
        assert _bound_host(r) == "127.0.0.1"
    finally:
        await r.stop()
