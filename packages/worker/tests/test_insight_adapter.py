"""The `insight` predict-step adapter: dispatch, payload shaping, failure modes."""

from __future__ import annotations

import json

import pytest

from worker.adapters.insight import adapter as mod
from worker.adapters.insight.adapter import InsightAdapter
from worker.adapters.registry import ADAPTER_MAP


def _fake_urlopen(monkeypatch, responder):
    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        return FakeResp(json.dumps(responder(req)).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)


def test_adapter_is_registered():
    assert ADAPTER_MAP["insight"] is InsightAdapter


@pytest.mark.asyncio
async def test_categorize_roundtrip(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()
    seen = {}

    def responder(req):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return {
            "kind": "categorize",
            "category": "timeout",
            "label": "timeout",
            "confidence": 1.0,
            "novel": False,
        }

    _fake_urlopen(monkeypatch, responder)
    result = await a.execute("categorize", {"text": "read timed out"})
    assert result.success
    assert seen["url"] == "http://insight:8500/infer"
    assert seen["body"] == {"kind": "categorize", "text": "read timed out"}
    # the fields downstream steps reference as ${step.response.*}
    assert result.response_payload["category"] == "timeout"
    assert result.response_payload["novel"] is False


@pytest.mark.asyncio
async def test_predict_outcome_roundtrip(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()

    def responder(req):
        body = json.loads(req.data.decode())
        assert body == {"kind": "predict_outcome", "scenario_id": "sc-42", "environment": "staging"}
        return {
            "kind": "predict_outcome",
            "p_fail_next": 0.71,
            "p_flaky": 0.2,
            "basis": "frequency",
            "n_history": 12,
        }

    _fake_urlopen(monkeypatch, responder)
    result = await a.execute("predict_outcome", {"scenario_id": "sc-42", "environment": "staging"})
    assert result.success and result.response_payload["p_fail_next"] == 0.71


@pytest.mark.asyncio
async def test_explain_and_model_pinning(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()
    seen = {}

    def responder(req):
        seen["body"] = json.loads(req.data.decode())
        return {
            "kind": "explain",
            "category": "timeout",
            "why": "…",
            "fix": "…",
            "explanation": "Category: timeout…",
        }

    _fake_urlopen(monkeypatch, responder)
    r = await a.execute("explain", {"text": "timed out", "model_id": "cm-1"})
    assert r.success and r.response_payload["explanation"]
    assert seen["body"] == {"kind": "explain", "text": "timed out", "model_id": "cm-1"}


@pytest.mark.asyncio
async def test_model_status_flattens_for_assertions(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()

    def responder(req):
        assert req.get_method() == "GET" and req.full_url.endswith("/status")
        return {
            "corpus_size": 42,
            "sklearn_available": True,
            "categorizer": {"learned": True, "custom": {"id": "cm-1", "name": "mine"}},
            "predictor": {"learned": False},
        }

    _fake_urlopen(monkeypatch, responder)
    r = await a.execute("model_status", {})
    p = r.response_payload
    assert p == {
        "corpus_size": 42,
        "custom_model_active": True,
        "custom_model": "mine",
        "cluster_model_active": True,
        "predictor_learned": False,
        "sklearn_available": True,
    }


@pytest.mark.asyncio
async def test_train_action(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()

    def responder(req):
        assert req.full_url.endswith("/train")
        return {"sync": {"runs_ingested": 2}, "train": {"fitted": False}}

    _fake_urlopen(monkeypatch, responder)
    r = await a.execute("train", {})
    assert r.success and r.response_payload["sync"]["runs_ingested"] == 2


@pytest.mark.asyncio
async def test_missing_params_fail_clean():
    a = InsightAdapter({"base_url": "http://insight:8500"})
    await a.connect()
    r1 = await a.execute("categorize", {})
    assert not r1.success and "text" in r1.error
    r2 = await a.execute("predict_outcome", {})
    assert not r2.success and "scenario_id" in r2.error
    r3 = await a.execute("nonsense", {"x": 1})
    assert not r3.success and "unknown insight action" in r3.error


@pytest.mark.asyncio
async def test_unreachable_service_fails_clean(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500", "response_timeout_sec": 0.1})
    await a.connect()

    def boom(req, timeout=0):
        raise mod.urllib.error.URLError("refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    r = await a.execute("categorize", {"text": "x"})
    assert not r.success and "optional deployment" in r.error
    assert await a.health_check() is False


@pytest.mark.asyncio
async def test_connection_token_wins(monkeypatch):
    a = InsightAdapter({"base_url": "http://insight:8500", "token": "conn-tok"})
    await a.connect()
    monkeypatch.setenv("API_TOKEN", "env-tok")
    headers = {}

    def responder(req):
        headers.update(req.headers)
        return {"kind": "categorize", "category": "error"}

    _fake_urlopen(monkeypatch, responder)
    await a.execute("categorize", {"text": "x"})
    assert headers.get("Authorization") == "Bearer conn-tok"
