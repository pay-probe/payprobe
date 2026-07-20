"""Phase-2 explain endpoint: grounding, parsing, and config fallbacks."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from assistant_service import explain, rest
from assistant_service.main import app

FAILURE = {
    "scenario_id": "sc-hsm", "scenario_name": "hsm scenario", "step_id": "s1",
    "target": "hsm", "label": "hsm-socket-drop", "novel": True,
    "evidence": {
        "help": {"what": "Transactions errored.", "fix": "See the trace."},
        "history": {"kind": "always_failing",
                    "text": "fails every recorded run (3)"},
        "similar_failures": [{"run_id": "run-1"}, {"run_id": "run-2"}],
        "recovered_after_similar": False,
        "signals": {"chaos_storm_active": "hsm outage"},
    },
    "explanation": "Category: error…",
}


def test_evidence_view_is_the_whole_llm_surface():
    """The LLM may see ONLY what the pack contains — the view must not leak
    raw errors, norms, signatures, or anything outside the evidence."""
    view = explain.evidence_view(FAILURE)
    assert view == {
        "scenario": "hsm scenario", "step": "s1", "target": "hsm",
        "category": "hsm-socket-drop", "novel_shape": True,
        "taxonomy_help": {"what": "Transactions errored.",
                          "fix": "See the trace."},
        "history": "fails every recorded run (3)",
        "similar_failures": 2, "recovered_after_similar": False,
        "signals": {"chaos_storm_active": "hsm outage"},
    }


def test_parse_explanations_tolerates_fences_and_prose():
    assert explain._parse_explanations('["a", "b"]', 2) == ["a", "b"]
    assert explain._parse_explanations('```json\n["a"]\n```', 1) == ["a"]
    assert explain._parse_explanations("just prose", 2) == ["just prose"] * 2
    assert explain._parse_explanations('["only one"]', 2) == ["only one", ""]


def test_explain_endpoint_needs_llm(monkeypatch):
    monkeypatch.delenv("ASSIST_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(app)
    out = c.post("/insights/explain/run-9").json()
    assert out.get("needs_config") is True


def test_explain_endpoint_grounded_roundtrip(monkeypatch):
    monkeypatch.setenv("ASSIST_LLM_API_KEY", "test-key")
    seen: dict = {}

    def fake_request(method, url, body=None):
        assert "/insights/failures/run-9" in url
        return {"run_id": "run-9", "failures": [FAILURE]}

    def fake_post_json(url, headers, body):
        seen["body"] = body
        return {"choices": [{"message": {
            "content": json.dumps(["The HSM drops the socket every run."])}}]}

    monkeypatch.setattr(rest, "request", fake_request)
    monkeypatch.setattr(rest, "post_json", fake_post_json)
    c = TestClient(app)
    out = c.post("/insights/explain/run-9").json()
    assert out["advisory_only"] is True
    assert out["explanations"] == [{
        "scenario_id": "sc-hsm", "step_id": "s1",
        "text": "The HSM drops the socket every run."}]
    # grounding: the prompt carries the evidence view and nothing else about
    # the failure (e.g. the deterministic explanation string is NOT sent)
    prompt = json.dumps(seen["body"])
    assert "hsm-socket-drop" in prompt and "chaos_storm_active" in prompt
    assert "Category: error…" not in prompt


def test_explain_endpoint_survives_insight_service_down(monkeypatch):
    monkeypatch.setenv("ASSIST_LLM_API_KEY", "test-key")

    def boom(method, url, body=None):
        raise RuntimeError("connection error: refused")

    monkeypatch.setattr(rest, "request", boom)
    c = TestClient(app)
    out = c.post("/insights/explain/run-9").json()
    assert out["explanations"] == [] and "refused" in out["error"]
