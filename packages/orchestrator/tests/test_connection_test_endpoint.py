"""POST /connections/test — probe a connection's reachability/health."""
import asyncio

from fastapi.testclient import TestClient

from orchestrator.api import main as m
from worker.adapters.tcp import iso8583


def _client():
    return TestClient(m.app)


def test_no_host_short_circuits():
    with _client() as c:
        r = c.post("/connections/test", json={"config": {"protocol": "iso8583"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "host/port" in body["error"]


def test_grpc_without_target_short_circuits():
    with _client() as c:
        r = c.post("/connections/test", json={"config": {"adapter": "grpc"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "target" in body["error"]


def test_unreachable_host_reports_failure_fast():
    with _client() as c:
        r = c.post("/connections/test", json={"config": {
            "host": "127.0.0.1", "port": 1,  # nothing listening
            "protocol": "iso8583", "connect_timeout_sec": 1,
            "sign_on": {"enabled": False},
        }})
    body = r.json()
    assert body["ok"] is False
    assert body["error"]  # a connection error message


def test_unknown_adapter_reports_error():
    with _client() as c:
        r = c.post("/connections/test", json={"config": {"adapter": "carrierpigeon"}})
    body = r.json()
    assert body["ok"] is False and "carrierpigeon" in body["error"]


def test_rest_adapter_does_not_require_host_port():
    """A REST connection (base_url, no host/port) must NOT short-circuit on the
    host/port check — it probes the URL and reports the connect result."""
    with _client() as c:
        r = c.post("/connections/test", json={"config": {
            "adapter": "http",
            "base_url": "http://127.0.0.1:1",  # nothing listening -> connect fails
            "connect_timeout_sec": 1,
        }})
    body = r.json()
    assert r.status_code == 200
    # the failure is a real probe result, not the "no host/port configured" guard
    assert body["ok"] is False
    assert "host/port" not in (body["error"] or "")


def test_payshield_still_requires_host_port():
    with _client() as c:
        r = c.post("/connections/test", json={"config": {"adapter": "payshield"}})
    body = r.json()
    assert body["ok"] is False and "host/port" in body["error"]


async def test_healthy_connection_reports_ok():
    """A live ISO 8583 echo server -> ok:true with a latency."""
    async def handle(reader, writer):
        try:
            while True:
                prefix = await reader.readexactly(2)
                n = int.from_bytes(prefix, "big")
                body = (await reader.readexactly(n)).decode("ascii")
                parsed = iso8583.iso_unpack(body, iso8583.DEFAULT_FIELDS)
                mti = "0810" if parsed["mti"] == "0800" else "0210"
                de = {k: v["value"] for k, v in parsed["fields"].items()}
                resp = iso8583.iso_pack(
                    mti, {"11": de.get("11", "000000"), "39": "00"},
                    iso8583.DEFAULT_FIELDS).encode("ascii")
                writer.write(len(resp).to_bytes(2, "big") + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # call the endpoint function directly (avoids nested event loops)
        result = await m.test_connection(
            m.TestConnectionRequest(config={
                "host": "127.0.0.1", "port": port, "protocol": "iso8583",
                "sign_on": {"enabled": True},
            })
        )
        assert result["ok"] is True
        assert result["latency_ms"] >= 0
    finally:
        server.close()
        await server.wait_closed()
