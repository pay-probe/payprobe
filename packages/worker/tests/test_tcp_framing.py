"""The one length-prefix encode/decode every TCP piece shares (adapter,
responder, proxy, chaos): binary integers, zero-padded ASCII digits and, since
ADR-0011, packed-BCD digits."""

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
        (43, 2, "big", "bcd", b"\x00\x43"),
        (1234, 2, "little", "bcd", b"\x12\x34"),
        (7, 1, "big", "bcd", b"\x07"),
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
    assert framing.length_capacity(2, "bcd") == 9999
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(10000, 2, "big", "bcd")
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(100, 2, "big", "ascii")
    with pytest.raises(ValueError, match="does not fit"):
        framing.encode_length(65536, 2, "big", "binary")


def test_non_digit_ascii_prefix_names_the_mismatch():
    with pytest.raises(ValueError, match="framing.length_encoding"):
        framing.decode_length(b"\x00\x2b", "big", "ascii")


def test_non_decimal_bcd_nibble_names_the_mismatch():
    with pytest.raises(ValueError, match="framing.length_encoding"):
        framing.decode_length(b"\x00\x2b", "big", "bcd")


def test_normalise_defaults_to_binary_and_refuses_unknown():
    assert framing.normalise_length_encoding(None) == "binary"
    assert framing.normalise_length_encoding("ASCII") == "ascii"
    assert framing.normalise_length_encoding("bcd") == "bcd"
    assert framing.normalise_length_encoding("hex") == "hex"
    assert framing.normalise_length_encoding("CHARHEX") == "hex"
    with pytest.raises(ValueError):
        framing.normalise_length_encoding("octal")


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


# -- FrameSpec: the one framing model, exact bytes per configuration ----------
#
# The reference frame throughout is the one a vendor POS-host client builds
# against a POS host (vendor settings "standard header GENERIC4, header type
# CHARDEC, bytes length 4, byte order Big"): a 4-character ASCII decimal
# length equal to the payload length, followed by the payload. For a 16-byte
# payload 0x00..0x0F that is ``"0016"`` + 16 bytes = 20 bytes on the wire.

PAYLOAD_00_0F = bytes(range(16))
ISO_BODY = b"0800" + b"8238000000000000"  # a stand-in body: 20 ASCII bytes
MHDR = "CDb4"  # 43 44 62 34


def _spec(**framing_cfg) -> framing.FrameSpec:
    return framing.FrameSpec.from_config({"framing": framing_cfg})


def test_client_frame_0016_plus_16_payload_bytes_is_20_bytes():
    """The vendor client's test frame: ``0016`` + 0x00..0x0F."""
    spec = _spec(length_prefix_bytes=4, length_encoding="ascii")
    frame = spec.frame(PAYLOAD_00_0F)
    assert frame == b"0016" + PAYLOAD_00_0F
    assert frame.hex() == "30303136" + "000102030405060708090a0b0c0d0e0f"
    assert len(frame) == 4 + 16 == 20
    assert spec.declared_length(PAYLOAD_00_0F) == 16  # payload only, prefix not counted


def test_chardec_is_the_vendor_name_for_an_ascii_digit_prefix():
    """``CHARDEC`` (characters, decimal) folds to ``ascii`` and frames identically."""
    chardec = _spec(length_prefix_bytes=4, length_encoding="chardec")
    ascii_ = _spec(length_prefix_bytes=4, length_encoding="ascii")
    assert chardec.length_encoding == "ascii"
    assert chardec.frame(PAYLOAD_00_0F) == ascii_.frame(PAYLOAD_00_0F) == b"0016" + PAYLOAD_00_0F
    assert framing.normalise_length_encoding("CHARDEC") == "ascii"


@pytest.mark.parametrize(
    "name,cfg,expected_hex",
    [
        # 2-byte binary big-endian MLI, the ISO 8583 norm (default config)
        ("2-byte binary MLI", {}, "0014" + ISO_BODY.hex()),
        # 2-byte binary little-endian
        ("2-byte binary little", {"length_byte_order": "little"}, "1400" + ISO_BODY.hex()),
        # 4-digit ASCII length, payload only
        (
            "4-digit ASCII",
            {"length_prefix_bytes": 4, "length_encoding": "ascii"},
            "30303230" + ISO_BODY.hex(),
        ),
        # 4-digit ASCII counting its own prefix: 20 + 4 = "0024"
        (
            "4-digit ASCII counting prefix",
            {"length_prefix_bytes": 4, "length_encoding": "ascii", "length_includes_prefix": True},
            "30303234" + ISO_BODY.hex(),
        ),
        # mhdr "CDb4" after a 4-digit ASCII length that COUNTS the header: 4 + 20 = "0024"
        (
            "mhdr counted in length",
            {"length_prefix_bytes": 4, "length_encoding": "ascii", "header": MHDR},
            "30303234" + "43446234" + ISO_BODY.hex(),
        ),
        # same header NOT counted: the length stays "0020"
        (
            "mhdr not counted in length",
            {
                "length_prefix_bytes": 4,
                "length_encoding": "ascii",
                "header": MHDR,
                "length_includes_header": False,
            },
            "30303230" + "43446234" + ISO_BODY.hex(),
        ),
        # header given as hex bytes, binary MLI counting it: 4 + 20 = 0x0018
        (
            "header_hex with binary MLI",
            {"header_hex": "43446234"},
            "0018" + "43446234" + ISO_BODY.hex(),
        ),
        # TPDU and header together, both counted: 5 + 4 + 20 = 0x001d
        (
            "tpdu + header",
            {"tpdu_outbound_hex": "6000000000", "tpdu_bytes": 5, "header": MHDR},
            "001d" + "6000000000" + "43446234" + ISO_BODY.hex(),
        ),
        # 2-byte BCD prefix (ADR-0011): 20 -> 0x00 0x20
        ("2-byte BCD", {"length_encoding": "bcd"}, "0020" + ISO_BODY.hex()),
    ],
)
def test_frame_spec_exact_hex_per_configuration(name, cfg, expected_hex):
    spec = _spec(**cfg)
    frame = spec.frame(ISO_BODY)
    assert frame.hex() == expected_hex, name
    # and the read side of the same spec gets the body back, byte for byte
    prefix, rest = frame[: spec.prefix_bytes], frame[spec.prefix_bytes :]
    assert spec.remaining_after_prefix(prefix) == len(rest), name
    assert spec.strip_inbound(rest) == (rest[: spec.fixed_in], ISO_BODY), name


def test_no_prefix_is_refused_by_design():
    """Without a length prefix the stream has no message boundary — the
    adapter refuses it at configuration time (not silently as 2-byte binary)."""
    with pytest.raises(ValueError, match="length_prefix_bytes must be >= 1"):
        _spec(length_prefix_bytes=0)


def test_top_level_mhdr_is_an_alias_of_framing_header():
    via_root = framing.FrameSpec.from_config(
        {"mhdr": MHDR, "framing": {"length_prefix_bytes": 4, "length_encoding": "ascii"}}
    )
    via_framing = _spec(length_prefix_bytes=4, length_encoding="ascii", header=MHDR)
    assert via_root == via_framing
    assert via_root.header_out == b"CDb4" and via_root.header_bytes == 4
    assert via_root.frame(PAYLOAD_00_0F) == b"0020" + b"CDb4" + PAYLOAD_00_0F


def test_conflicting_headers_are_refused():
    with pytest.raises(ValueError, match="conflicting message headers"):
        framing.FrameSpec.from_config({"mhdr": "AAAA", "framing": {"header": "BBBB"}})
    # the same value spelt twice is fine
    spec = framing.FrameSpec.from_config({"mhdr": MHDR, "framing": {"header_hex": "43446234"}})
    assert spec.header_out == b"CDb4"


def test_header_text_must_be_ascii_and_hex_must_be_hex():
    with pytest.raises(ValueError, match="ASCII"):
        _spec(header="héader")
    with pytest.raises(ValueError, match="hex digits"):
        _spec(header_hex="xyz")


def test_inbound_header_width_defaults_to_outbound_and_is_overridable():
    spec = _spec(header=MHDR)
    assert spec.fixed_in == 4
    spec = _spec(header=MHDR, header_bytes=8)
    assert spec.fixed_in == 8
    # not counted in the length: the reader adds the fixed blocks back on
    spec = _spec(header=MHDR, length_includes_header=False)
    assert spec.remaining_after_prefix(b"\x00\x14") == 20 + 4


def test_frame_parts_and_describe_are_what_a_dry_run_shows():
    spec = _spec(length_prefix_bytes=4, length_encoding="chardec", header=MHDR)
    parts = spec.parts(PAYLOAD_00_0F)
    assert parts["hex"] == "30303230" + "43446234" + PAYLOAD_00_0F.hex()
    assert parts["prefix_hex"] == "30303230" and parts["declared_length"] == 20
    assert parts["header_hex"] == "43446234" and parts["tpdu_hex"] == ""
    assert parts["body_bytes"] == 16 and parts["total_bytes"] == 24
    assert spec.describe() == "prefix=4 ascii · header=43446234/4in (counted)"
    assert framing.FrameSpec().describe() == "prefix=2 binary big-endian"


def test_responder_and_adapter_agree_on_a_header_framed_wire():
    """The same FrameSpec on both ends: a simulator can stand in for a host
    that wants ``0024CDb4`` + body, and the adapter reads its header-framed
    replies back."""
    cfg = {"length_prefix_bytes": 4, "length_encoding": "chardec", "header": MHDR}
    responder = TcpResponder(
        {"protocol": "iso8583", "framing": cfg, "default": {"echo": ["11"], "set": {"39": "00"}}}
    )
    adapter_spec = framing.FrameSpec.from_config({"framing": cfg})
    assert responder.frame_spec == adapter_spec
    body = b"0200" + b"2000000000000000" + b"000123"
    assert responder._frame(body) == adapter_spec.frame(body) == b"0030CDb4" + body


async def test_adapter_round_trips_through_a_header_framed_responder():
    cfg = {"length_prefix_bytes": 4, "length_encoding": "chardec", "header": MHDR}
    responder = TcpResponder(
        {"protocol": "iso8583", "framing": cfg, "default": {"echo": ["11"], "set": {"39": "00"}}}
    )
    port = await responder.start()
    adapter = _adapter(port, cfg, timeout=2)
    await adapter.connect()
    try:
        res = await adapter.execute("send_0200", {"amount": 1000})
        assert res.success, res.error
        assert res.response_payload["mti"] == "0210"
        assert res.response_payload["response_code"] == "00"
        tx = next(e for e in adapter.wire_events() if e["dir"] == "tx")
        assert tx["hex"].startswith("3030")  # ASCII digits …
        assert bytes.fromhex(tx["hex"])[4:8] == b"CDb4"  # … then the header
        rx = next(e for e in adapter.wire_events() if e["dir"] == "rx")
        assert bytes.fromhex(rx["hex"])[4:8] == b"CDb4"  # the reply carries it too
    finally:
        await adapter.disconnect()
        await responder.stop()
