"""The ASCII ISO 8583 codec in a form that can be pasted into a ``code`` step.

Code steps run in ``python -I`` with an empty environment (see
``worker/engine/code_runner.py``), so they cannot import this package. The
scenario-service catalog therefore pastes the *source* of the functions below
into its ``iso8583_pack`` / ``iso8583_parse`` templates (``ISO8583_CODEC_SRC``).
This module is that paste: pure functions, no imports, ASCII profile only.
``test_iso_catalog_codec_parity`` asserts it packs and unpacks the golden set
exactly like :func:`payprobe_common.iso8583.pack` / :func:`unpack` under the
ASCII profile, so the two can never drift apart unnoticed.

``iso_unpack`` trims only leading/trailing whitespace: text fields (DE 43, 63) may
legitimately contain spaces, so the historical "strip every space" convenience of
the old paste mangled real messages. The worker keeps that convenience only in its
legacy ``str`` helper; the wire path uses the bytes codec, never this one.
"""

# --- paste starts here -------------------------------------------------------


def _bits_from_hex(h):
    n = int(h, 16)
    w = len(h) * 4
    return {i + 1 for i in range(w) if n & (1 << (w - 1 - i))}


def _bitmap(des, width):
    n = 0
    for d in des:
        n |= 1 << (width - d)
    return format(n, f"0{width // 4}X")


def iso_unpack(msg, FIELDS):
    msg = msg.strip()
    pos = 0
    mti = msg[0:4]
    pos = 4
    present = _bits_from_hex(msg[pos : pos + 16])
    pos += 16
    if 1 in present:
        present |= {b + 64 for b in _bits_from_hex(msg[pos : pos + 16])}
        pos += 16
        present.discard(1)
    fields = {}
    for de in sorted(present):
        sp = FIELDS.get(str(de))
        if not sp:
            fields[str(de)] = {"name": "(unknown)", "value": "", "error": "DE not in spec"}
            break
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            ln = int(sp.get("length", 0))
            v = msg[pos : pos + ln]
            pos += ln
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            ln = int(msg[pos : pos + pw])
            pos += pw
            v = msg[pos : pos + ln]
            pos += ln
        fields[str(de)] = {"name": sp.get("name", ""), "value": v}
    return {"mti": mti, "de_list": sorted(present), "fields": fields}


def iso_pack(mti, values, FIELDS):
    values = {str(k): str(v) for k, v in values.items()}
    des = sorted(int(d) for d in values)
    has_sec = any(d > 64 for d in des)
    primary = {d for d in des if d <= 64}
    if has_sec:
        primary.add(1)
    out = mti + _bitmap(primary, 64)
    if has_sec:
        out += _bitmap({d - 64 for d in des if d > 64}, 64)
    for d in des:
        sp = FIELDS[str(d)]
        v = values[str(d)]
        lt = sp.get("len_type", "fixed")
        if lt == "fixed":
            out += v
        else:  # llvar(2)/lllvar(3)/llllvar(4)/lllllvar(5) length prefix
            pw = {"llvar": 2, "lllvar": 3, "llllvar": 4, "lllllvar": 5}.get(lt, 3)
            out += str(len(v)).zfill(pw) + v
    return out


# --- paste ends here ---------------------------------------------------------


def codec_source() -> str:
    """The source text between the paste markers, for the catalog templates."""
    import inspect

    src = inspect.getsource(inspect.getmodule(codec_source))
    start = src.index("# --- paste starts here")
    start = src.index("\n", start) + 1
    end = src.index("# --- paste ends here")
    return src[start:end].strip("\n") + "\n"


__all__ = ["codec_source", "iso_pack", "iso_unpack"]
