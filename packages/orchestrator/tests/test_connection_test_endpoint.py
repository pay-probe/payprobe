"""POST /connections/test — probe a connection's reachability/health."""
import asyncio

import pytest

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


# -- frame preview: a step payload against its dialect ----------------------

POS_CONFIG = {
    "adapter": "tcp", "protocol": "iso8583", "host": "172.16.90.11", "port": 52000,
    "mode": "outbound",
    "framing": {"length_prefix_bytes": 4, "length_byte_order": "big",
                "length_encoding": "ascii", "encoding": "ascii"},
    "health_check": {"mode": "connect"}, "sign_on": {"enabled": False},
}
POS_0200 = {"mti": "0200", "values": {
    "2": "5488350005706204", "3": "000000", "4": "000000000200", "7": "0303083731",
    "11": "000005", "12": "260303083731", "22": "020", "24": "200", "25": "00",
    "41": "POS20001", "42": "GCPOS13        ", "49": "981"}}


@pytest.fixture()
def pos_format(monkeypatch):
    """A fake scenario-service holding the POS host dialect — the 1987 table with
    DE 24 (NII, n 3) added and DE 12 widened to YYMMDDhhmmss — in two copies:
    ``iso8583-1987-ascii-copy-1`` (all ASCII, as the registry copy is today) and
    ``pos-binary-bitmap`` (the same with ``bitmap_encoding: binary``)."""
    fields = {**iso8583.DEFAULT_FIELDS,
              "12": {"name": "Local date & time", "len_type": "fixed", "length": 12, "type": "n"},
              "24": {"name": "NII", "len_type": "fixed", "length": 3, "type": "n"}}
    formats = {
        "iso8583-1987-ascii-copy-1": {"id": "iso8583-1987-ascii-copy-1", "protocol": "iso8583",
                                      "definition": {"encoding": "ascii", "fields": fields}},
        "pos-binary-bitmap": {"id": "pos-binary-bitmap", "protocol": "iso8583",
                               "definition": {"encoding": "ascii", "bitmap_encoding": "binary",
                                              "fields": fields}},
    }

    async def fake_get(url):
        fid = url.rsplit("/formats/", 1)[-1] if "/formats/" in url else None
        if fid in formats:
            return formats[fid]
        raise RuntimeError(f"404 {url}")

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    monkeypatch.setattr(m, "_ISO8583_FORMAT_ENCODING", True)
    return formats


#: The 0200 above, byte for byte, under the ASCII dialect (DE 13 is not
#: stamped: auto_fields is off in the preview so the frame is reproducible).
POS_0200_BODY_ASCII = (
    b"0200" b"7230058000C08000"  # no DE 35 / 48 in this set: bytes 5-6 = 00 C0
    b"165488350005706204" b"000000" b"000000000200" b"0303083731" b"000005"
    b"260303083731" b"020" b"200" b"00" b"POS20001" b"GCPOS13        " b"981"
)


async def test_frame_preview_of_the_0200_payload_against_the_pos_format(pos_format):
    """The reproduced request, exactly as sent, plus ``message_format_id``."""
    out = await m.frame_preview(m.FramePreviewRequest(
        config={**POS_CONFIG, "auto_fields": False},
        action="send_message", payload=POS_0200,
        message_format_id="iso8583-1987-ascii-copy-1"))
    body = bytes.fromhex(out["body_hex"])
    assert body == POS_0200_BODY_ASCII and len(body) == 118
    assert out["prefix_hex"] == "30313138" and out["declared_length"] == 118  # "0118"
    assert out["total_bytes"] == 122
    assert out["hex"] == (
        "30313138"  # 4-digit ASCII length
        "30323030" "37323330303538303030433038303030"  # MTI + ASCII bitmap
        "3136" "35343838333530303035373036323034"  # DE 2
        "303030303030" "303030303030303030323030" "30333033303833373331" "303030303035"
        "323630333033303833373331" "303230" "323030" "3030"
        "504f533230303031" "4743504f5331332020202020202020" "393831"
    )
    assert out["request"]["values"]["24"] == "200"
    # the same request through the HTTP surface, with the id in the payload
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": {**POS_CONFIG, "auto_fields": False}, "action": "send_message",
            "payload": {**POS_0200, "message_format_id": "iso8583-1987-ascii-copy-1"}})
    assert r.status_code == 200, r.text
    assert r.json()["hex"] == out["hex"]


async def test_frame_preview_with_a_binary_bitmap_format(pos_format):
    """With ``bitmap_encoding: binary`` on the format the bitmap goes out as 8
    raw bytes — once the connection's legacy ``framing.encoding`` text codec is
    out of the way (ADR-0011 refuses a config whose own encoding disagrees
    with the bound format's rather than silently picking one)."""
    cfg = {**POS_CONFIG, "auto_fields": False,
           "framing": {k: v for k, v in POS_CONFIG["framing"].items() if k != "encoding"}}
    out = await m.frame_preview(m.FramePreviewRequest(
        config=cfg, action="send_message", payload=POS_0200,
        message_format_id="pos-binary-bitmap"))
    body = bytes.fromhex(out["body_hex"])
    assert body[:4] == b"0200" and body[4:12] == bytes.fromhex("7230058000C08000")
    assert body[12:] == POS_0200_BODY_ASCII[20:]  # everything after the bitmap unchanged
    assert out["declared_length"] == 110 and out["prefix_hex"] == "30313130"
    # the reproduced config still carries framing.encoding: "ascii" -> refused, named
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": POS_CONFIG, "action": "send_message", "payload": POS_0200,
            "message_format_id": "pos-binary-bitmap"})
    assert r.status_code == 400
    assert "declares encoding" in r.json()["detail"] and "keep one" in r.json()["detail"]


def test_frame_preview_without_the_format_is_a_400_naming_the_de(pos_format):
    """The reproduced 500: DE 24 is not in the stock 1987 table."""
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": POS_CONFIG, "action": "send_message", "payload": POS_0200})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "DE24 not defined in the field table" in detail
    assert "DE12 expected 6 chars, got 12" in detail  # the 1987 DE 12 is hhmmss
    assert "pass message_format_id" in detail


def test_frame_preview_reports_length_and_charset_errors_with_the_format(pos_format):
    bad = {"mti": "0200", "values": {**POS_0200["values"], "4": "00000000020", "49": "98A"}}
    bad["values"]["4"] = "0000000002A"  # 11 chars: _coerce zero-fills only digits
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": POS_CONFIG, "action": "send_message", "payload": bad,
            "message_format_id": "iso8583-1987-ascii-copy-1"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "DE4 expected 12 chars, got 11" in detail
    assert "DE49 must be numeric" in detail
    assert detail.endswith("(format 'iso8583-1987-ascii-copy-1')")


def test_frame_preview_unknown_format_is_a_404(pos_format):
    with _client() as c:
        r = c.post("/connections/frame-preview", json={
            "config": POS_CONFIG, "action": "send_message", "payload": POS_0200,
            "message_format_id": "nope"})
    assert r.status_code == 404 and "no message format 'nope'" in r.json()["detail"]
