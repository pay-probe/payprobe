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


# -- why it failed, and what went over the wire ------------------------------


async def _slamming_host(after_ms: int = 10):
    """A host that reads one frame and hangs up ``after_ms`` later."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        seen.append(await reader.read(4096))
        await asyncio.sleep(after_ms / 1000)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


async def test_health_failure_reports_the_reason_bytes_and_close_timing():
    """The probe's frame, and the host's hang-up relative to it, come back in
    the result — no packet capture needed to see what the host rejected."""
    server, port, seen = await _slamming_host()
    try:
        result = await m.test_connection(
            m.TestConnectionRequest(config={
                "host": "127.0.0.1", "port": port, "protocol": "iso8583",
                "sign_on": {"enabled": False}, "response_timeout_sec": 2,
                "framing": {"length_prefix_bytes": 4, "length_encoding": "chardec"},
                "mhdr": "CDb4",
            })
        )
        assert result["ok"] is False
        assert result["error"].startswith("connected, but health check failed: ")
        assert "ms after the last send" in result["error"]
        health = result["health"]
        assert health["mode"] == "probe" and health["healthy"] is False
        assert bytes.fromhex(health["sent"]["hex"]) == seen[0]  # exactly what the host got
        assert seen[0][:8] == b"0069CDb4"
        dirs = [e["dir"] for e in result["wire"]]
        assert dirs == ["open", "tx", "close"]
        assert "0 of 4 expected bytes arrived" in result["wire"][-1]["note"]
    finally:
        server.close()
        await server.wait_closed()


async def test_connect_only_health_mode_sends_nothing():
    server, port, seen = await _slamming_host()
    try:
        result = await m.test_connection(
            m.TestConnectionRequest(config={
                "host": "127.0.0.1", "port": port, "protocol": "iso8583",
                "sign_on": {"enabled": False},
                "health_check": {"mode": "connect", "settle_ms": 50},
            })
        )
        assert result["ok"] is True and result["error"] is None
        assert result["health"]["sent"] is None
        assert [e["dir"] for e in result["wire"]] == ["open"]
        assert seen == []  # not a byte went out
    finally:
        server.close()
        await server.wait_closed()


def test_frame_preview_returns_the_exact_bytes_without_dialling():
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": {
                "adapter": "tcp", "protocol": "iso8583",
                "framing": {"length_prefix_bytes": 4, "length_encoding": "chardec"},
                "mhdr": "CDb4",
            },
            "action": "send_0800",
            "payload": {"values": {"70": "301", "11": "000001", "7": "1106123000",
                                   "12": "123000", "13": "1106"}},
        })
    assert r.status_code == 200, r.text
    body = r.json()
    frame = bytes.fromhex(body["hex"])
    assert frame[:8] == b"0069CDb4" and frame[8:12] == b"0800"
    assert body["total_bytes"] == 73 and body["declared_length"] == 69
    assert body["header_hex"] == "43446234" and body["prefix_hex"] == "30303639"
    assert body["framing"] == "prefix=4 ascii · header=43446234/4in (counted)"


def test_frame_preview_rejects_a_bad_framing_key_with_400():
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": {"protocol": "iso8583", "framing": {"length_encoding": "hex"}},
            "action": "send_0200", "payload": {"amount": 1},
        })
        assert r.status_code == 400 and "length_encoding" in r.json()["detail"]
        r = c.post("/connections/frame-preview", json={"config": {"adapter": "http"}})
        assert r.status_code == 400 and "TCP" in r.json()["detail"]
