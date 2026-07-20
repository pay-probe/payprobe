"""AI scenario assistant (heuristic provider) — grounded flow generation."""
from api.assist import assist, build_steps, catalog_index

IDX = catalog_index()


def _targets_actions_valid(flow):
    for s in flow["steps"]:
        assert s["target"] in IDX, f"unknown target {s['target']}"
        assert s["action"] in IDX[s["target"]], f"unknown action {s['target']}/{s['action']}"


def test_chip_purchase_approved_chain():
    out = assist("create a card purchase that is approved and settles downstream")
    flow = out["flow"]
    _targets_actions_valid(flow)
    refs = [s["ref"] for s in flow["steps"]]
    assert "auth" in refs and "settle" in refs
    auth = next(s for s in flow["steps"] if s["ref"] == "auth")
    assert {"field": "response_code", "operator": "eq", "expected": "00"} in auth["assertions"]
    settle = next(s for s in flow["steps"] if s["ref"] == "settle")
    assert settle["config"]["rrn"] == "${auth.response.rrn}"  # wired to the auth step


def test_generic_payment_prompt_builds_multistep_flow():
    out = assist("make a card payment flow")
    steps = out["flow"]["steps"]
    _targets_actions_valid(out["flow"])
    actions = [s["action"] for s in steps]
    # an authorization that settles downstream
    assert len(steps) >= 2
    assert "send_auth_request" in actions and "query_transaction" in actions


def test_only_authorization_stays_minimal():
    out = assist("just an authorization, no settlement")
    actions = [s["action"] for s in out["flow"]["steps"]]
    assert "query_transaction" not in actions  # minimal -> no settlement


def test_payment_prompt_builds_auth_flow():
    out = assist("authorize a contactless card payment")
    _targets_actions_valid(out["flow"])
    assert any(s["action"] == "send_auth_request" for s in out["flow"]["steps"])


def test_decline_sets_negative_assertion():
    out = assist("authorization that is declined for insufficient funds")
    auth = next(s for s in out["flow"]["steps"] if s["ref"] == "auth")
    assert {"field": "response_code", "operator": "ne", "expected": "00"} in auth["assertions"]


def test_echo_and_iso_messaging():
    out = assist("network echo then ISO 8583 pack and parse round trip")
    actions = {s["action"] for s in out["flow"]["steps"]}
    assert "echo_test" in actions
    assert "iso8583_pack" in actions and "iso8583_parse" in actions


def test_every_builtin_step_is_grounded_and_has_id():
    out = assist("do a sale and reverse it")
    assert out["provider"] == "heuristic"
    assert out["flow"]["id"].startswith("ai-")
    _targets_actions_valid(out["flow"])
    assert any(s["action"] == "send_reversal" for s in out["flow"]["steps"])


def test_edit_mode_wires_new_step_to_existing_auth():
    existing = [{"id": "step_aaa", "target": "http", "action": "send_auth_request"}]
    out = assist("also add a settlement check downstream", existing)
    assert out["mode"] == "extend"
    _targets_actions_valid(out["flow"])
    settle = next(s for s in out["flow"]["steps"] if s["action"] == "query_transaction")
    # references the EXISTING auth node id, not a new "auth" ref
    assert settle["config"]["rrn"] == "${step_aaa.response.rrn}"
    # did not re-create the auth step
    assert not any(s["action"] == "send_auth_request" for s in out["flow"]["steps"])


def test_create_mode_when_no_scenario_context():
    out = assist("chip purchase approved")  # no existing steps
    assert out["mode"] == "create"


class _FakeResp:
    def __init__(self, payload):
        import json
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _openai_reply(content):
    return {"choices": [{"message": {"content": content}}]}


def test_llm_provider_path_with_stubbed_http(monkeypatch):
    import json
    from api import assist as A

    flow_json = json.dumps({"label": "x", "description": "x", "steps": [
        {"ref": "auth", "target": "http", "action": "send_auth_request",
         "config": {}, "inputs": {}, "assertions": []}]})

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResp(_openai_reply(flow_json)))
    llm = {"enabled": True, "api_key": "k", "base_url": "http://x", "model": "m"}
    out = A.assist("anything goes here", llm=llm)
    assert out["provider"] == "llm"
    assert out["flow"]["steps"][0]["action"] == "send_auth_request"


def test_llm_handles_markdown_fenced_json(monkeypatch):
    import json
    from api import assist as A
    fenced = "```json\n" + json.dumps({"steps": [
        {"ref": "e", "target": "http", "action": "echo_test"}]}) + "\n```"
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(_openai_reply(fenced)))
    llm = {"enabled": True, "api_key": "k", "base_url": "http://x", "model": "m"}
    out = A.assist("echo", llm=llm)
    assert out["provider"] == "llm"


def test_anthropic_provider_path(monkeypatch):
    import json
    from api import assist as A

    flow_json = json.dumps({"steps": [
        {"ref": "auth", "target": "http", "action": "send_auth_request"}]})
    captured = {}

    def fake_post(url, headers, body):
        captured["url"], captured["headers"], captured["body"] = url, headers, body
        return {"content": [{"type": "text", "text": flow_json}]}

    monkeypatch.setattr(A, "_http_post_json", fake_post)
    llm = {"provider": "anthropic", "enabled": True, "api_key": "k",
           "base_url": "https://api.anthropic.com/v1/messages", "model": "claude-x"}
    out = A.assist("authorize a purchase", llm=llm)
    assert out["provider"] == "llm"
    assert out["flow"]["steps"][0]["action"] == "send_auth_request"
    # used Anthropic conventions: x-api-key header, system hoisted out of messages
    assert "x-api-key" in captured["headers"]
    assert "system" in captured["body"]
    assert all(m["role"] != "system" for m in captured["body"]["messages"])


def test_test_llm_detects_key_provider_mismatch():
    from api import assist as A
    # Anthropic key under OpenAI provider -> clear hint, no HTTP call
    r = A.test_llm({"provider": "openai", "enabled": True,
                    "api_key": "sk-ant-abcd1234", "base_url": "http://x", "model": "m"})
    assert r["ok"] is False and "Anthropic key" in r["error"]
    # OpenAI key under Anthropic provider
    r2 = A.test_llm({"provider": "anthropic", "enabled": True,
                     "api_key": "sk-proj-xyz", "base_url": "http://x", "model": "m"})
    assert r2["ok"] is False and "OpenAI key" in r2["error"]


def test_test_llm_flags_invalid_model_id():
    from api import assist as A
    # a model with a space (e.g. "opus 4.7") -> caught before any HTTP call
    r = A.test_llm({"provider": "anthropic", "enabled": True, "api_key": "sk-ant-x",
                    "base_url": "http://x", "model": "opus 4.7"})
    assert r["ok"] is False and "no spaces" in r["error"]
    assert "claude-opus-4-8" in r["error"]
    # anthropic model not starting with claude-
    r2 = A.test_llm({"provider": "anthropic", "enabled": True, "api_key": "sk-ant-x",
                     "base_url": "http://x", "model": "gpt-4o"})
    assert r2["ok"] is False and "claude-" in r2["error"]


def test_test_llm_reports_ok_and_errors(monkeypatch):
    from api import assist as A
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(_openai_reply("OK")))
    res = A.test_llm({"enabled": True, "api_key": "k", "base_url": "http://x", "model": "m"})
    assert res["ok"] and res["reply"] == "OK"

    def _boom(req, timeout=None):
        raise RuntimeError("HTTP 401: invalid key")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    res2 = A.test_llm({"enabled": True, "api_key": "bad", "base_url": "http://x", "model": "m"})
    assert res2["ok"] is False and "401" in res2["error"]


def test_grounding_is_tolerant_of_label_and_case():
    from api.assist import ground_steps, catalog_index
    idx = catalog_index()
    raw = [
        {"ref": "a", "target": "Http", "action": "Send Auth Request"},      # case/spacing
        {"ref": "b", "target": "http", "action": "send_reversal"},          # exact
        {"ref": "c", "target": "query_transaction", "action": "query_transaction"},  # remap by action
        {"ref": "d", "target": "nope", "action": "does_not_exist"},         # dropped
    ]
    grounded, dropped = ground_steps(raw, idx)
    by_ref = {s["ref"]: s for s in grounded}
    assert by_ref["a"]["target"] == "http" and by_ref["a"]["action"] == "send_auth_request"
    assert by_ref["c"]["target"] == "db_probe_core"  # unique action remapped its target
    assert dropped == ["nope/does_not_exist"]


def test_llm_garbage_falls_back_to_heuristic(monkeypatch):
    import json
    from api import assist as A
    # LLM returns steps with unknown ids -> all dropped -> heuristic fallback
    bad = json.dumps({"steps": [{"ref": "x", "target": "FooSvc", "action": "do_thing"}]})
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(_openai_reply(bad)))
    out = A.assist("a card purchase",
                   llm={"enabled": True, "api_key": "k", "base_url": "http://x", "model": "m"})
    assert out["provider"] == "heuristic-fallback"
    assert out["flow"]["steps"]                      # not empty
    _targets_actions_valid(out["flow"])


def test_unmappable_prompt_returns_note():
    out = assist("xyzzy quux frobnicate")
    # falls back to a default auth step (still grounded) OR returns a note
    assert out["flow"]["steps"] or out["notes"]
    _targets_actions_valid(out["flow"])
