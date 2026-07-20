"""Orchestrator wiring: named test-data pools -> inline cards/terminals."""
import pytest

from orchestrator.api import main as m


def _stub(monkeypatch, *, cards=None, terminals=None, fail=False):
    async def fake_get(url):
        if fail:
            raise RuntimeError("service down")
        if "/card-pools/" in url:
            return {"name": "visa_goods", "cards": cards or []}
        if "/terminal-pools/" in url:
            return {"name": "lane", "terminals": terminals or []}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(m, "_http_get_json", fake_get)


async def test_resolves_pools_into_inline_lists(monkeypatch):
    _stub(monkeypatch, cards=["4111111111111111", "4111111111111129"],
          terminals=["TERM0001"])
    scs = [{"id": "sc", "card_pool": "visa_goods", "terminal_pool": "lane"}]
    await m._attach_test_data(scs)
    assert scs[0]["cards"] == ["4111111111111111", "4111111111111129"]
    assert scs[0]["terminals"] == ["TERM0001"]


async def test_inline_values_win_over_named_pool(monkeypatch):
    _stub(monkeypatch, cards=["FROM_POOL"])
    scs = [{"id": "sc", "card_pool": "visa_goods", "cards": ["INLINE"]}]
    await m._attach_test_data(scs)
    assert scs[0]["cards"] == ["INLINE"]  # not overwritten


async def test_no_pool_reference_is_a_noop(monkeypatch):
    called = False

    async def fake_get(url):  # pragma: no cover - must not run
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(m, "_http_get_json", fake_get)
    scs = [{"id": "sc"}]
    await m._attach_test_data(scs)
    assert called is False and "cards" not in scs[0]


async def test_missing_service_is_best_effort(monkeypatch):
    _stub(monkeypatch, fail=True)
    scs = [{"id": "sc", "card_pool": "visa_goods"}]
    await m._attach_test_data(scs)  # must not raise
    assert "cards" not in scs[0]


# -- ${key.NAME}: crypto keys by registry name --------------------------------

def _stub_keys(monkeypatch, materials: dict, *, fail=False):
    async def fake_get(url):
        if fail:
            raise RuntimeError("service down")
        for name, value in materials.items():
            if url.endswith(f"/test-data/keys/{name}/material"):
                return {"name": name, "value": value}
        raise RuntimeError("no such key")

    monkeypatch.setattr(m, "_http_get_json", fake_get)


async def test_key_tokens_resolve_everywhere(monkeypatch):
    _stub_keys(monkeypatch, {"visa_cvk": "0123456789ABCDEF0123456789ABCDEF"})
    scs = [{
        "id": "sc",
        "steps": [{
            "id": "s1", "target": "crypto", "action": "cvv",
            "payload": {"cvk_hex": "${key.visa_cvk}",
                        "note": "uses ${key.visa_cvk} twice"},
        }],
    }]
    await m._attach_test_data(scs)
    payload = scs[0]["steps"][0]["payload"]
    assert payload["cvk_hex"] == "0123456789ABCDEF0123456789ABCDEF"
    assert payload["note"].count("0123456789ABCDEF0123456789ABCDEF") == 1


async def test_unknown_key_token_stays_literal(monkeypatch):
    _stub_keys(monkeypatch, {})  # registry has no such key
    scs = [{"id": "sc", "steps": [{"payload": {"k": "${key.missing}"}}]}]
    await m._attach_test_data(scs)  # must not raise
    assert scs[0]["steps"][0]["payload"]["k"] == "${key.missing}"


async def test_key_service_down_is_best_effort(monkeypatch):
    _stub_keys(monkeypatch, {"any": "x"}, fail=True)
    scs = [{"id": "sc", "payload": {"k": "${key.any}"}}]
    await m._attach_test_data(scs)  # must not raise
    assert scs[0]["payload"]["k"] == "${key.any}"
