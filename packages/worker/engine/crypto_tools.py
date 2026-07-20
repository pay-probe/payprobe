"""Payment cryptography for ``crypto`` nodes (real algorithms, not sandboxed code).

Covers the common HSM-style operations a payment test needs: DES/3DES and AES,
key check value, ISO 9564-1 PIN blocks (DES formats 0/1/3 and AES format 4),
Retail MAC (ISO 9797-1 MAC algorithm 3 / ANSI X9.19), AES-CMAC, Visa CVV/CVC,
Visa PVV, the IBM 3624 PIN offset method, TDES and AES (X9.24-3) DUKPT, and the
EMV application cryptogram (ARQC) MAC.

All hex in / hex out. Uses pycryptodome (``Crypto.Cipher.DES``/``DES3``); these
run in the worker process, so the sandbox restriction on code nodes doesn't
apply. Keys: 8 bytes ⇒ single DES, 16/24 bytes ⇒ Triple-DES.
"""

from __future__ import annotations

import secrets
from typing import Any


def _hx(s: str) -> bytes:
    return bytes.fromhex("".join((s or "").split()))


def _ecb(key: bytes):
    from Crypto.Cipher import DES, DES3

    if len(key) == 8:
        return DES.new(key, DES.MODE_ECB)
    return DES3.new(key, DES3.MODE_ECB)


def _des_block(key: bytes, block: bytes, encrypt: bool) -> bytes:
    from Crypto.Cipher import DES

    c = DES.new(key, DES.MODE_ECB)
    return c.encrypt(block) if encrypt else c.decrypt(block)


# -- generic DES / 3DES ------------------------------------------------------


def des(
    key_hex: str, data_hex: str, op: str = "encrypt", mode: str = "ecb", iv_hex: str = ""
) -> dict:
    from Crypto.Cipher import DES, DES3

    key, data = _hx(key_hex), _hx(data_hex)
    iv = _hx(iv_hex) if iv_hex else b"\x00" * 8
    algo = DES if len(key) == 8 else DES3
    cipher = algo.new(key, algo.MODE_CBC, iv) if mode == "cbc" else algo.new(key, algo.MODE_ECB)
    out = cipher.encrypt(data) if op == "encrypt" else cipher.decrypt(data)
    return {"result": out.hex().upper()}


def kcv(key_hex: str) -> dict:
    """Key Check Value: encrypt 8 zero bytes, take the first 3 (6 hex)."""
    out = _ecb(_hx(key_hex)).encrypt(b"\x00" * 8)
    return {"kcv": out.hex().upper()[:6], "full": out.hex().upper()}


# -- ISO 9564-1 PIN blocks (DES formats 0, 1, 3) -----------------------------
#
# Format 0 (ANSI X9.8): control nibble 0, PIN prefixed by its length and padded
#   with 'F', then XORed with a PAN field. Deterministic.
# Format 1: control nibble 1, no PAN, padded with a random/transaction field —
#   the block is randomised (two encodes of the same PIN differ) but still
#   decodes back to the PIN.
# Format 3: control nibble 3, PIN padded with random nibbles A–F, XORed with the
#   PAN field (like format 0 but with a randomised, non-'F' fill).
#
# Formats 0 and 3 bind the PAN into the block; format 1 does not. Format 2 (no
# PAN, offline smartcard) and format 4 (AES) are out of scope for this DES codec.

_ISO_PIN_FORMATS = {"0", "1", "3"}


def _fmt_uses_pan(fmt: str) -> bool:
    return str(fmt) in ("0", "3")


def _pin_field(pin: str, fmt: str = "0") -> bytes:
    """Build the 8-byte PIN field for an ISO 9564-1 ``fmt`` (0/1/3)."""
    fmt = str(fmt)
    control = fmt if fmt in _ISO_PIN_FORMATS else "0"
    body = control + format(len(pin), "X") + pin
    fill = 16 - len(body)
    if fmt == "3":
        pad = "".join(secrets.choice("ABCDEF") for _ in range(fill))
    elif fmt == "1":
        pad = secrets.token_hex(8).upper()[:fill]
    else:  # format 0
        pad = "F" * fill
    return _hx((body + pad)[:16])


def _pan_field(pan: str) -> bytes:
    digits = "".join(c for c in pan if c.isdigit())
    acct = digits[-13:-1]  # rightmost 12 digits excluding the check digit
    return _hx("0000" + acct.rjust(12, "0"))


def pin_block_encode(pin: str, pan: str, key_hex: str, fmt: str = "0") -> dict:
    """Encrypt a clear PIN into an ISO 9564-1 ``fmt`` PIN block under ``key``.

    Formats 0/1/3 are DES-based (8-byte block); format 4 is AES-based (16-byte
    block, two-pass PIN⊕PAN construction) and expects an AES ``key``."""
    if str(fmt) == "4":
        return pin_block_iso4_encode(pin, pan, key_hex)
    field = _pin_field(pin, fmt)
    clear = bytes(a ^ b for a, b in zip(field, _pan_field(pan))) if _fmt_uses_pan(fmt) else field
    enc = _ecb(_hx(key_hex)).encrypt(clear)
    return {"clear_block": clear.hex().upper(), "pin_block": enc.hex().upper(), "format": str(fmt)}


def pin_block_decode(pin_block_hex: str, pan: str, key_hex: str, fmt: str = "0") -> dict:
    """Recover the clear PIN from an ISO 9564-1 ``fmt`` PIN block."""
    if str(fmt) == "4":
        return pin_block_iso4_decode(pin_block_hex, pan, key_hex)
    clear = _ecb(_hx(key_hex)).decrypt(_hx(pin_block_hex))
    field = bytes(a ^ b for a, b in zip(clear, _pan_field(pan))) if _fmt_uses_pan(fmt) else clear
    pf = field.hex().upper()
    try:
        length = int(pf[1], 16)
        pin = pf[2 : 2 + length]
    except (ValueError, IndexError):
        pin = ""
    return {"pin": pin, "clear_block": clear.hex().upper(), "format": str(fmt)}


# -- Retail MAC (ISO 9797-1 MAC algorithm 3, method-2 padding) ---------------


def _pad_m2(data: bytes) -> bytes:
    data += b"\x80"
    while len(data) % 8:
        data += b"\x00"
    return data


def retail_mac(key_hex: str, data_hex: str) -> dict:
    key = _hx(key_hex)
    k1, k2 = key[:8], (key[8:16] or key[:8])
    blocks = _pad_m2(_hx(data_hex))
    h = b"\x00" * 8
    for i in range(0, len(blocks), 8):
        h = _des_block(k1, bytes(a ^ b for a, b in zip(h, blocks[i : i + 8])), True)
    h = _des_block(k2, h, False)  # decrypt with K2
    h = _des_block(k1, h, True)  # encrypt with K1
    return {"mac": h.hex().upper(), "mac4": h.hex().upper()[:8]}


# -- Visa CVV / CVC ----------------------------------------------------------


def cvv(pan: str, expiry: str, service_code: str, cvk_hex: str) -> dict:
    cvk = _hx(cvk_hex)
    a, b = cvk[:8], (cvk[8:16] or cvk[:8])
    block = (pan + expiry + service_code).ljust(32, "0")[:32]
    b1, b2 = _hx(block[:16]), _hx(block[16:32])
    r = _des_block(a, b1, True)
    r = bytes(x ^ y for x, y in zip(r, b2))
    r = _des_block(a, r, True)
    r = _des_block(b, r, False)
    r = _des_block(a, r, True)
    hexstr = r.hex().upper()
    digits = [c for c in hexstr if c.isdigit()]
    letters = [str(int(c, 16) - 10) for c in hexstr if not c.isdigit()]
    return {"cvv": "".join(digits + letters)[:3]}


# -- EMV ARQC / ARPC (application + response cryptograms) ---------------------


def _match(out: dict, key: str, expected: str) -> dict:
    """Attach a verification verdict when an ``expected`` value is supplied."""
    if expected:
        want = "".join((expected or "").split()).upper()
        out["expected"] = want
        out["match"] = out[key] == want
    return out


def arqc(session_key_hex: str, data_hex: str, expected: str = "") -> dict:
    """Generate (or verify) an Application Cryptogram — Retail MAC over the
    transaction data with the AC session key. Pass ``expected`` to verify."""
    mac = retail_mac(session_key_hex, data_hex)
    return _match({"arqc": mac["mac"]}, "arqc", expected)


def arpc(
    session_key_hex: str,
    arqc_hex: str,
    arc_hex: str = "",
    csu_hex: str = "",
    proprietary_hex: str = "",
    method: str = "1",
    expected: str = "",
) -> dict:
    """Generate (or verify) the Authorisation Response Cryptogram.

    Method 1 (ARC): ARPC = 3DES(SK_AC, ARQC XOR (ARC || 00..00)), 8 bytes.
    Method 2 (CSU): ARPC = leftmost 4 bytes of a Retail MAC over
    ARQC || CSU || [proprietary auth data] with the AC session key.
    Pass ``expected`` to verify the host/card value instead of generating.
    """
    sk, aq = _hx(session_key_hex), _hx(arqc_hex)
    if str(method) == "2":
        data = aq + _hx(csu_hex) + (_hx(proprietary_hex) if proprietary_hex else b"")
        value = retail_mac(session_key_hex, data.hex())["mac"][:8]
    else:
        padded = (_hx(arc_hex) + b"\x00" * 8)[:8]
        value = _ecb(sk).encrypt(_xor(aq, padded)).hex().upper()
    return _match({"arpc": value, "method": str(method)}, "arpc", expected)


# -- EMV key derivation (Option A ICC master key + Common Session Key) --------


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def emv_icc_mk(mdk_hex: str, pan: str, psn: str = "00") -> dict:
    """Derive the ICC Master Key (UDK) from the Issuer MDK — EMV Option A."""
    mdk = _hx(mdk_hex)
    digits = "".join(c for c in (pan + (psn or "00")) if c.isdigit())
    y = digits[-16:].rjust(16, "0")  # rightmost 16 digits, BCD (8 bytes)
    block = _hx(y)
    cipher = _ecb(mdk)
    zl = cipher.encrypt(block)
    zr = cipher.encrypt(_xor(block, b"\xff" * 8))
    return {"udk": (zl + zr).hex().upper()}


def emv_session_key(mk_hex: str, atc_hex: str) -> dict:
    """Derive the EMV Common Session Key (CVN 18 style) from a key + ATC."""
    mk = _hx(mk_hex)
    atc = _hx(atc_hex.zfill(4))[:2].rjust(2, b"\x00")
    cipher = _ecb(mk)
    skl = cipher.encrypt(atc + bytes([0xF0, 0x00, 0x00, 0x00, 0x00, 0x00]))
    skr = cipher.encrypt(atc + bytes([0x0F, 0x00, 0x00, 0x00, 0x00, 0x00]))
    return {"session_key": (skl + skr).hex().upper()}


# -- DUKPT (ANSI X9.24-1, TDES) ----------------------------------------------
#
# A terminal holds an Initial PIN Encryption Key (IPEK) derived from the
# acquirer Base Derivation Key (BDK) and its Key Serial Number (KSN). Each
# transaction it derives a fresh "future"/transaction key from the IPEK and the
# KSN counter, then XORs a variant mask to get the working PIN / MAC / data key.
# The host re-derives the identical key from BDK + KSN, which is how PIN/MAC
# translation works without the clear key ever leaving an HSM.

_DUKPT_KSN_COUNTER_BITS = 21
_DUKPT_REG_MASK = bytes.fromhex("C0C0C0C000000000C0C0C0C000000000")

#: Working-key variant masks applied to the derived transaction key.
DUKPT_VARIANTS = {
    "none": "00000000000000000000000000000000",
    "pin": "00000000000000FF00000000000000FF",
    "mac_req": "000000000000FF00000000000000FF00",
    "mac_resp": "00000000FF00000000000000FF000000",
    "data_req": "0000000000FF00000000000000FF0000",
}


def _tdes_key(k: bytes) -> bytes:
    """Normalise a DUKPT key to a 16-byte (double-length) value."""
    if len(k) == 8:
        return k + k
    return k[:16]


def _dukpt_encrypt_register(key16: bytes, reg8: bytes) -> bytes:
    top, bottom = key16[:8], key16[8:16]
    return _xor(_des_block(top, _xor(reg8, bottom), True), bottom)


def _dukpt_generate_key(key16: bytes, reg8: bytes) -> bytes:
    masked = _xor(key16, _DUKPT_REG_MASK)
    left = _dukpt_encrypt_register(masked, reg8)
    right = _dukpt_encrypt_register(key16, reg8)
    return left + right


def dukpt_ipek(bdk_hex: str, ksn_hex: str) -> dict:
    """Derive the Initial PIN Encryption Key from the BDK and (full) KSN."""
    bdk = _tdes_key(_hx(bdk_hex))
    ksn = _hx(ksn_hex).rjust(10, b"\x00")[-10:]
    mask = bytes.fromhex("FFFFFFFFFFFFFFE00000")
    base = bytes(a & b for a, b in zip(ksn, mask))
    data = base[:8]
    left = _ecb(bdk).encrypt(data)
    right = _ecb(_xor(bdk, _DUKPT_REG_MASK)).encrypt(data)
    ipek = left + right
    return {"ipek": ipek.hex().upper(), "kcv": kcv(ipek.hex())["kcv"]}


def _dukpt_transaction_key(ipek: bytes, ksn: bytes) -> bytes:
    full = int.from_bytes(ksn, "big")
    counter = full & ((1 << _DUKPT_KSN_COUNTER_BITS) - 1)
    reg = full & ~((1 << _DUKPT_KSN_COUNTER_BITS) - 1)
    cur = ipek
    shift = 1 << (_DUKPT_KSN_COUNTER_BITS - 1)
    while shift:
        if counter & shift:
            reg |= shift
            reg8 = (reg & ((1 << 64) - 1)).to_bytes(8, "big")
            cur = _dukpt_generate_key(cur, reg8)
        shift >>= 1
    return cur


def _dukpt_working_key(
    bdk_hex: str, ksn_hex: str, ipek_hex: str, variant: str
) -> tuple[bytes, bytes]:
    """Return (transaction_key, working_key) for ``variant``.

    Supply either ``bdk_hex`` (derive IPEK first) or ``ipek_hex`` directly."""
    if ipek_hex:
        ipek = _tdes_key(_hx(ipek_hex))
    else:
        ipek = _hx(dukpt_ipek(bdk_hex, ksn_hex)["ipek"])
    ksn = _hx(ksn_hex).rjust(10, b"\x00")[-10:]
    txn = _dukpt_transaction_key(ipek, ksn)
    mask_hex = DUKPT_VARIANTS.get(variant)
    if mask_hex is None:
        raise ValueError(f"unknown DUKPT variant '{variant}'")
    working = _xor(txn, _hx(mask_hex))
    if variant == "data_req":
        # data keys are additionally enciphered under themselves
        c = _ecb(working)
        working = c.encrypt(working[:8]) + c.encrypt(working[8:16])
    return txn, working


def dukpt_derive_key(
    bdk_hex: str = "", ksn_hex: str = "", variant: str = "pin", ipek_hex: str = ""
) -> dict:
    """Derive the DUKPT transaction key and its ``variant`` working key."""
    txn, working = _dukpt_working_key(bdk_hex, ksn_hex, ipek_hex, variant)
    return {
        "transaction_key": txn.hex().upper(),
        "key": working.hex().upper(),
        "variant": variant,
        "kcv": kcv(working.hex())["kcv"],
    }


def dukpt_pin_block(
    pin: str, pan: str, bdk_hex: str = "", ksn_hex: str = "", ipek_hex: str = ""
) -> dict:
    """Encrypt an ISO format-0 PIN block under the DUKPT PIN key for this KSN."""
    _, pek = _dukpt_working_key(bdk_hex, ksn_hex, ipek_hex, "pin")
    clear = bytes(a ^ b for a, b in zip(_pin_field(pin), _pan_field(pan)))
    enc = _ecb(pek).encrypt(clear)
    return {
        "pin_block": enc.hex().upper(),
        "pin_key": pek.hex().upper(),
        "ksn": "".join((ksn_hex or "").split()).upper(),
    }


# -- Visa PVV (PIN Verification Value, IBM 3624 decimalisation) ---------------


def pvv(pan: str, pin: str, pvki: str, pvk_hex: str) -> dict:
    """Compute the 4-digit Visa PVV from PAN, PIN, key index and the PVK."""
    digits = "".join(c for c in pan if c.isdigit())
    eleven = digits[:-1][-11:].rjust(11, "0")  # 11 rightmost PAN digits, sans check digit
    tsp = eleven + (str(pvki)[:1] or "0") + "".join(c for c in pin if c.isdigit())[:4]
    block = _ecb(_hx(pvk_hex)).encrypt(_hx(tsp))
    hexstr = block.hex().upper()
    out = [c for c in hexstr if c.isdigit()]
    if len(out) < 4:  # second pass: map A-F -> 0-5
        out += [str(int(c, 16) - 10) for c in hexstr if not c.isdigit()]
    return {"pvv": "".join(out)[:4]}


# -- IBM 3624 PIN offset (natural PIN + offset) ------------------------------
#
# The 3624 method derives a "natural" (intermediate) PIN by enciphering the PIN
# validation data under the PVK and decimalising the ciphertext through a
# decimalisation table. The customer-chosen PIN is stored as an *offset* —
# offset = PIN - natural, digit-wise, modulo 10, no borrow. Verification
# re-derives the natural PIN and checks natural + offset == entered PIN.

#: IBM default decimalisation table: 0-9 map to themselves, A-F map to 0-5.
DEFAULT_DEC_TABLE = "0123456789012345"
_HEX_DIGITS = "0123456789ABCDEF"


def _decimalise(hexstr: str, table: str = DEFAULT_DEC_TABLE) -> str:
    """Map each hex digit through a 16-entry decimalisation ``table``."""
    table = (table or DEFAULT_DEC_TABLE)[:16].ljust(16, "0")
    mapping = {_HEX_DIGITS[i]: table[i] for i in range(16)}
    return "".join(mapping[c] for c in hexstr.upper())


def ibm3624_pin(
    validation_data: str, pvk_hex: str, dec_table: str = DEFAULT_DEC_TABLE, pin_len: int = 4
) -> dict:
    """Derive the IBM 3624 natural PIN from ``validation_data`` under the PVK."""
    block = _ecb(_hx(pvk_hex)).encrypt(_hx(validation_data.ljust(16, "0")[:16]))
    natural = _decimalise(block.hex().upper(), dec_table)
    return {"natural_pin": natural[: max(1, pin_len)], "natural_full": natural}


def ibm3624_offset(
    validation_data: str, pvk_hex: str, pin: str, dec_table: str = DEFAULT_DEC_TABLE
) -> dict:
    """Compute the 3624 PIN offset = PIN - natural (digit-wise, mod 10)."""
    pin = "".join(c for c in pin if c.isdigit())
    natural = ibm3624_pin(validation_data, pvk_hex, dec_table, len(pin))["natural_pin"]
    offset = "".join(str((int(p) - int(n)) % 10) for p, n in zip(pin, natural))
    return {"offset": offset, "natural_pin": natural}


def ibm3624_verify(
    validation_data: str,
    pvk_hex: str,
    offset: str,
    pin: str,
    dec_table: str = DEFAULT_DEC_TABLE,
) -> dict:
    """Verify an entered ``pin`` against a stored 3624 ``offset``."""
    offset = "".join(c for c in offset if c.isdigit())
    pin = "".join(c for c in pin if c.isdigit())
    natural = ibm3624_pin(validation_data, pvk_hex, dec_table, len(offset))["natural_pin"]
    derived = "".join(str((int(n) + int(o)) % 10) for n, o in zip(natural, offset))
    return {"derived_pin": derived, "match": bool(derived) and derived == pin}


# -- AES primitives ----------------------------------------------------------


def _aes_ecb(key: bytes):
    from Crypto.Cipher import AES

    return AES.new(key, AES.MODE_ECB)


def aes(
    key_hex: str, data_hex: str, op: str = "encrypt", mode: str = "ecb", iv_hex: str = ""
) -> dict:
    """Generic AES-128/192/256 in ECB or CBC (hex in / hex out)."""
    from Crypto.Cipher import AES

    key, data = _hx(key_hex), _hx(data_hex)
    iv = _hx(iv_hex) if iv_hex else b"\x00" * 16
    cipher = AES.new(key, AES.MODE_CBC, iv) if mode == "cbc" else AES.new(key, AES.MODE_ECB)
    out = cipher.encrypt(data) if op == "encrypt" else cipher.decrypt(data)
    return {"result": out.hex().upper()}


# -- ISO 9564-1 format 4 PIN block (AES) -------------------------------------
#
# Format 4 is a 16-byte AES block. The clear PIN field (control nibble 4, PIN
# length, PIN, 'A' fill, then 8 random bytes) is AES-enciphered, XORed with a
# PAN field (a length indicator + the PAN, zero-filled to 16 bytes), then
# AES-enciphered again. Decode reverses it. This is the ISO 9564-1:2017
# construction (verified against the psec reference implementation).


def _iso4_pin_field(pin: str) -> bytes:
    pin = "".join(c for c in pin if c.isdigit())
    if not 4 <= len(pin) <= 12:
        raise ValueError("ISO-4 PIN must be 4–12 digits")
    body = "4" + format(len(pin), "X") + pin + "A" * (14 - len(pin))
    return _hx(body + secrets.token_hex(8).upper())


def _iso4_pan_field(pan: str) -> bytes:
    digits = "".join(c for c in pan if c.isdigit()) or "0"
    field = (str(max(0, len(digits) - 12)) + digits.rjust(12, "0")).ljust(32, "0")
    return _hx(field[:32])


def pin_block_iso4_encode(pin: str, pan: str, key_hex: str) -> dict:
    key = _hx(key_hex)
    a = _aes_ecb(key).encrypt(_iso4_pin_field(pin))
    b = bytes(x ^ y for x, y in zip(a, _iso4_pan_field(pan)))
    block = _aes_ecb(key).encrypt(b)
    return {"pin_block": block.hex().upper(), "format": "4"}


def pin_block_iso4_decode(pin_block_hex: str, pan: str, key_hex: str) -> dict:
    key = _hx(key_hex)
    b = _aes_ecb(key).decrypt(_hx(pin_block_hex))
    a = bytes(x ^ y for x, y in zip(b, _iso4_pan_field(pan)))
    field = _aes_ecb(key).decrypt(a).hex().upper()
    pin = ""
    if field[:1] == "4":
        try:
            length = int(field[1], 16)
            if 4 <= length <= 12:
                pin = field[2 : 2 + length]
        except (ValueError, IndexError):
            pin = ""
    return {"pin": pin, "format": "4", "clear_field": field}


# -- AES-CMAC (NIST SP 800-38B / RFC 4493) -----------------------------------


def _aes_cmac_subkeys(key: bytes) -> tuple[bytes, bytes]:
    def _dbl(b: bytes) -> bytes:
        i = (int.from_bytes(b, "big") << 1) & ((1 << 128) - 1)
        if b[0] & 0x80:
            i ^= 0x87  # Rb for a 128-bit block
        return i.to_bytes(16, "big")

    l0 = _aes_ecb(key).encrypt(b"\x00" * 16)
    k1 = _dbl(l0)
    k2 = _dbl(k1)
    return k1, k2


def aes_cmac(key_hex: str, data_hex: str) -> dict:
    """AES-CMAC over ``data`` under ``key`` (full 16-byte tag + 8-byte prefix)."""
    key, msg = _hx(key_hex), _hx(data_hex)
    k1, k2 = _aes_cmac_subkeys(key)
    if len(msg) and len(msg) % 16 == 0:  # last block complete -> XOR K1
        head, last = msg[:-16], bytes(x ^ y for x, y in zip(msg[-16:], k1))
    else:  # pad 0x80 00.. and XOR K2
        rem = msg[(len(msg) // 16) * 16 :]
        padded = rem + b"\x80" + b"\x00" * (15 - len(rem))
        head, last = msg[: len(msg) - len(rem)], bytes(x ^ y for x, y in zip(padded, k2))
    x = b"\x00" * 16
    for i in range(0, len(head), 16):
        x = _aes_ecb(key).encrypt(bytes(a ^ b for a, b in zip(x, head[i : i + 16])))
    tag = _aes_ecb(key).encrypt(bytes(a ^ b for a, b in zip(x, last)))
    return {"mac": tag.hex().upper(), "mac8": tag.hex().upper()[:16]}


# -- AES DUKPT (ANSI X9.24-3) ------------------------------------------------
#
# X9.24-3 replaces the TDES DUKPT key-derivation with an AES key-derivation
# function (KDF): each derived key is AES-ECB(derivation_key, derivation_data),
# where the 16-byte derivation data encodes the key usage, algorithm, key
# length and either the initial-key id (for the initial key) or the transaction
# counter (for working keys). The 12-byte KSN is an 8-byte initial-key id plus a
# 4-byte transaction counter. Verified against the published X9.24-3 initial-key
# vector (BDK FEDCBA9876543210F1F1F1F1F1F1F1F1 + id 1234567890123456).

#: Key-usage indicators (2 bytes).
_AKU_PIN = 0x1000
_AKU_MAC_GEN = 0x2000
_AKU_MAC_VER = 0x2001
_AKU_DATA_ENC = 0x3000
_AKU_DATA_DEC = 0x3001
_AKU_KEY_DERIVATION = 0x8000
_AKU_INITIAL_KEY = 0x8001

#: Working-key variant -> key-usage indicator.
AES_DUKPT_USAGES = {
    "pin": _AKU_PIN,
    "mac_gen": _AKU_MAC_GEN,
    "mac_ver": _AKU_MAC_VER,
    "mac": _AKU_MAC_GEN,
    "data_enc": _AKU_DATA_ENC,
    "data_dec": _AKU_DATA_DEC,
    "data": _AKU_DATA_ENC,
}

#: AES key length (bytes) -> algorithm indicator (2 bytes).
_AES_ALGO = {16: 0x0002, 24: 0x0003, 32: 0x0004}


def _aes_dukpt_dd(key_usage: int, algo: int, key_bits: int, id8: bytes, counter: int) -> bytearray:
    dd = bytearray(16)
    dd[0] = 0x01  # version
    dd[1] = 0x01  # KDF block counter (bumped per output block)
    dd[2:4] = key_usage.to_bytes(2, "big")
    dd[4:6] = algo.to_bytes(2, "big")
    dd[6:8] = key_bits.to_bytes(2, "big")
    if key_usage == _AKU_INITIAL_KEY:
        dd[8:16] = id8[:8]
    else:  # working / intermediate: rightmost 4 bytes of id + 32-bit counter
        dd[8:12] = id8[4:8]
        dd[12:16] = counter.to_bytes(4, "big")
    return dd


def _aes_dukpt_kdf(derivation_key: bytes, dd: bytearray) -> bytes:
    """The X9.24-3 KDF: enough AES-ECB blocks to fill the derived key length."""
    key_bytes = int.from_bytes(dd[6:8], "big") // 8
    out = b""
    block_ctr = 1
    while len(out) < key_bytes:
        dd[1] = block_ctr
        out += _aes_ecb(derivation_key).encrypt(bytes(dd))
        block_ctr += 1
    return out[:key_bytes]


def aes_dukpt_initial_key(bdk_hex: str, initial_key_id_hex: str) -> dict:
    """Derive the Initial Key (IK) from the BDK and the 8-byte initial-key id."""
    bdk = _hx(bdk_hex)
    id8 = _hx(initial_key_id_hex).rjust(8, b"\x00")[:8]
    algo = _AES_ALGO[len(bdk)]
    dd = _aes_dukpt_dd(_AKU_INITIAL_KEY, algo, len(bdk) * 8, id8, 0)
    ik = _aes_dukpt_kdf(bdk, dd)
    return {"ik": ik.hex().upper()}


def _aes_dukpt_transaction_key(ik: bytes, id8: bytes, counter: int) -> bytes:
    """Walk the set bits of the transaction counter, deriving forward keys."""
    algo = _AES_ALGO[len(ik)]
    key = ik
    working = 0
    mask = 0x80000000
    while mask:
        if counter & mask:
            working |= mask
            dd = _aes_dukpt_dd(_AKU_KEY_DERIVATION, algo, len(ik) * 8, id8, working)
            key = _aes_dukpt_kdf(key, dd)
        mask >>= 1
    return key


def aes_dukpt_derive_key(
    ksn_hex: str, variant: str = "pin", bdk_hex: str = "", ik_hex: str = ""
) -> dict:
    """Derive an AES DUKPT working key for ``ksn``.

    Supply the BDK (the initial key is derived first) or the initial key
    directly. ``variant`` selects the key usage (pin/mac_gen/mac_ver/data_enc/…).
    """
    ksn = _hx(ksn_hex)
    id8, counter = ksn[:8], int.from_bytes(ksn[8:12].rjust(4, b"\x00"), "big")
    if ik_hex:
        ik = _hx(ik_hex)
    else:
        ik = _hx(aes_dukpt_initial_key(bdk_hex, id8.hex())["ik"])
    txn_key = _aes_dukpt_transaction_key(ik, id8, counter)
    usage = AES_DUKPT_USAGES.get(variant, _AKU_PIN)
    algo = _AES_ALGO[len(ik)]
    dd = _aes_dukpt_dd(usage, algo, len(ik) * 8, id8, counter)
    working = _aes_dukpt_kdf(txn_key, dd)
    return {
        "key": working.hex().upper(),
        "transaction_key": txn_key.hex().upper(),
        "ik": ik.hex().upper(),
        "variant": variant,
    }


def aes_dukpt_pin_block(
    pin: str, pan: str, ksn_hex: str, bdk_hex: str = "", ik_hex: str = ""
) -> dict:
    """Encipher an ISO-4 PIN block under the AES DUKPT PIN key for this KSN."""
    pek = aes_dukpt_derive_key(ksn_hex, "pin", bdk_hex=bdk_hex, ik_hex=ik_hex)["key"]
    out = pin_block_iso4_encode(pin, pan, pek)
    return {"pin_block": out["pin_block"], "pin_key": pek, "ksn": ksn_hex.upper()}


# -- dispatcher --------------------------------------------------------------

OPERATIONS = {
    "des": lambda p: des(
        p.get("key", ""),
        p.get("data", ""),
        p.get("cipher_op", "encrypt"),
        p.get("cipher_mode", "ecb"),
        p.get("iv", ""),
    ),
    "kcv": lambda p: kcv(p.get("key", "")),
    "pin_block_encode": lambda p: pin_block_encode(
        p.get("pin", ""), p.get("pan", ""), p.get("key", ""), p.get("format", p.get("fmt", "0"))
    ),
    "pin_block_decode": lambda p: pin_block_decode(
        p.get("pin_block", ""),
        p.get("pan", ""),
        p.get("key", ""),
        p.get("format", p.get("fmt", "0")),
    ),
    "retail_mac": lambda p: retail_mac(p.get("key", ""), p.get("data", "")),
    "cvv": lambda p: cvv(
        p.get("pan", ""), p.get("expiry", ""), p.get("service_code", ""), p.get("key", "")
    ),
    "arqc": lambda p: arqc(p.get("key", ""), p.get("data", ""), p.get("expected", "")),
    "arpc": lambda p: arpc(
        p.get("key", ""),
        p.get("arqc", ""),
        p.get("arc", ""),
        p.get("csu", ""),
        p.get("proprietary", ""),
        p.get("arpc_method", "1"),
        p.get("expected", ""),
    ),
    "emv_icc_mk": lambda p: emv_icc_mk(p.get("key", ""), p.get("pan", ""), p.get("psn", "00")),
    "emv_session_key": lambda p: emv_session_key(p.get("key", ""), p.get("atc", "")),
    "dukpt_ipek": lambda p: dukpt_ipek(p.get("bdk", p.get("key", "")), p.get("ksn", "")),
    "dukpt_derive_key": lambda p: dukpt_derive_key(
        p.get("bdk", p.get("key", "")),
        p.get("ksn", ""),
        p.get("variant", "pin"),
        p.get("ipek", ""),
    ),
    "dukpt_pin_block": lambda p: dukpt_pin_block(
        p.get("pin", ""),
        p.get("pan", ""),
        p.get("bdk", p.get("key", "")),
        p.get("ksn", ""),
        p.get("ipek", ""),
    ),
    "pvv": lambda p: pvv(
        p.get("pan", ""), p.get("pin", ""), p.get("pvki", "0"), p.get("key", p.get("pvk", ""))
    ),
    "ibm3624_pin": lambda p: ibm3624_pin(
        p.get("validation_data", p.get("pan", "")),
        p.get("key", p.get("pvk", "")),
        p.get("dec_table", DEFAULT_DEC_TABLE),
        int(p.get("pin_len", 4)),
    ),
    "ibm3624_offset": lambda p: ibm3624_offset(
        p.get("validation_data", p.get("pan", "")),
        p.get("key", p.get("pvk", "")),
        p.get("pin", ""),
        p.get("dec_table", DEFAULT_DEC_TABLE),
    ),
    "ibm3624_verify": lambda p: ibm3624_verify(
        p.get("validation_data", p.get("pan", "")),
        p.get("key", p.get("pvk", "")),
        p.get("offset", ""),
        p.get("pin", ""),
        p.get("dec_table", DEFAULT_DEC_TABLE),
    ),
    "aes": lambda p: aes(
        p.get("key", ""),
        p.get("data", ""),
        p.get("cipher_op", "encrypt"),
        p.get("cipher_mode", "ecb"),
        p.get("iv", ""),
    ),
    "aes_cmac": lambda p: aes_cmac(p.get("key", ""), p.get("data", "")),
    "aes_dukpt_initial_key": lambda p: aes_dukpt_initial_key(
        p.get("bdk", p.get("key", "")), p.get("initial_key_id", p.get("ikid", ""))
    ),
    "aes_dukpt_derive_key": lambda p: aes_dukpt_derive_key(
        p.get("ksn", ""),
        p.get("variant", "pin"),
        p.get("bdk", p.get("key", "")),
        p.get("ipek", p.get("ik", "")),
    ),
    "aes_dukpt_pin_block": lambda p: aes_dukpt_pin_block(
        p.get("pin", ""),
        p.get("pan", ""),
        p.get("ksn", ""),
        p.get("bdk", p.get("key", "")),
        p.get("ipek", p.get("ik", "")),
    ),
}


def run_crypto(operation: str, params: dict[str, Any]) -> dict:
    """Run a crypto ``operation`` with string ``params`` (already ref-resolved)."""
    fn = OPERATIONS.get(operation)
    if fn is None:
        return {"error": f"unknown crypto operation '{operation}'"}
    try:
        return fn(params)
    except ImportError:
        return {"error": "pycryptodome is not installed in this runtime"}
    except (ValueError, KeyError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
