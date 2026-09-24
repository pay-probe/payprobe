"""ADR-0011: the ISO 8583 codec pasted into the catalog's code steps is generated
from ``payprobe_common.iso8583.portable`` and stays byte-identical to the shared
codec under the ASCII profile. Code steps run in ``python -I`` with no
``PYTHONPATH``, so the paste cannot become an import; this test is what keeps the
two from drifting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from models import iso_catalog
from models.message_format import BUILTIN_FORMATS

from payprobe_common import iso8583 as shared
from payprobe_common.iso8583 import portable

_GOLDEN = Path(__file__).resolve().parents[2] / "worker" / "tests" / "fixtures" / "iso8583_golden.json"
GOLDEN = json.loads(_GOLDEN.read_text())


def _pasted_namespace() -> dict:
    """Execute the paste the way a code step would: bare namespace, no imports."""
    ns: dict = {}
    exec(iso_catalog.ISO8583_CODEC_SRC, ns)  # noqa: S102 - trusted module constant
    return ns


def test_paste_is_generated_from_the_portable_module_and_import_free():
    assert iso_catalog.ISO8583_CODEC_SRC == portable.codec_source()
    assert "import " not in iso_catalog.ISO8583_CODEC_SRC
    for name in ("iso_pack", "iso_unpack", "_bitmap", "_bits_from_hex"):
        assert f"def {name}(" in iso_catalog.ISO8583_CODEC_SRC


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["name"] for c in GOLDEN["cases"]])
def test_pasted_codec_matches_shared_codec_under_ascii(case):
    ns = _pasted_namespace()
    fields = shared.BUILTIN_TABLES[case["format"]]
    text = ns["iso_pack"](case["mti"], case["values"], fields)
    assert text.encode("ascii").hex().upper() == case["wire"]["ascii"]
    pasted = ns["iso_unpack"](text, fields)
    ours = shared.unpack(text.encode("ascii"), fields, "ascii")
    assert pasted["mti"] == ours["mti"]
    assert pasted["de_list"] == ours["de_list"]
    assert {k: v["value"] for k, v in pasted["fields"].items()} == {
        k: v["value"] for k, v in ours["fields"].items()
    }


def test_builtin_formats_and_step_defaults_use_the_shared_tables():
    by_id = {f.id: f for f in BUILTIN_FORMATS}
    assert by_id["iso8583-1987"].definition["fields"] == shared.ISO8583_1987
    assert by_id["iso8583-1993"].definition["fields"] == shared.ISO8583_1993
    assert by_id["visa-base1"].definition["fields"] == shared.VISA_BASE_I
    # builtins are copies: editing a registry document must not touch the shared table
    assert by_id["iso8583-1987"].definition["fields"] is not shared.ISO8583_1987
    assert json.loads(iso_catalog._FIELDS_1987_JSON) == shared.ISO8583_1987
    encodings = {f.id: f.definition["encoding"] for f in BUILTIN_FORMATS if f.protocol == "iso8583"}
    assert encodings == {"iso8583-1987": "ascii", "iso8583-1993": "ascii",
                         "visa-base1": "ascii", "iso8583-binary": "binary"}
    assert by_id["iso8583-binary"].definition["fields"] == shared.ISO8583_1987
