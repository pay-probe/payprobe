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
    # call node exposes "out" plus the ADR-0014 "error" port
    assert node_output_ports(ok.steps[0]) == ["out", "error"]


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
    assert node_output_ports(loop_node) == ["loop", "done", "error"]
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
    assert node_output_ports(d.steps[0]) == ["out", "error"]


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


# -- ADR-0014: loop containers, error ports, failure policies -----------------


def _act(sid, **kw):
    return {"id": sid, "target": "http", "action": "echo_test", **kw}


def _loop(sid="lp", **cfg):
    return {"id": sid, "kind": "loop", "config": {"mode": "times", "count": 2, **cfg}}


def test_contained_loop_body_is_valid():
    d = ScenarioDraft(
        name="box",
        steps=[_loop(), _act("a", parent="lp"), _act("b", parent="lp"), _act("after")],
        edges=[
            {"source": "lp", "source_port": "loop", "target": "a"},
            {"source": "a", "source_port": "out", "target": "b"},
            {"source": "lp", "source_port": "done", "target": "after"},
        ],
    )
    assert validate_scenario(d).valid, _msgs(validate_scenario(d))


def test_wire_crossing_a_container_is_an_error():
    d = ScenarioDraft(
        name="cross",
        steps=[_loop(), _act("a", parent="lp"), _act("after")],
        edges=[
            {"source": "lp", "source_port": "loop", "target": "a"},
            {"source": "a", "source_port": "out", "target": "after"},
        ],
    )
    res = validate_scenario(d)
    assert not res.valid
    assert any("crosses the container" in m for m in _msgs(res))


def test_error_wire_must_stay_inside_the_container():
    d = ScenarioDraft(
        name="err-out",
        steps=[_loop(), _act("a", parent="lp"), _act("rescue")],
        edges=[
            {"source": "lp", "source_port": "loop", "target": "a"},
            {"source": "a", "source_port": "error", "target": "rescue"},
        ],
    )
    assert not validate_scenario(d).valid


def test_loop_entry_outside_its_container_is_an_error():
    d = ScenarioDraft(
        name="entry-out",
        steps=[_loop(), _act("a", parent="lp"), _act("b")],
        edges=[{"source": "lp", "source_port": "loop", "target": "b"}],
    )
    assert any("outside its container" in m for m in _msgs(validate_scenario(d)))


def test_parent_must_be_a_loop_and_not_cycle():
    not_loop = ScenarioDraft(name="p", steps=[_act("x"), _act("a", parent="x")])
    assert any("not a loop" in m for m in _msgs(validate_scenario(not_loop)))
    cyc = ScenarioDraft(
        name="c", steps=[_loop("l1", ) | {"parent": "l2"}, _loop("l2") | {"parent": "l1"}]
    )
    assert any("contain each other" in m for m in _msgs(validate_scenario(cyc)))


def test_old_loop_body_leaking_into_main_flow_is_only_a_warning():
    d = ScenarioDraft(
        name="leak",
        steps=[_loop(), _act("a"), _act("after")],
        edges=[
            {"source": "lp", "source_port": "loop", "target": "a"},
            {"source": "a", "source_port": "out", "target": "after"},
            {"source": "lp", "source_port": "done", "target": "after"},
        ],
    )
    res = validate_scenario(d)
    assert res.valid  # old scenarios keep saving
    assert any("wires back into the main flow" in m for m in _msgs(res))


def test_next_and_break_need_a_loop():
    outside = ScenarioDraft(
        name="o",
        steps=[_act("a", on_failure="next"), _act("b")],
        edges=[{"source": "a", "source_port": "out", "target": "b"}],
    )
    assert not validate_scenario(outside).valid
    inside_old = ScenarioDraft(
        name="i",
        steps=[_loop(), _act("a", on_failure="break")],
        edges=[{"source": "lp", "source_port": "loop", "target": "a"}],
    )
    assert validate_scenario(inside_old).valid
    ignore_outside = ScenarioDraft(name="g", steps=[_act("a", on_failure="ignore")])
    assert validate_scenario(ignore_outside).valid


def test_unreachable_contained_step_warns():
    d = ScenarioDraft(
        name="u",
        steps=[_loop(), _act("a", parent="lp"), _act("stray", parent="lp")],
        edges=[{"source": "lp", "source_port": "loop", "target": "a"}],
    )
    res = validate_scenario(d)
    assert res.valid
    assert any("not reachable from its entry" in m for m in _msgs(res))


def test_loop_policy_values_are_checked():
    d = ScenarioDraft(name="v", steps=[_loop(on_failure="retry", handled_failures="maybe")])
    msgs = _msgs(validate_scenario(d))
    assert any("on_failure" in m for m in msgs)
    assert any("handled_failures" in m for m in msgs)


def test_error_port_only_on_executing_kinds():
    d = ScenarioDraft(
        name="w",
        steps=[{"id": "w", "kind": "wait", "config": {"ms": 1}}, _act("a")],
        edges=[{"source": "w", "source_port": "error", "target": "a"}],
    )
    assert not validate_scenario(d).valid
