"""payShield 10K host-command handlers.

Each handler takes the decoded request ``data`` (everything after the 2-char
command code, ASCII) and a :class:`HsmContext`, and returns
``(error_code, response_data)``. The simulator frames the reply as
``header + response_code + error_code + response_data`` — the response code is
the request command with its second character incremented (A0->A1, CW->CX,
NC->ND, …), computed centrally so handlers never spell it out.

Crypto fidelity is **hybrid**: KCVs, CVV/CVC, PVV, IBM 3624 PIN offsets, ISO-0
PIN blocks and retail MACs are computed for real (from the clear key the test
LMK unwraps), so values verify across commands and against independent tools;
key *material* is random and key tokens only round-trip within this simulator
(see :mod:`.lmk`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ...engine import crypto_tools as ct
from .lmk import TestLmk, kcv6, read_key_field, variant_for_keytype

log = logging.getLogger(__name__)

#: firmware reference reported by NC; override per-simulator via config.
DEFAULT_FIRMWARE = "0007-E000"

# Standard-ish payShield error codes used by the simulator.
ERR_OK = "00"
ERR_VERIFY_FAIL = "01"  # CVV/MAC/PIN/ARQC failed verification
ERR_PARITY = "10"  # key parity error
ERR_INVALID_DATA = "15"  # invalid input data / parse failure
ERR_NO_COMMAND = "30"  # message format / unknown command
ERR_DISABLED = "68"  # command disabled by a security setting

#: payShield PIN-block format code (2N) -> ISO 9564-1 format the codec uses.
#: Codes follow the Thales convention (01 = ISO-0/ANSI X9.8, 05 = ISO-1,
#: 47 = ISO-3, 48 = ISO-4/AES); anything unknown falls back to ISO-0. ISO-4
#: (format 48) uses an AES PIN key; the others are DES.
PIN_FORMAT_CODES = {"01": "0", "05": "1", "47": "3", "48": "4"}


def _pin_fmt(code: str) -> str:
    """Map a payShield PIN-block format code to an ISO 9564-1 format ('0'/'1'/'3'/'4')."""
    return PIN_FORMAT_CODES.get((code or "").strip(), "0")


def _split_translate_tail(body: str) -> tuple[str, str, str, str]:
    """Split a translate command's ``<PIN block><src fmt 2><dst fmt 2><PAN 12>``
    tail. The trailing three fields are fixed width, so the PIN block takes the
    remainder — which lets its width follow the source format (16 hex for the DES
    formats, 32 hex for an ISO-4/AES block) without reordering the layout."""
    pan = body[-12:]
    src_fmt, dst_fmt = body[-16:-14], body[-14:-12]
    pin_block = body[:-16]
    return src_fmt, dst_fmt, pin_block, pan


@dataclass
class HsmContext:
    """Shared state handed to every command handler."""

    lmk: TestLmk
    firmware: str = DEFAULT_FIRMWARE
    #: optional scripted overrides: command -> {"error": "..", "data": ".."}.
    overrides: dict[str, dict] = field(default_factory=dict)
    #: command codes disabled by a security setting -> they return error 68.
    disabled_commands: set[str] = field(default_factory=set)
    #: PCI-HSM compliance flag reported by the NO (HSM status) command.
    pci_hsm: bool = False


Handler = Callable[[HsmContext, str], tuple[str, str]]
_REGISTRY: dict[str, Handler] = {}


def command(code: str):
    def _wrap(fn: Handler) -> Handler:
        _REGISTRY[code] = fn
        return fn

    return _wrap


def response_code(command_code: str) -> str:
    """payShield response code: the command with its 2nd char incremented."""
    if len(command_code) == 2:
        return command_code[0] + chr(ord(command_code[1]) + 1)
    return command_code


def dispatch(ctx: HsmContext, cmd: str, data: str) -> tuple[str, str]:
    """Resolve a command to ``(error_code, response_data)``."""
    if cmd in ctx.overrides:
        ov = ctx.overrides[cmd]
        return ov.get("error", ERR_OK), ov.get("data", "")
    if cmd in ctx.disabled_commands:
        return ERR_DISABLED, ""
    handler = _REGISTRY.get(cmd)
    if handler is None:
        return ERR_NO_COMMAND, ""
    try:
        return handler(ctx, data)
    except Exception as exc:  # noqa: BLE001 — a bad request is a client error, not a crash
        log.debug("payShield %s handler error: %s", cmd, exc)
        return ERR_INVALID_DATA, ""


def supported_commands() -> list[str]:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _trim_trailer(data: str) -> str:
    """Drop an optional ``%``+LMK-id suffix and any message trailer (X'19')."""
    data = data.split("\x19", 1)[0]
    if "%" in data:
        data = data.rsplit("%", 1)[0]
    return data


def _read_cvv_data(rest: str) -> tuple[str, str, str]:
    """Parse the CVV source data (``#``-form or PAN``;``expiry+service-code)."""
    rest = _trim_trailer(rest)
    if rest[:1] == "#":
        block = rest[1:33]
        # 32N single field: trailing service code (3) + expiry (4) + PAN.
        pan, expiry, sc = block[:-7], block[-7:-3], block[-3:]
        return pan, expiry, sc
    pan, _, tail = rest.partition(";")
    return pan, tail[:4], tail[4:7]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@command("NC")
def _nc(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Perform Diagnostics — return the LMK check value + firmware number."""
    return ERR_OK, ctx.lmk.check_value() + ctx.firmware


@command("A0")
def _a0(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate a Key. Mints a random key under the test LMK; optionally also
    encrypts it under a supplied ZMK/TMK for export. Returns token(s) + KCV."""
    data = _trim_trailer(data)
    keytype = data[1:4]
    scheme = data[4] if data[4:5] in ("U", "T", "X", "Y", "Z", "R", "W") else "U"
    _, token, kcv = ctx.lmk.generate(scheme, variant_for_keytype(keytype))
    out = token
    # optional export: ';' + flag + ZMK token + export scheme
    tail = data[5:]
    if tail[:1] == ";" and tail[1:2] == "1":
        zmk_token, after = read_key_field(tail[2:])
        export_scheme = after[:1] if after[:1] in ("U", "T", "X") else "X"
        clear = ctx.lmk.unwrap(token, variant_for_keytype(keytype))
        zmk_clear = ctx.lmk.unwrap(zmk_token, variant_for_keytype("000"))
        export_ct = ct.des(zmk_clear, clear, op="encrypt", mode="ecb")["result"]
        out += f"{export_scheme}{export_ct}"
    return ERR_OK, out + kcv


def _bu_keytype3(code2: str) -> str:
    """Reconstruct a 3-digit key-type code from BU's 2-digit form (the same
    code without the middle digit), e.g. '42' -> '402', '01' -> '001'."""
    code2 = code2.strip()
    return (code2[0] + "0" + code2[1]) if len(code2) == 2 else code2


@command("BU")
def _bu(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate a Key Check Value for a key encrypted under the LMK."""
    body = _trim_trailer(data)
    keytype = _bu_keytype3(body[:2])
    token, _ = read_key_field(body[3:])  # after 2-digit type code + length flag
    clear = ctx.lmk.unwrap(token, variant_for_keytype(keytype))
    return ERR_OK, kcv6(clear)


_CVK_VARIANT = variant_for_keytype("402")


@command("CW")
def _cw(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate a Card Verification Code/Value (Visa CVV / Mastercard CVC)."""
    token, rest = read_key_field(_trim_trailer(data))
    cvk = ctx.lmk.unwrap(token, _CVK_VARIANT)
    pan, expiry, sc = _read_cvv_data(rest)
    return ERR_OK, ct.cvv(pan, expiry, sc, cvk)["cvv"]


@command("CY")
def _cy(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Verify a CVV/CVC. Error 01 on mismatch."""
    token, rest = read_key_field(_trim_trailer(data))
    cvk = ctx.lmk.unwrap(token, _CVK_VARIANT)
    supplied, rest = rest[:3], rest[3:]
    pan, expiry, sc = _read_cvv_data(rest)
    expected = ct.cvv(pan, expiry, sc, cvk)["cvv"]
    return (ERR_OK if supplied == expected else ERR_VERIFY_FAIL), ""


@command("CA")
def _ca(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Translate a PIN block from TPK to ZPK encryption, reformatting between
    ISO 9564-1 formats if the source and destination format codes differ.

    Layout: <TPK><ZPK><maxPINlen 2N><PIN block 16H><src fmt 2N><dst fmt 2N><PAN 12N>
    """
    body = _trim_trailer(data)
    tpk_t, body = read_key_field(body)
    zpk_t, body = read_key_field(body)
    body = body[2:]  # max PIN length (2N)
    src_fmt, dst_fmt, pin_block, pan = _split_translate_tail(body)
    tpk, zpk = ctx.lmk.unwrap(tpk_t), ctx.lmk.unwrap(zpk_t)
    pin = ct.pin_block_decode(pin_block, pan, tpk, _pin_fmt(src_fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    out_block = ct.pin_block_encode(pin, pan, zpk, _pin_fmt(dst_fmt))["pin_block"]
    return ERR_OK, f"{len(pin):02d}{out_block}"


@command("EC")
def _ec(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Verify an interchange PIN using the Visa PVV method.

    Layout: <ZPK><PVK><PIN block 16H><fmt 2N><PAN 12N><PVKI 1N><PVV 4N>
    """
    body = _trim_trailer(data)
    zpk_t, body = read_key_field(body)
    pvk_t, body = read_key_field(body)
    pin_block, body = body[:16], body[16:]
    fmt, body = body[:2], body[2:]  # PIN block format code
    pan, body = body[:12], body[12:]
    pvki, supplied = body[:1], body[1:5]
    zpk, pvk = ctx.lmk.unwrap(zpk_t), ctx.lmk.unwrap(pvk_t)
    pin = ct.pin_block_decode(pin_block, pan, zpk, _pin_fmt(fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    expected = ct.pvv(pan, pin, pvki, pvk)["pvv"]
    return (ERR_OK if supplied == expected else ERR_VERIFY_FAIL), ""


def _ibm_pin_verify_layout(ctx: HsmContext, data: str, pin_key_variant: int):
    """Shared parser for the IBM-method verify commands (BA terminal / BE
    interchange). Both carry:

        <PIN key><PVK><PIN block 16H><fmt 2N><PAN 12N><dec table 16N>
        <validation data 16H><offset ...>

    They differ only in which key encrypts the incoming PIN block (a TPK for BA,
    a ZPK for BE) — hence the ``pin_key_variant``. Returns the verify verdict."""
    body = _trim_trailer(data)
    pin_key_t, body = read_key_field(body)
    pvk_t, body = read_key_field(body)
    pin_block, body = body[:16], body[16:]
    fmt, body = body[:2], body[2:]  # PIN block format code
    pan, body = body[:12], body[12:]
    dec_table, body = body[:16], body[16:]
    val_data, body = body[:16], body[16:]
    offset = body  # remaining digits are the stored offset
    pin_key = ctx.lmk.unwrap(pin_key_t, pin_key_variant)
    pvk = ctx.lmk.unwrap(pvk_t)
    pin = ct.pin_block_decode(pin_block, pan, pin_key, _pin_fmt(fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    ok = ct.ibm3624_verify(val_data, pvk, offset, pin, dec_table)["match"]
    return (ERR_OK if ok else ERR_VERIFY_FAIL), ""


@command("DE")
def _de(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate an IBM 3624 PIN Offset.

    Layout: <TPK><PVK><PIN block 16H><fmt 2N><PAN 12N><dec table 16N>
            <validation data 16H>. Decodes the customer PIN with the TPK,
            derives the natural PIN from the validation data under the PVK, and
            returns offset = PIN - natural (digit-wise, mod 10)."""
    body = _trim_trailer(data)
    tpk_t, body = read_key_field(body)
    pvk_t, body = read_key_field(body)
    pin_block, body = body[:16], body[16:]
    fmt, body = body[:2], body[2:]  # PIN block format code
    pan, body = body[:12], body[12:]
    dec_table, body = body[:16], body[16:]
    val_data = body[:16]
    tpk, pvk = ctx.lmk.unwrap(tpk_t), ctx.lmk.unwrap(pvk_t)
    pin = ct.pin_block_decode(pin_block, pan, tpk, _pin_fmt(fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    return ERR_OK, ct.ibm3624_offset(val_data, pvk, pin, dec_table)["offset"]


@command("BA")
def _ba(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Verify a Terminal PIN using the IBM Method (PIN block under a TPK)."""
    return _ibm_pin_verify_layout(ctx, data, variant_for_keytype("002"))


@command("BE")
def _be(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Verify an Interchange PIN using the IBM Method (PIN block under a ZPK)."""
    return _ibm_pin_verify_layout(ctx, data, variant_for_keytype("001"))


@command("JA")
def _ja(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate a Visa PIN Verification Value (PVV).

    Layout: <ZPK><PVK><PIN block 16H><fmt 2N><PAN 12N><PVKI 1N>. Decodes the PIN
    with the ZPK and returns the 4-digit PVV — the value EC verifies against."""
    body = _trim_trailer(data)
    zpk_t, body = read_key_field(body)
    pvk_t, body = read_key_field(body)
    pin_block, body = body[:16], body[16:]
    fmt, body = body[:2], body[2:]  # PIN block format code
    pan, body = body[:12], body[12:]
    pvki = body[:1]
    zpk, pvk = ctx.lmk.unwrap(zpk_t), ctx.lmk.unwrap(pvk_t)
    pin = ct.pin_block_decode(pin_block, pan, zpk, _pin_fmt(fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    return ERR_OK, ct.pvv(pan, pin, pvki, pvk)["pvv"]


@command("M6")
def _m6(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Generate a MAC (ISO 9797-1 algorithm 3 / retail MAC) over a data block."""
    token, rest = read_key_field(_trim_trailer(data))
    key = ctx.lmk.unwrap(token)
    msg_hex = rest if _is_hex(rest) else rest.encode("ascii").hex().upper()
    return ERR_OK, ct.retail_mac(key, msg_hex)["mac"]


@command("M8")
def _m8(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Verify a MAC. The supplied MAC (16H) trails the data block."""
    token, rest = read_key_field(_trim_trailer(data))
    key = ctx.lmk.unwrap(token)
    supplied, msg = rest[-16:], rest[:-16]
    msg_hex = msg if _is_hex(msg) else msg.encode("ascii").hex().upper()
    expected = ct.retail_mac(key, msg_hex)["mac"]
    return (ERR_OK if supplied.upper() == expected else ERR_VERIFY_FAIL), ""


def _is_hex(s: str) -> bool:
    return bool(s) and all(c in "0123456789ABCDEFabcdef" for c in s)


@command("CC")
def _cc(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Translate a PIN block from one ZPK to another, reformatting between ISO
    9564-1 formats if the source and destination format codes differ.

    Layout: <src ZPK><dst ZPK><max PIN len 2N><src PIN block 16H><src fmt 2N>
            <dst fmt 2N><PAN 12N>. Returns PIN length (2N) + dst PIN block (16H)
            + dst format (2N).
    """
    body = _trim_trailer(data)
    src_t, body = read_key_field(body)
    dst_t, body = read_key_field(body)
    body = body[2:]  # max PIN length
    src_fmt, dst_fmt, pin_block, pan = _split_translate_tail(body)
    src, dst = ctx.lmk.unwrap(src_t), ctx.lmk.unwrap(dst_t)
    pin = ct.pin_block_decode(pin_block, pan, src, _pin_fmt(src_fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    out_block = ct.pin_block_encode(pin, pan, dst, _pin_fmt(dst_fmt))["pin_block"]
    return ERR_OK, f"{len(pin):02d}{out_block}{dst_fmt}"


@command("A6")
def _a6(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Import a Key encrypted under a ZMK. Decrypts the key from the ZMK and
    re-wraps it under the LMK.

    Layout: <key type 3H><ZMK><key-under-ZMK><LMK key scheme 1A>. Returns the
    imported key token + KCV.
    """
    body = _trim_trailer(data)
    keytype, body = body[:3], body[3:]
    zmk_t, body = read_key_field(body)
    enc_key, body = read_key_field(body)
    scheme = body[:1] if body[:1] in ("U", "T", "X", "Y", "Z") else "U"
    zmk_clear = ctx.lmk.unwrap(zmk_t, variant_for_keytype("000"))
    if enc_key[:1] in ("U", "X", "T", "Y", "Z"):
        enc_key = enc_key[1:]
    clear = ct.des(zmk_clear, enc_key, op="decrypt", mode="ecb")["result"]
    token = ctx.lmk.wrap(clear, scheme, variant_for_keytype(keytype))
    return ERR_OK, token + kcv6(clear)


@command("K8")
def _k8(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Export a Key under a KEK/ZMK. Decrypts the key from the LMK and
    re-encrypts it under the KEK.

    Layout: <key type 3H><key (LMK)><KEK><KEK key scheme 1A>. Returns the
    key encrypted under the KEK + KCV.
    """
    body = _trim_trailer(data)
    keytype, body = body[:3], body[3:]
    key_t, body = read_key_field(body)
    kek_t, body = read_key_field(body)
    scheme = body[:1] if body[:1] in ("U", "T", "X", "Y", "Z") else "X"
    clear = ctx.lmk.unwrap(key_t, variant_for_keytype(keytype))
    kek_clear = ctx.lmk.unwrap(kek_t, variant_for_keytype("000"))
    enc = ct.des(kek_clear, clear, op="encrypt", mode="ecb")["result"]
    return ERR_OK, f"{scheme}{enc}" + kcv6(clear)


@command("G0")
def _g0(ctx: HsmContext, data: str) -> tuple[str, str]:
    """Translate a PIN block from BDK (3DES DUKPT) to ZPK encryption.

    Layout: <BDK><ZPK><KSN 20H><src PIN block 16H><src fmt 2N><dst fmt 2N>
            <PAN 12N>. The DUKPT PIN key is re-derived from BDK + KSN. Returns
            PIN length (2N) + dst PIN block (16H).
    """
    body = _trim_trailer(data)
    bdk_t, body = read_key_field(body)
    zpk_t, body = read_key_field(body)
    ksn, body = body[:20], body[20:]
    src_fmt, dst_fmt, pin_block, pan = _split_translate_tail(body)
    bdk = ctx.lmk.unwrap(bdk_t, variant_for_keytype("302"))
    zpk = ctx.lmk.unwrap(zpk_t)
    pek = ct.dukpt_derive_key(bdk_hex=bdk, ksn_hex=ksn, variant="pin")["key"]
    pin = ct.pin_block_decode(pin_block, pan, pek, _pin_fmt(src_fmt))["pin"]
    if not pin:
        return ERR_INVALID_DATA, ""
    out_block = ct.pin_block_encode(pin, pan, zpk, _pin_fmt(dst_fmt))["pin_block"]
    return ERR_OK, f"{len(pin):02d}{out_block}"


_MKAC_VARIANT = variant_for_keytype("109")


@command("KQ")
def _kq(ctx: HsmContext, data: str) -> tuple[str, str]:
    """ARQC verification and/or ARPC generation (static/Mastercard SKD).

    Simulator layout (hex-ASCII fields; the real command packs some as binary):
    <mode 1N><scheme 1N><MK-AC><PAN 16H><PSN 2N><ATC 4H><txnlen 2H><txn data>
    ; <ARQC 16H> [<ARC 4H> when mode 1/2]. Derives the ICC master key (EMV
    option A) and AC session key, verifies the ARQC, and on mode 1/2 returns
    the ARPC.
    """
    body = _trim_trailer(data)
    mode, body = body[:1], body[1:]
    _scheme, body = body[:1], body[1:]
    mkac_t, body = read_key_field(body)
    pan, body = body[:16], body[16:]
    psn, body = body[:2], body[2:]
    atc, body = body[:4], body[4:]
    txnlen = int(body[:2], 16) * 2
    body = body[2:]
    txn, body = body[:txnlen], body[txnlen:]
    if body[:1] == ";":
        body = body[1:]
    arqc_supplied, body = body[:16], body[16:]
    arc = body[:4]

    mkac = ctx.lmk.unwrap(mkac_t, _MKAC_VARIANT)
    udk = ct.emv_icc_mk(mkac, pan, psn)["udk"]
    sk = ct.emv_session_key(udk, atc)["session_key"]
    check = ct.arqc(sk, txn, expected=arqc_supplied)
    if not check.get("match"):
        return ERR_VERIFY_FAIL, ""
    if mode in ("1", "2"):
        arpc = ct.arpc(sk, check["arqc"], arc_hex=arc, method="1")["arpc"]
        return ERR_OK, arpc
    return ERR_OK, ""


@command("NO")
def _no(ctx: HsmContext, data: str) -> tuple[str, str]:
    """HSM status. Mode 00 returns buffer/ethernet/sockets/firmware; mode 01
    returns the PCI-HSM compliance flag."""
    mode = data[:2] or "00"
    if mode == "01":
        return ERR_OK, ("1" if ctx.pci_hsm else "0") + "0" * 10
    # mode 00: I/O buffer (1=8K), Ethernet (1=TCP), sockets (0128), firmware,
    # reserved (1N) + reserved (4A).
    return ERR_OK, "11" + "0128" + ctx.firmware + "0" + "0000"
