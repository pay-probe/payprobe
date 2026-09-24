"""The one length-prefix encode/decode every TCP piece shares (adapter,
responder, proxy, chaos): binary integers and zero-padded ASCII digits."""

import asyncio

import pytest

from worker.adapters.tcp import framing, iso8583
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.chaos import ChaosEngine
from worker.adapters.tcp.responder import TcpResponder


@pytest.mark.parametrize(
    "value,width,order,enc,expected",
    [
        (43, 2, "big", "binary", b"\x00\x2b"),
        (43, 2, "little", "binary", b"\x2b\x00"),
        (43, 4, "big", "ascii", b"0043"),
        (43, 6, "big", "ascii", b"000043"),
        (0, 2, "big", "ascii", b"00"),
    ],
)
def test_encode_and_decode_round_trip(value, width, order, enc, expected):
    prefix = framing.encode_length(value, width, order, enc)
    assert prefix == expected and len(prefix) == width
    assert framing.decode_length(prefix, order, enc) == value


def test_ascii_byte_order_is_irrelevant():
    assert framing.encode_length(7, 3, "little", "ascii") == b"007"
    assert framing.decode_length(b"007", "little", "ascii") == 7


def test_capacity_and_overflow():
    assert framing.length_capacity(2, "binary") == 65535
    assert framing.length_capacity(2, "ascii") == 99
    assert framing.length_capacity(4, "ascii") == 9999
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(100, 2, "big", "ascii")
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(65536, 2, "big", "binary")


def test_non_digit_ascii_prefix_names_the_mismatch():
    with pytest.raises(ValueError, match="framing.length_encoding"):
        framing.decode_length(b"\x00\x2b", "big", "ascii")


def test_normalise_defaults_to_binary_and_refuses_unknown():
    assert framing.normalise_length_encoding(None) == "binary"
    assert framing.normalise_length_encoding("ASCII") == "ascii"
    with pytest.raises(ValueError):
        framing.normalise_length_encoding("bcd")


def test_chaos_bad_length_keeps_the_ascii_prefix_digits():
    frame = b"0012" + b"0210AUTH00ABCD"
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=4, length_encoding="ascii")
    assert out[4:] == frame[4:]
    assert out[:4].isdigit() and int(out[:4]) > 12  # still digits, still inflated


def test_chaos_bad_length_near_capacity_still_inflates():
    frame = b"95" + b"x" * 95
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=2, length_encoding="ascii")
    assert out[:2] == b"99"


# -- on the wire, against a host that frames with digits (from PR #3) ----------

ASCII6 = {"length_prefix_bytes": 6, "length_encoding": "ascii"}


class RawAsciiHost:
    """Minimal host framing with a ``width``-digit ASCII length (excluding itself
    unless ``includes_prefix``). Records every raw prefix it reads and answers
    MTI+10 echoing DE 11 in the same framing."""

    def __init__(self, width: int = 6, includes_prefix: bool = False):
        self.width = width
        self.extra = width if includes_prefix else 0
        self.prefixes: list[bytes] = []
        self.bodies: list[str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                prefix = await reader.readexactly(self.width)
                self.prefixes.append(prefix)
                body = (await reader.readexactly(int(prefix) - self.extra)).decode("ascii")
                self.bodies.append(body)
                req = iso8583.iso_unpack(body, iso8583.DEFAULT_FIELDS)
                mti = f"{int(req['mti']) + 10:04d}"
                stan = req["fields"]["11"]["value"]
                resp = iso8583.iso_pack(mti, {"11": stan, "39": "00"}, iso8583.DEFAULT_FIELDS)
                length = str(len(resp) + self.extra).zfill(self.width)
                writer.write(length.encode() + resp.encode("ascii"))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()


def _adapter(port: int, framing_cfg: dict, timeout: float = 3) -> TcpAdapter:
    return TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": framing_cfg,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": timeout,
        }
    )


async def test_adapter_writes_and_reads_ascii_length_prefix():
    host = RawAsciiHost()
    port = await host.start()
    adapter = _adapter(port, ASCII6)
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 250})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        # the prefix on the wire is 6 decimal digits equal to the body length
        assert host.prefixes[0] == f"{len(host.bodies[0]):06d}".encode()
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_adapter_ascii_prefix_counting_itself():
    """length_includes_prefix still applies: the digits then include their own 6."""
    host = RawAsciiHost(includes_prefix=True)
    port = await host.start()
    adapter = _adapter(port, {**ASCII6, "length_includes_prefix": True})
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 250})
        assert res.success, res.error
        assert host.prefixes[0] == f"{len(host.bodies[0]) + 6:06d}".encode()
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_five_digit_ascii_prefix():
    host = RawAsciiHost(width=5)
    port = await host.start()
    adapter = _adapter(port, {"length_prefix_bytes": 5, "length_encoding": "ascii"})
    await adapter.connect()
    try:
        res = await adapter.execute("send_0100", {"amount": 99})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0110"
        assert host.prefixes[0] == f"{len(host.bodies[0]):05d}".encode()
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_responder_and_adapter_round_trip_over_ascii_framing():
    responder = TcpResponder(
        {
            "protocol": "iso8583",
            "framing": ASCII6,
            "default": {"echo": ["11"], "set": {"39": "00"}},
        }
    )
    port = await responder.start()
    adapter = _adapter(port, ASCII6, timeout=2)
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await responder.stop()
