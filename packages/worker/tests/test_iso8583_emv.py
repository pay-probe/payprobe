"""ADR-0013 phase 3: EMV DE 55 awareness on the wire.

Decoded messages carry an ``emv`` tag map; responder rules match on tags; the
adapter's shaped response exposes them; the VISA simulator verifies the ARQC
in tag 9F26 over the message's own tags, derives the session key from an MDK
the way an issuer does, and returns an ARPC (tag 91) the client can verify.
"""

from __future__ import annotations

from payprobe_common import iso8583 as shared
from payprobe_common.iso8583 import build_tlv, tag_map
from worker.adapters.scheme.visa import VISA_FIELDS, VisaSimulator
from worker.adapters.tcp.adapter import TcpAdapter
from worker.adapters.tcp.responder import TcpResponder
from worker.engine import crypto_tools as ct

MDK = "0123456789ABCDEFFEDCBA9876543210"
PAN, PSN, ATC = "5555550000001111", "00", "001C"

#: a chip transaction's DE 55 as a terminal would send it (CVN 10/18 data set)
CHIP = {
    "9F02": "000000001000",  # amount
    "9F03": "000000000000",  # other amount
    "9F1A": "0826",  # terminal country
    "95": "0000008000",  # TVR
    "5F2A": "0826",  # currency
    "9A": "260925",  # date
    "9C": "00",  # type
    "9F37": "1A2B3C4D",  # unpredictable number
    "82": "1980",  # AIP
    "9F36": ATC,  # ATC
    "9F10": "06010A03A00000",  # IAD
    "9F27": "80",  # CID: ARQC
    "5F34": "00",  # PSN
}


def _session_key():
    udk = ct.emv_icc_mk(MDK, PAN, PSN)["udk"]
    return ct.emv_session_key(udk, ATC)["session_key"]


def _de55(cryptogram: str | None = None, **over) -> str:
    tags = {**CHIP, **over}
    data = "".join(tags[t] for t in VisaSimulator.ARQC_DATA_TAGS)
    arqc = cryptogram if cryptogram is not None else ct.arqc(_session_key(), data)["arqc"]
    nodes = [{"tag": t, "value": v} for t, v in tags.items()] + [{"tag": "9F26", "value": arqc}]
    return build_tlv(nodes)


async def _client(port, **cfg):
    a = TcpAdapter(
        {
            "host": "127.0.0.1",
            "port": port,
            "fields": VISA_FIELDS,
            "framing": {"length_prefix_bytes": 2},
            "sign_on": {"enabled": False},
            "response_timeout_sec": 2,
            "reconnect": {"enabled": False},
            **cfg,
        }
    )
    await a.connect()
    return a


# -- tag map + rules --------------------------------------------------------------


def test_tag_map_flattens_and_descends_constructed_tags():
    inner = build_tlv([{"tag": "9F26", "value": "0011223344556677"}])
    hexstr = build_tlv(
        [
            {"tag": "9F27", "value": "80"},
            {"tag": "71", "children": [{"tag": "9F26", "value": "0011223344556677"}]},
        ]
    )
    assert tag_map(hexstr) == {"9F27": "80", "9F26": "0011223344556677"}
    assert tag_map(inner)["9F26"] == "0011223344556677"


async def test_responder_rules_match_on_emv_tags_and_expose_the_map():
    r = TcpResponder(
        {
            "protocol": "iso8583",
            "fields": VISA_FIELDS,
            "framing": {"length_prefix_bytes": 2},
            "rules": [
                {
                    "when": {"emv": {"9F27": {"eq": "00"}}},
                    "respond": {"echo": ["11"], "set": {"39": "05"}},
                },
                {
                    "when": {"emv": {"9F27": "80", "9F1A": {"prefix": "08"}}},
                    "respond": {"echo": ["11"], "set": {"39": "00"}},
                },
            ],
            "default": {"echo": ["11"], "set": {"39": "91"}},
        }
    )
    port = await r.start()
    a = await _client(port)
    try:
        approved = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": _de55()}}
        )
        assert approved.response_payload["response_code"] == "00"
        declined = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": _de55(**{"9F27": "00"})}}
        )
        assert declined.response_payload["response_code"] == "05"
        plain = await a.execute("send_0200", {"values": {"2": PAN, "4": "000000001000"}})
        assert plain.response_payload["response_code"] == "91"  # no DE 55: neither rule
        assert r.received[0]["emv"]["9F27"] == "80" and "emv" not in r.received[2]
        # the client sees the same map on the reply when the host echoes DE 55
    finally:
        await a.disconnect()
        await r.stop()


async def test_shaped_response_exposes_emv_tags_when_the_host_returns_de55():
    r = TcpResponder(
        {
            "protocol": "iso8583",
            "fields": VISA_FIELDS,
            "framing": {"length_prefix_bytes": 2},
            "default": {"echo": ["11", "55"], "set": {"39": "00"}},
        }
    )
    port = await r.start()
    a = await _client(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": _de55()}}
        )
        assert res.response_payload["emv"]["9F36"] == ATC
        assert res.response_payload["emv"]["9F27"] == "80"
    finally:
        await a.disconnect()
        await r.stop()


# -- the VISA simulator as an issuer -----------------------------------------------


async def _visa(verify_arqc: dict):
    r = VisaSimulator(
        {
            "protocol": "visa",
            "fields": VISA_FIELDS,
            "visa": {"stand_in": True, "verify_arqc": verify_arqc},
        }
    )
    port = await r.start()
    return r, port


async def test_visa_verifies_the_arqc_from_the_message_tags_with_a_given_session_key():
    r, port = await _visa({"session_key": _session_key(), "decline": "05"})
    a = await _client(port)
    try:
        ok = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": _de55()}}
        )
        assert ok.response_payload["response_code"] == "00" and ok.response_payload["auth_code"]
        forged = await a.execute(
            "send_0200",
            {"values": {"2": PAN, "4": "000000001000", "55": _de55("0000000000000000")}},
        )
        assert forged.response_payload["response_code"] == "05"
        # the amount in the tags changed after the cryptogram was computed: a replay/tamper
        tampered = _de55().replace(CHIP["9F02"], "000000009999", 1)
        replay = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000009999", "55": tampered}}
        )
        assert replay.response_payload["response_code"] == "05"
    finally:
        await a.disconnect()
        await r.stop()


async def test_visa_derives_the_session_key_from_the_mdk_and_returns_a_verifiable_arpc():
    r, port = await _visa({"mdk": MDK, "decline": "05", "arpc": True})
    a = await _client(port)
    try:
        res = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": _de55()}}
        )
        assert res.response_payload["response_code"] == "00"
        iad = res.response_payload["emv"]["91"]
        arpc, arc_hex = iad[:16], iad[16:]
        assert bytes.fromhex(arc_hex).decode() == "00"
        de55 = _de55()
        arqc = tag_map(de55)["9F26"]
        expected = ct.arpc(_session_key(), arqc, arc_hex=arc_hex, method="1")
        assert expected["arpc"] == arpc
        # a declined chip transaction carries no ARPC
        bad = await a.execute(
            "send_0200",
            {"values": {"2": PAN, "4": "000000001000", "55": _de55("FFFFFFFFFFFFFFFF")}},
        )
        assert bad.response_payload["response_code"] == "05" and "emv" not in bad.response_payload
    finally:
        await a.disconnect()
        await r.stop()


async def test_visa_arqc_check_skips_when_nothing_to_check_and_legacy_vector_still_works():
    r, port = await _visa({"mdk": MDK, "decline": "05"})
    a = await _client(port)
    try:
        # magstripe transaction (no DE 55): the chip check does not fail it
        res = await a.execute("send_0200", {"values": {"2": PAN, "4": "000000001000"}})
        assert res.response_payload["response_code"] == "00"
    finally:
        await a.disconnect()
        await r.stop()
    # legacy shape: verbatim data, the whole configured field is the cryptogram
    sk = _session_key()
    data = "0000001000"
    r, port = await _visa({"session_key": sk, "data": data, "field": "55", "decline": "05"})
    a = await _client(port)
    try:
        good = await a.execute(
            "send_0200",
            {"values": {"2": PAN, "4": "000000001000", "55": ct.arqc(sk, data)["arqc"]}},
        )
        assert good.response_payload["response_code"] == "00"
        bad = await a.execute(
            "send_0200", {"values": {"2": PAN, "4": "000000001000", "55": "0000000000000000"}}
        )
        assert bad.response_payload["response_code"] == "05"
    finally:
        await a.disconnect()
        await r.stop()


def test_shared_dictionary_still_types_de55_as_binary_for_the_wire():
    assert shared.ISO8583_1987["55"]["type"] == "b" and VISA_FIELDS["55"]["type"] == "b"
