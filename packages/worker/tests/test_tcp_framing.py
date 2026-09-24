"""ASCII-digit length prefixes (``framing.length_encoding = "ascii"``).

Some host links frame every message with a zero-padded decimal length (e.g. 6
digits: ``b"000043"``). These tests pin the codec, the exact bytes the adapter
puts on the wire, and an adapter <-> responder round trip over that framing.
"""

import asyncio

import pytest

from worker.adapters.tcp import framing, iso8583
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.chaos import ChaosEngine
from worker.adapters.tcp.responder import TcpResponder

ASCII6 = {"length_prefix_bytes": 6, "length_encoding": "ascii"}


# -- codec --------------------------------------------------------------------


def test_length_encoding_defaults_to_binary_and_validates():
    assert framing.length_encoding({}) == "binary"
    assert framing.length_encoding({"length_encoding": "ASCII"}) == "ascii"
    with pytest.raises(ValueError):
        framing.length_encoding({"length_encoding": "bcd"})


def test_ascii_prefix_is_zero_padded_digits():
    assert framing.encode_length(43, 6, "big", "ascii") == b"000043"
    assert framing.decode_length(b"001848", "big", "ascii") == 1848


def test_binary_prefix_unchanged():
    assert framing.encode_length(43, 2, "big", "binary") == b"\x00\x2b"
    assert framing.encode_length(43, 2, "little", "binary") == b"\x2b\x00"
    assert framing.decode_length(b"\x00\x2b", "big", "binary") == 43


def test_ascii_prefix_rejects_overflow_and_non_digits():
    with pytest.raises(ValueError):
        framing.encode_length(1000, 3, "big", "ascii")
    with pytest.raises(ValueError):
        framing.decode_length(b"\x00\x00\x00\x00\x00A", "big", "ascii")


def test_chaos_bad_length_keeps_ascii_prefix_numeric():
    body = b"0810822000000000000004000000000000000001301"
    frame = f"{len(body):06d}".encode() + body
    out = ChaosEngine().corrupt(frame, "bad_length", prefix_bytes=6, length_encoding="ascii")
    assert out[6:] == frame[6:]
    assert out[:6].isdigit() and int(out[:6]) > len(body)


# -- adapter on the wire ------------------------------------------------------


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


async def test_adapter_writes_and_reads_ascii_length_prefix():
    host = RawAsciiHost()
    port = await host.start()
    adapter = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": ASCII6,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 3,
        }
    )
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
    adapter = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": {**ASCII6, "length_includes_prefix": True},
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 3,
        }
    )
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 250})
        assert res.success, res.error
        assert host.prefixes[0] == f"{len(host.bodies[0]) + 6:06d}".encode()
    finally:
        await adapter.disconnect()
        await host.stop()


async def test_five_digit_ascii_prefix():
    """A 5-digit ASCII prefix works the same way."""
    host = RawAsciiHost(width=5)
    port = await host.start()
    adapter = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": {"length_prefix_bytes": 5, "length_encoding": "ascii"},
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 3,
        }
    )
    await adapter.connect()
    try:
        res = await adapter.execute("send_0100", {"amount": 99})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0110"
        assert host.prefixes[0] == f"{len(host.bodies[0]):05d}".encode()
    finally:
        await adapter.disconnect()
        await host.stop()


# -- adapter <-> responder ----------------------------------------------------


async def test_responder_and_adapter_round_trip_over_ascii_framing():
    responder = TcpResponder(
        {
            "protocol": "iso8583",
            "framing": ASCII6,
            "default": {"echo": ["11"], "set": {"39": "00"}},
        }
    )
    port = await responder.start()
    adapter = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "framing": ASCII6,
            "sign_on": {"enabled": False},
            "reconnect": {"enabled": False},
            "response_timeout_sec": 2,
        }
    )
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
    finally:
        await adapter.disconnect()
        await responder.stop()
