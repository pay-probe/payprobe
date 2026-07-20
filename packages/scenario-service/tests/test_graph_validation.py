"""Validation tests for the control-flow graph model (if/switch/loop/edges)."""

from models.scenario import ScenarioDraft, node_output_ports, validate_scenario


def _msgs(result):
    return [i.message for i in result.issues]


def test_legacy_linear_scenario_still_valid():
    """A plain step list with no edges validates exactly as before."""
    d = ScenarioDraft(
        name="legacy",
        steps=[{"id": "s1", "target": "http", "action": "echo_test"}],
    )
    assert validate_scenario(d).valid


def test_valid_if_graph():
    d = ScenarioDraft(
        name="if-graph",
        steps=[
            {"id": "a", "target": "http", "action": "echo_test"},
            {"id": "g", "kind": "if",
             "config": {"left": "${a.response.response_code}",
                        "operator": "eq", "right": "00"}},
            {"id": "ok", "target": "http", "action": "echo_test"},
        ],
        edges=[
            {"source": "a", "source_port": "out", "target": "g"},
            {"source": "g", "source_port": "true", "target": "ok"},
        ],
    )
    assert validate_scenario(d).valid


def test_call_node_requires_scenario_id():
    bad = ScenarioDraft(
        name="call-bad",
        steps=[{"id": "c", "kind": "call", "config": {}}],
    )
    res = validate_scenario(bad)
    assert not res.valid
    assert any("scenario" in m.lower() for m in _msgs(res))

    ok = ScenarioDraft(
        name="call-ok",
        steps=[{"id": "c", "kind": "call", "config": {"scenario_id": "sc-9"}}],
    )
    assert validate_scenario(ok).valid
    # call node exposes a single "out" port
    assert node_output_ports(ok.steps[0]) == ["out"]


def test_output_ports_per_kind():
    if_node = ScenarioDraft(
        name="x", steps=[{"id": "g", "kind": "if"}]
    ).steps[0]
    loop_node = ScenarioDraft(
        name="x", steps=[{"id": "l", "kind": "loop"}]
    ).steps[0]
    switch_node = ScenarioDraft(
        name="x",
        steps=[{"id": "s", "kind": "switch",
                "config": {"cases": [{"value": "a"}, {"value": "b"}]}}],
    ).steps[0]
    assert node_output_ports(if_node) == ["true", "false"]
    assert node_output_ports(loop_node) == ["loop", "done"]
    assert node_output_ports(switch_node) == ["case_0", "case_1", "default"]


def test_bad_port_and_missing_config_flagged():
    d = ScenarioDraft(
        name="bad",
        steps=[{"id": "g", "kind": "if", "config": {}}],
        edges=[{"source": "g", "source_port": "nope", "target": "g"}],
    )
    result = validate_scenario(d)
    assert not result.valid
    joined = " ".join(_msgs(result))
    assert "needs a 'left'" in joined
    assert "no output port 'nope'" in joined


def test_edge_to_unknown_node_flagged():
    d = ScenarioDraft(
        name="dangling",
        steps=[{"id": "a", "target": "http", "action": "echo_test"}],
        edges=[{"source": "a", "source_port": "out", "target": "ghost"}],
    )
    result = validate_scenario(d)
    assert not result.valid
    assert any("unknown node 'ghost'" in m for m in _msgs(result))


def test_forward_reference_is_warning_in_graph_not_error():
    """In a branching graph, ordering is path-dependent, so a not-yet-seen
    reference is a warning rather than a hard error."""
    d = ScenarioDraft(
        name="fwd",
        steps=[
            {"id": "first", "target": "db_probe_core", "action": "query_transaction",
             "payload": {"rrn": "${second.response.rrn}"}},
            {"id": "second", "target": "http", "action": "send_auth_request"},
        ],
        edges=[{"source": "second", "source_port": "out", "target": "first"}],
    )
    result = validate_scenario(d)
    assert result.valid  # warning only, no error
    assert any("earlier step" in m for m in _msgs(result))


def test_valid_code_node():
    d = ScenarioDraft(
        name="code",
        steps=[
            {"id": "calc", "kind": "code",
             "config": {"language": "python", "code": "return {'x': 1}"}},
            {"id": "use", "target": "http", "action": "send_auth_request",
             "payload": {"amount": "${calc.response.x}"}},
        ],
        edges=[{"source": "calc", "source_port": "out", "target": "use"}],
    )
    assert validate_scenario(d).valid


def test_code_node_requires_code_and_valid_language():
    d = ScenarioDraft(
        name="bad-code",
        steps=[{"id": "c", "kind": "code",
                "config": {"language": "ruby", "code": ""}}],
    )
    result = validate_scenario(d)
    assert not result.valid
    joined = " ".join(_msgs(result))
    assert "needs a code snippet" in joined
    assert "language 'ruby'" in joined


def test_valid_http_node():
    d = ScenarioDraft(
        name="http",
        steps=[{"id": "call", "kind": "http",
                "config": {"method": "GET", "url": "https://api.example.com/ping"}}],
    )
    assert validate_scenario(d).valid
    assert node_output_ports(d.steps[0]) == ["out"]


def test_http_node_requires_url_and_valid_method():
    d = ScenarioDraft(
        name="bad-http",
        steps=[{"id": "c", "kind": "http",
                "config": {"method": "FETCH", "url": ""}}],
    )
    result = validate_scenario(d)
    assert not result.valid
    joined = " ".join(_msgs(result))
    assert "needs a URL" in joined
    assert "method 'FETCH'" in joined


def test_crypto_node_unknown_operation_flagged():
    d = ScenarioDraft(
        name="crypto-bad",
        steps=[{"id": "c", "kind": "crypto", "config": {"operation": "rsa"}}],
    )
    result = validate_scenario(d)
    assert not result.valid
    assert any("operation 'rsa'" in m for m in _msgs(result))


def test_crypto_node_valid_operation_ok():
    d = ScenarioDraft(
        name="crypto-ok",
        steps=[{"id": "c", "kind": "crypto",
                "config": {"operation": "kcv", "key": "0123456789ABCDEF"}}],
    )
    assert validate_scenario(d).valid


def test_init_node_invalid_json_flagged():
    d = ScenarioDraft(
        name="init-bad",
        steps=[{"id": "i", "kind": "init", "config": {"data": "{not json"}}],
    )
    result = validate_scenario(d)
    assert not result.valid
    assert any("not valid JSON" in m for m in _msgs(result))


def test_init_node_valid_json_ok():
    d = ScenarioDraft(
        name="init-ok",
        steps=[{"id": "i", "kind": "init", "config": {"data": '{"a": 1}'}}],
    )
    assert validate_scenario(d).valid


def test_loop_item_reference_allowed():
    """${loop.item} inside a loop body must not be flagged as unresolved."""
    d = ScenarioDraft(
        name="loopref",
        steps=[
            {"id": "lp", "kind": "loop", "config": {"mode": "times", "count": 2}},
            {"id": "body", "target": "http", "action": "send_auth_request",
             "payload": {"card_profile": "${loop.item}"}},
        ],
        edges=[{"source": "lp", "source_port": "loop", "target": "body"}],
    )
    assert validate_scenario(d).valid
