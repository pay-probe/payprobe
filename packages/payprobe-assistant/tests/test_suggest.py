"""LLM label suggestions: grounding, parsing, config + outage fallbacks."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from assistant_service import rest, suggest
from assistant_service.main import app

QUEUE = [
    {"signature": "sig-1", "sample_text": "unresolved reference: '<n>.key'",
     "count": 12, "current": {"label": "timeout", "confidence": 0.23,
                              "novel": False}},
    {"signature": "sig-2", "sample_text": "assert error_code eq '<n>' -> none",
     "count": 7, "current": {"label": "connection_reset", "confidence": 0.21,
                             "novel": False}},
]


def test_parse_suggestions_tolerates_shapes():
    ok = suggest._parse_suggestions(
        '[{"label": "unresolved-reference", "reason": "r"}, "assertion-mismatch"]',
        2)
    assert ok == [{"label": "unresolved-reference", "reason": "r"},
                  {"label": "assertion-mismatch", "reason": ""}]
    short = suggest._parse_suggestions('[{"label": "a"}]', 2)
    assert short[1] == {"label": "", "reason": ""}
    assert suggest._parse_suggestions("prose", 1) == [{"label": "", "reason": ""}]


def test_suggest_endpoint_needs_llm(monkeypatch):
    monkeypatch.delenv("ASSIST_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    out = c.post("/insights/suggest-labels", json={}).json()
    assert out.get("needs_config") is True


def test_suggest_endpoint_grounded_roundtrip(monkeypatch):
    monkeypatch.setenv("ASSIST_LLM_API_KEY", "test-key")
    seen: dict = {}

    def fake_request(method, url, body=None):
        if "/insights/label-queue" in url:
            return {"queue": QUEUE, "shapes_total": 2}
        if url.endswith("/datasets"):
            return {"datasets": [{"labels": {"timeout": 6, "dns_error": 6}}]}
        if "/insights/categories" in url:
            return {"categories": [{"label": "hsm-socket-drop"}]}
        raise AssertionError(f"unexpected url {url}")

    def fake_post_json(url, headers, body):
        seen["body"] = body
        return {"choices": [{"message": {"content": json.dumps([
            {"label": "unresolved-reference", "reason": "ref not resolved"},
            {"label": "timeout", "reason": "matches existing"},
        ])}}]}

    monkeypatch.setattr(rest, "request", fake_request)
    monkeypatch.setattr(rest, "post_json", fake_post_json)
    c = TestClient(app)
    out = c.post("/insights/suggest-labels", json={"limit": 8}).json()
    assert out["advisory_only"] is True
    assert out["suggestions"][0] == {
        "signature": "sig-1", "sample_text": "unresolved reference: '<n>.key'",
        "label": "unresolved-reference", "reason": "ref not resolved",
        "is_new": True}
    assert out["suggestions"][1]["label"] == "timeout"
    assert out["suggestions"][1]["is_new"] is False
    # grounding: the prompt carries the shapes + taxonomy, nothing else
    prompt = json.dumps(seen["body"])
    assert "unresolved reference" in prompt and "hsm-socket-drop" in prompt


def test_suggest_endpoint_survives_insight_service_down(monkeypatch):
    monkeypatch.setenv("ASSIST_LLM_API_KEY", "test-key")

    def boom(method, url, body=None):
        raise RuntimeError("cannot reach insight-service: refused")

    monkeypatch.setattr(rest, "request", boom)
    c = TestClient(app)
    out = c.post("/insights/suggest-labels", json={}).json()
    assert out["suggestions"] == [] and "refused" in out["error"]
