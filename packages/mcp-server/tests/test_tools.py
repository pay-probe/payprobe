"""MCP tool bodies: correct endpoints/methods/payloads (HTTP mocked)."""
import pytest

from mcp_server import tools


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def fake_request(method, url, body=None, raw=False):
        seen.append((method, url, body))
        if raw:
            return "raw-doc"
        # return a shape that matches what each tool would expect
        if url.endswith("/catalog"):
            return [{"target": "terminal_sim", "actions": [{"name": "insert_card"}]}]
        if "/scenarios/assist" in url:
            return {"flow": {"steps": []}, "provider": "heuristic"}
        if url.endswith("/runs"):
            return {"run_id": "r1", "status": "pending"}
        if url.endswith("/simulator-configs"):
            return {"id": "cfg1", "label": "x", "enabled": True, "running": False}
        if "/simulator-configs/" in url and url.endswith("/start"):
            return {"id": "cfg1", "running": True, "port": 1500}
        return {"ok": True}

    monkeypatch.setattr(tools, "_request", fake_request)
    return seen


def test_list_step_catalog(calls):
    out = tools.list_step_catalog()
    assert out[0]["target"] == "terminal_sim"
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/catalog")


def test_assist_scenario_posts_prompt(calls):
    tools.assist_scenario("a chip purchase")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/scenarios/assist")
    assert body["prompt"] == "a chip purchase" and body["scenario"] == []


def test_create_scenario_wraps_payload(calls):
    tools.create_scenario({"name": "x", "steps": []}, project_id="p1")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/scenarios")
    assert body["scenario"]["name"] == "x" and body["project_id"] == "p1"


def test_run_scenario_targets_run_api(calls):
    tools.run_scenario(environment_name="mock", scenario_ids=["a", "b"], label="suite")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/runs")
    assert body["environment_name"] == "mock" and body["scenario_ids"] == ["a", "b"]
    assert body["label"] == "suite"


def test_get_run_uses_run_api(calls):
    tools.get_run("r1")
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/runs/r1")


def test_environment_management(calls):
    tools.get_environment("staging")
    assert calls[-1][:2] == ("GET", f"{tools.SCENARIO_API}/environments/staging")
    tools.save_environment("staging", {"name": "Staging", "adapters": {}})
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/environments/staging")
    assert body["name"] == "Staging"
    tools.delete_environment("staging")
    assert calls[-1][:2] == ("DELETE", f"{tools.SCENARIO_API}/environments/staging")


def test_project_management(calls):
    tools.create_project({"name": "Regression"})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/projects") and body["name"] == "Regression"
    tools.update_project("p1", {"name": "Renamed"})
    assert calls[-1][:2] == ("PUT", f"{tools.SCENARIO_API}/projects/p1")
    tools.delete_project("p1")
    assert calls[-1][:2] == ("DELETE", f"{tools.SCENARIO_API}/projects/p1")


def test_update_scenario_puts_with_version(calls):
    tools.update_scenario("s1", {"name": "x", "steps": []}, base_version=3)
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/scenarios/s1")
    assert body["scenario"]["name"] == "x" and body["base_version"] == 3


def test_update_scenario_omits_version_when_none(calls):
    tools.update_scenario("s1", {"name": "x", "steps": []})
    _, _, body = calls[-1]
    assert "base_version" not in body


def test_iso8583_analyze_posts_message(calls):
    tools.iso8583_analyze("0200...", encoding="ascii")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/iso8583/analyze")
    assert body["message"] == "0200..." and body["encoding"] == "ascii"
    assert "fields" not in body


def test_iso8583_build_defaults_values(calls):
    tools.iso8583_build("0200")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/iso8583/build")
    assert body["mti"] == "0200" and body["values"] == {}


def test_iso8583_diff_sends_both(calls):
    tools.iso8583_diff("0200a", "0200b")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/iso8583/diff")
    assert body["a"] == "0200a" and body["b"] == "0200b"


def test_iso8583_tlv_build_wraps_nodes(calls):
    tools.iso8583_tlv_build([{"tag": "9F26", "value": "00"}])
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/iso8583/tlv/build")
    assert body["nodes"][0]["tag"] == "9F26"


def test_list_runs_uses_run_api(calls):
    tools.list_runs()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/runs")


def test_cancel_run_posts(calls):
    tools.cancel_run("r1")
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/runs/r1/cancel")


def test_diagnose_run_gets(calls):
    tools.diagnose_run("r1")
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/runs/r1/diagnose")


def test_list_environments_uses_run_api(calls):
    tools.list_environments()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/environments")


def test_server_registers_new_tools():
    # every new tool body should exist and be callable
    for name in ("update_scenario", "iso8583_analyze", "iso8583_build",
                 "iso8583_diff", "iso8583_tlv_build", "list_runs",
                 "cancel_run", "diagnose_run", "list_environments"):
        assert callable(getattr(tools, name))


# -- P1: connections / simulators / schedules / formats / test-data / packs ---

def test_test_connection_wraps_config(calls):
    tools.test_connection({"host": "h", "port": 5000})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/connections/test")
    assert body["config"]["host"] == "h"


def test_start_simulator_posts_label_and_config(calls):
    tools.start_simulator({"port": 5000}, label="acquirer")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/simulators")
    assert body["label"] == "acquirer" and body["config"]["port"] == 5000


def test_stop_simulator_deletes(calls):
    tools.stop_simulator("s1")
    assert calls[-1][0] == "DELETE" and calls[-1][1].endswith("/simulators/s1")


def test_get_simulator_gets(calls):
    tools.get_simulator("s1")
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/simulators/s1")


def test_start_payshield_simulator_persists_by_default(calls):
    tools.start_payshield_simulator(port=1500, variant_lmk=True,
                                    disabled_commands=["CW"], pci_hsm=True)
    # persist=True: create the saved config, then start it
    create = calls[-2]
    start = calls[-1]
    assert create[0] == "POST" and create[1].endswith("/simulator-configs")
    cfg = create[2]["config"]
    assert cfg["protocol"] == "payshield" and cfg["port"] == 1500
    assert cfg["variant_lmk"] is True and cfg["pci_hsm"] is True
    assert cfg["disabled_commands"] == ["CW"]
    assert create[2]["enabled"] is True and create[2]["label"] == "payshield-hsm"
    assert start[0] == "POST" and start[1].endswith("/simulator-configs/cfg1/start")


def test_start_payshield_simulator_ephemeral(calls):
    tools.start_payshield_simulator(persist=False)
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/simulators")
    assert body["config"]["protocol"] == "payshield"


def test_save_running_simulator_posts_save(calls):
    tools.save_running_simulator("7f61ed10", enabled=True)
    method, url, _ = calls[-1]
    assert method == "POST"
    assert url.endswith("/simulators/7f61ed10/save?enabled=true")


def test_create_saved_simulator_posts_config(calls):
    tools.create_saved_simulator({"protocol": "payshield", "port": 1500},
                                 label="hsm", enabled=True)
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/simulator-configs")
    assert body["label"] == "hsm" and body["enabled"] is True
    assert body["config"]["protocol"] == "payshield"


def test_saved_simulator_lifecycle_endpoints(calls):
    tools.list_saved_simulators()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/simulator-configs")
    tools.start_saved_simulator("cfg1")
    assert calls[-1][1].endswith("/simulator-configs/cfg1/start")
    tools.stop_saved_simulator("cfg1")
    assert calls[-1][1].endswith("/simulator-configs/cfg1/stop")
    tools.set_saved_simulator_enabled("cfg1", True)
    assert calls[-1][1].endswith("/simulator-configs/cfg1/enabled?enabled=true")
    tools.delete_saved_simulator("cfg1")
    assert calls[-1][0] == "DELETE" and calls[-1][1].endswith("/simulator-configs/cfg1")


def test_hsm_command_named_action(calls):
    tools.hsm_command(host="h", port=1500, action="generate_cvv",
                      params={"cvk": "U..", "pan": "4000"})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/hsm/command")
    assert body["action"] == "generate_cvv" and body["params"]["pan"] == "4000"
    assert body["host"] == "h" and "command" not in body


def test_hsm_command_raw_passthrough(calls):
    tools.hsm_command(action="raw", command="NC", data="")
    _, url, body = calls[-1]
    assert url.endswith("/hsm/command")
    assert body["action"] == "raw" and body["command"] == "NC" and body["data"] == ""


def test_run_hsm_example_posts_inline_scenario(calls):
    tools.run_hsm_example(label="hsm-smoke")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/runs")
    assert body["environment_name"] == "payshield-sim"
    assert body["scenarios"][0]["name"] == "payshield_hsm_smoke"
    assert [s["action"] for s in body["scenarios"][0]["steps"]] == [
        "nc", "generate_key", "generate_cvv", "verify_cvv"]
    assert body["label"] == "hsm-smoke"


def test_create_schedule_builds_body(calls):
    tools.create_schedule("nightly", request={"environment_name": "mock"}, daily_at="02:00")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/schedules")
    assert body["label"] == "nightly" and body["daily_at"] == "02:00"
    assert body["request"]["environment_name"] == "mock"
    assert "interval_sec" not in body


def test_run_schedule_now_posts(calls):
    tools.run_schedule_now("sc1")
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/schedules/sc1/run")


def test_set_schedule_enabled_encodes_query(calls):
    tools.set_schedule_enabled("sc1", enabled=False)
    method, url, _ = calls[-1]
    assert method == "POST" and url.endswith("/schedules/sc1/enabled?enabled=false")


def test_delete_schedule_deletes(calls):
    tools.delete_schedule("sc1")
    assert calls[-1][0] == "DELETE" and calls[-1][1].endswith("/schedules/sc1")


def test_clone_format_omits_empty(calls):
    tools.clone_format("f1")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/formats/f1/clone")
    assert body == {}


def test_create_format_posts_draft(calls):
    tools.create_format({"name": "iso87", "fields": {}})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/formats")
    assert body["name"] == "iso87"


def test_save_card_pool_puts(calls):
    tools.save_card_pool("vip", {"cards": []})
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/test-data/card-pools/vip")
    assert body["cards"] == []


def test_generate_bin_pans_builds_query(calls):
    tools.generate_bin_pans("visa", count=25, as_pool="gen", seed="abc")
    method, url, _ = calls[-1]
    assert method == "POST"
    assert "/test-data/bin-ranges/visa/generate?" in url
    assert "count=25" in url and "as_pool=gen" in url and "seed=abc" in url


def test_generate_bin_pans_minimal_query(calls):
    tools.generate_bin_pans("visa")
    url = calls[-1][1]
    assert url.endswith("/test-data/bin-ranges/visa/generate?count=10")


def test_save_test_key_puts(calls):
    tools.save_test_key("zmk1", {"type": "ZMK", "value": "00"})
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/test-data/keys/zmk1")
    assert body["type"] == "ZMK"


def test_install_pack_posts(calls):
    tools.install_pack("emv-contact")
    method, url, _ = calls[-1]
    assert method == "POST" and url.endswith("/packs/emv-contact/install")


def test_list_packs_gets(calls):
    tools.list_packs()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/packs")


# -- P2: reports / load / code / variables / tables / flows / projects -------

def test_compare_runs_appends_to(calls):
    tools.compare_runs("r1", to="r0")
    method, url, _ = calls[-1]
    assert method == "GET" and url.endswith("/runs/r1/compare?to=r0")


def test_compare_runs_without_to(calls):
    tools.compare_runs("r1")
    assert calls[-1][1].endswith("/runs/r1/compare")


def test_compare_load_runs_appends_to(calls):
    tools.compare_load_runs("l2", to="l1")
    method, url = calls[-1][0], calls[-1][1]
    assert method == "GET" and url.endswith("/load-runs/l2/compare?to=l1")


def test_compare_load_runs_without_to(calls):
    tools.compare_load_runs("l2")
    assert calls[-1][1].endswith("/load-runs/l2/compare")


def test_run_certification_requires_pack(calls):
    tools.run_certification("r1", pack="emv")
    method, url, _ = calls[-1]
    assert method == "GET" and url.endswith("/runs/r1/certification?pack=emv")


def test_run_junit_is_raw_string(calls):
    out = tools.run_junit("r1")
    assert out == "raw-doc"
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/runs/r1/junit")


def test_export_scenario_is_raw_string(calls):
    out = tools.export_scenario("s1", format="json")
    assert out == "raw-doc"
    assert calls[-1][1].endswith("/scenarios/s1/export?format=json")


def test_start_load_run_merges_extra(calls):
    tools.start_load_run(scenario_ids=["a"], profile_type="ramp",
                         extra={"start_tps": 10, "end_tps": 100})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/load-runs")
    assert body["type"] == "ramp" and body["start_tps"] == 10 and body["end_tps"] == 100
    assert body["scenario_ids"] == ["a"]


def test_stop_load_run_posts(calls):
    tools.stop_load_run("l1")
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/load-runs/l1/stop")


def test_stop_all_load_workers_posts(calls):
    tools.stop_all_load_workers()
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/load-workers/stop-all")


def test_validate_code_posts(calls):
    tools.validate_code("python", "x = 1")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/code/validate")
    assert body["language"] == "python" and body["code"] == "x = 1"


def test_execute_node_defaults_context(calls):
    tools.execute_node({"type": "rest"})
    _, url, body = calls[-1]
    assert url.endswith("/nodes/execute")
    assert body["node"]["type"] == "rest" and body["context"] == {}


def test_playground_targets_gets(calls):
    tools.playground_targets()
    method, url, _ = calls[-1]
    assert method == "GET" and url.endswith("/playground/targets")


def test_playground_execute_posts_reference(calls):
    tools.playground_execute(
        {"kind": "connection", "id": "switch_visa", "environment": "staging"},
        "send_message", {"mti": "0200"}, message_format_id="visa-base1")
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/playground/execute")
    assert body["target"]["id"] == "switch_visa"
    assert body["payload"] == {"mti": "0200"}
    assert body["message_format_id"] == "visa-base1"


def test_set_global_variables_puts(calls):
    tools.set_global_variables({"k": "v"}, secrets=["k"])
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/variables/global")
    assert body["variables"] == {"k": "v"} and body["secrets"] == ["k"]


def test_resolve_variables_builds_body(calls):
    tools.resolve_variables(project_id="p1", set_ids=["s1"])
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/variables/resolve")
    assert body["project_id"] == "p1" and body["set_ids"] == ["s1"]


def test_scenario_variables_gets(calls):
    tools.scenario_variables("s1")
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/scenarios/s1/variables")


def test_save_table_puts(calls):
    tools.save_table("bins", {"rows": []})
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/tables/bins")
    assert body["rows"] == []


def test_create_starter_flow_posts(calls):
    tools.create_starter_flow({"name": "auth"})
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/starter-flows")
    assert body["name"] == "auth"


def test_search_scopes_to_project(calls):
    tools.search("purchase", project_id="p1")
    method, url, _ = calls[-1]
    assert method == "GET" and url.endswith("/search?q=purchase&project_id=p1")


def test_list_secrets_gets(calls):
    tools.list_secrets()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/secrets")


def test_request_raw_returns_text(monkeypatch):
    # _request(raw=True) returns the verbatim body, not parsed JSON
    import urllib.request

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"name: hi\n"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert tools._request("GET", "http://x/export", raw=True) == "name: hi\n"


def test_server_registers_p2_tools():
    for name in ("compare_runs", "run_certification", "run_junit", "run_flakiness",
                 "list_load_runs", "get_load_run", "start_load_run", "stop_load_run",
                 "list_load_workers", "stop_load_worker", "stop_all_load_workers",
                 "validate_code", "execute_node", "get_global_variables",
                 "set_global_variables", "get_project_variables", "set_project_variables",
                 "get_set_variables", "set_set_variables", "resolve_variables",
                 "scenario_variables", "list_tables", "runtime_tables", "get_table",
                 "save_table", "delete_table", "list_starter_flows", "get_starter_flow",
                 "create_starter_flow", "update_starter_flow", "delete_starter_flow",
                 "list_projects", "get_project", "search", "list_secrets",
                 "export_scenario"):
        assert callable(getattr(tools, name))


# -- debug session + catalog management --------------------------------------

def test_run_scenario_debug_flag(calls):
    tools.run_scenario(scenario_ids=["a"], debug=True)
    _, url, body = calls[-1]
    assert url.endswith("/runs") and body["debug"] is True


def test_run_scenario_no_debug_key_by_default(calls):
    tools.run_scenario(scenario_ids=["a"])
    assert "debug" not in calls[-1][2]


def test_debug_set_breakpoints_posts(calls):
    tools.debug_set_breakpoints("r1", ["n1", "n2"])
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/runs/r1/debug/breakpoints")
    assert body["breakpoints"] == ["n1", "n2"]


def test_debug_step_continue_pause(calls):
    tools.debug_step("r1")
    assert calls[-1][1].endswith("/runs/r1/debug/step")
    tools.debug_continue("r1")
    assert calls[-1][1].endswith("/runs/r1/debug/continue")
    tools.debug_pause("r1")
    assert calls[-1][1].endswith("/runs/r1/debug/pause")
    assert all(c[0] == "POST" for c in calls[-3:])


def test_save_catalog_target_puts(calls):
    tools.save_catalog_target("my_tgt", {"target": "my_tgt", "actions": []})
    method, url, body = calls[-1]
    assert method == "PUT" and url.endswith("/catalog/targets/my_tgt")
    assert body["target"] == "my_tgt"


def test_restore_catalog_target_posts(calls):
    tools.restore_catalog_target("terminal_sim")
    method, url, _ = calls[-1]
    assert method == "POST" and url.endswith("/catalog/targets/terminal_sim/restore")


def test_manage_catalog_gets(calls):
    tools.manage_catalog()
    assert calls[-1][0] == "GET" and calls[-1][1].endswith("/catalog/manage")


def test_server_registers_debug_catalog_tools():
    for name in ("debug_set_breakpoints", "debug_step", "debug_continue",
                 "debug_pause", "manage_catalog", "save_catalog_target",
                 "delete_catalog_target", "restore_catalog_target"):
        assert callable(getattr(tools, name))


def test_server_registers_p1_tools():
    for name in ("test_connection", "list_simulators", "get_simulator",
                 "start_simulator", "stop_simulator", "list_schedules",
                 "create_schedule", "run_schedule_now", "set_schedule_enabled",
                 "delete_schedule", "get_format", "create_format", "update_format",
                 "clone_format", "delete_format", "list_card_pools", "get_card_pool",
                 "save_card_pool", "delete_card_pool", "list_bin_ranges",
                 "get_bin_range", "save_bin_range", "delete_bin_range",
                 "generate_bin_pans", "list_terminal_pools", "get_terminal_pool",
                 "save_terminal_pool", "delete_terminal_pool", "list_test_keys",
                 "get_test_key", "save_test_key", "delete_test_key", "list_packs",
                 "get_pack", "install_pack"):
        assert callable(getattr(tools, name))


def test_server_import_does_not_require_mcp_sdk():
    # importing the server module must not need the mcp SDK
    from mcp_server import server
    assert callable(server.build_server)


def test_auth_header_prefers_static_bearer(monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "tok-123")
    monkeypatch.setenv("AUTH_JWT_SECRET", "shh")  # static bearer must win
    assert tools._auth_header() == {"Authorization": "Bearer tok-123"}


def test_auth_header_mints_jwt_from_secret(monkeypatch):
    import jwt

    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", "shared-secret")
    tools._jwt_cache.update(token=None, exp=0)  # reset cache

    header = tools._auth_header()
    assert header["Authorization"].startswith("Bearer ")
    token = header["Authorization"].split(" ", 1)[1]
    claims = jwt.decode(token, "shared-secret", algorithms=["HS256"])
    assert claims["sub"] == "mcp-server" and "exp" in claims


def test_auth_header_empty_when_unconfigured(monkeypatch):
    for var in ("MCP_API_TOKEN", "API_TOKEN", "AUTH_JWT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert tools._auth_header() == {}
