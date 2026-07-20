"""PayProbe operations exposed as plain functions (the MCP tool bodies).

Each function is a thin, dependency-free (stdlib ``urllib``) wrapper over the
scenario-service / orchestrator REST APIs, so they're unit-testable without the
MCP SDK or live services (monkeypatch :func:`_request`). The MCP server
(``server.py``) just registers these as tools.

Endpoints are configurable via ``SCENARIO_API_URL`` / ``RUN_API_URL``.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SCENARIO_API = os.environ.get("SCENARIO_API_URL", "http://localhost:8000").rstrip("/")
RUN_API = os.environ.get("RUN_API_URL", "http://localhost:8001").rstrip("/")
# Advisory ML insight service (ADR-0005). Optional deployment — the insight
# tools raise a clear "cannot reach" error when it isn't running.
INSIGHT_API = os.environ.get(
    "INSIGHT_API_URL", "http://localhost:8500").rstrip("/")

# Cached minted service JWT: {"token": str, "exp": epoch-seconds}.
_jwt_cache: dict[str, Any] = {"token": None, "exp": 0}


def _service_jwt(secret: str) -> str:
    """Mint (and cache) a short-lived HS256 JWT the upstream services accept.

    Both scenario-service and the orchestrator verify bearer JWTs with the shared
    ``AUTH_JWT_SECRET``, so a token signed with it authenticates the MCP server to
    either. Cached until ~1 min before expiry, then re-minted."""
    now = int(time.time())
    cached = _jwt_cache.get("token")
    if cached and _jwt_cache.get("exp", 0) - 60 > now:
        return cached
    try:
        import jwt  # PyJWT, imported lazily
    except ImportError as exc:  # pragma: no cover - config error
        raise RuntimeError(
            "AUTH_JWT_SECRET is set but PyJWT is not installed — "
            "install with: pip install pyjwt") from exc

    ttl = int(os.environ.get("MCP_JWT_TTL", "3600"))
    claims: dict[str, Any] = {
        "sub": os.environ.get("MCP_JWT_SUB", "mcp-server"),
        "iat": now,
        "exp": now + ttl,
    }
    if aud := os.environ.get("AUTH_JWT_AUDIENCE"):
        claims["aud"] = aud
    if iss := os.environ.get("AUTH_JWT_ISSUER"):
        claims["iss"] = iss
    token = jwt.encode(claims, secret, algorithm="HS256")
    _jwt_cache.update(token=token, exp=now + ttl)
    return token


def _auth_header() -> dict[str, str]:
    """Authorization header for upstream calls, if a credential is configured.

    Precedence: an explicit static bearer (``MCP_API_TOKEN`` / ``API_TOKEN``,
    service-to-service) wins; otherwise mint a JWT from ``AUTH_JWT_SECRET``. If
    neither is set (e.g. PAYPROBE_ENV=dev with the gate open) no header is sent."""
    token = os.environ.get("MCP_API_TOKEN") or os.environ.get("API_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    secret = os.environ.get("AUTH_JWT_SECRET")
    if secret:
        return {"Authorization": f"Bearer {_service_jwt(secret)}"}
    return {}


def _request(method: str, url: str, body: dict | None = None,
             raw: bool = False) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **_auth_header()}
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            if raw:  # caller wants the body verbatim (YAML/XML/HTML exports)
                return text
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}")


# -- catalog / discovery ------------------------------------------------------

def list_step_catalog() -> list[dict]:
    """Every step type available: targets with their actions, params, response
    fields. This is what to read first to know which steps exist."""
    return _request("GET", f"{SCENARIO_API}/catalog")


def list_connections() -> list[dict]:
    """Saved adapter connections (named endpoints a step can target)."""
    return _request("GET", f"{SCENARIO_API}/connections")


def get_connection(name: str) -> dict:
    """One saved connection by name (adapter, host/port, protocol-specific keys
    like response_timeout_sec, keepalive, environment_overrides, ...)."""
    return _request("GET", f"{SCENARIO_API}/connections/{name}")


def save_connection(name: str, draft: dict) -> dict:
    """Create or replace connection ``name``. ``draft`` is the full connection
    body (only the keys you send are persisted, so pass the complete config, not
    just the field you're changing — fetch it with :func:`get_connection` first,
    edit, then save). Common keys: ``adapter``, ``protocol``, ``host``, ``port``,
    ``mode``, ``response_timeout_sec``, ``connect_timeout_sec``,
    ``keepalive``, ``environment_overrides``. Use this to tune a connection —
    e.g. lower an HSM's ``response_timeout_sec`` so failover trips fast on a
    dead member instead of stalling on its timeout."""
    return _request("PUT", f"{SCENARIO_API}/connections/{name}", draft)


def delete_connection(name: str) -> dict:
    """Delete a saved connection."""
    return _request("DELETE", f"{SCENARIO_API}/connections/{name}")


def list_message_formats() -> list[dict]:
    """Registered ISO 8583 / ISO 20022 message formats (DE tables, etc.)."""
    return _request("GET", f"{SCENARIO_API}/formats")


# -- scenarios ----------------------------------------------------------------

def list_scenarios() -> list[dict]:
    """All saved scenarios (summaries)."""
    return _request("GET", f"{SCENARIO_API}/scenarios")


def get_scenario(scenario_id: str) -> dict:
    """Full scenario document by id."""
    return _request("GET", f"{SCENARIO_API}/scenarios/{scenario_id}")


def assist_scenario(prompt: str, scenario: list[dict] | None = None) -> dict:
    """Ask PayProbe's assistant to draft/extend a flow from a sentence. Returns
    a Starter-Flow chain grounded in the catalog."""
    return _request("POST", f"{SCENARIO_API}/scenarios/assist",
                    {"prompt": prompt, "scenario": scenario or []})


def validate_scenario(scenario: dict) -> dict:
    """Validate a scenario draft (structure + references) before saving."""
    return _request("POST", f"{SCENARIO_API}/validate", scenario)


def create_scenario(scenario: dict, project_id: str | None = None,
                    comment: str = "created via MCP") -> dict:
    """Persist a new scenario. ``scenario`` is a ScenarioDraft (name, steps, …)."""
    return _request("POST", f"{SCENARIO_API}/scenarios",
                    {"scenario": scenario, "comment": comment, "project_id": project_id})


def update_scenario(scenario_id: str, scenario: dict,
                    comment: str = "updated via MCP",
                    base_version: int | None = None) -> dict:
    """Update an existing scenario. ``scenario`` is a ScenarioDraft. Pass
    ``base_version`` (the version you edited) to get optimistic-locking: a stale
    save is rejected with 409 instead of silently clobbering newer edits."""
    body: dict = {"scenario": scenario, "comment": comment}
    if base_version is not None:
        body["base_version"] = base_version
    return _request("PUT", f"{SCENARIO_API}/scenarios/{scenario_id}", body)


# -- ISO 8583 inspector -------------------------------------------------------

def iso8583_analyze(message: str, fields: dict | None = None,
                    encoding: str | None = None) -> dict:
    """Decode a raw ISO 8583 wire message into MTI + bitmap + validated data
    elements (+ EMV TLV). ``fields`` overrides the default 1987 DE table."""
    body: dict = {"message": message}
    if fields is not None:
        body["fields"] = fields
    if encoding is not None:
        body["encoding"] = encoding
    return _request("POST", f"{SCENARIO_API}/iso8583/analyze", body)


def iso8583_build(mti: str, values: dict | None = None,
                  fields: dict | None = None, encoding: str | None = None) -> dict:
    """Validate a ``{DE: value}`` map against the field table and pack it into a
    wire message. ``fields`` overrides the default 1987 DE table."""
    body: dict = {"mti": mti, "values": values or {}}
    if fields is not None:
        body["fields"] = fields
    if encoding is not None:
        body["encoding"] = encoding
    return _request("POST", f"{SCENARIO_API}/iso8583/build", body)


def iso8583_diff(a: str, b: str, fields: dict | None = None,
                 encoding: str | None = None) -> dict:
    """Field-level diff of two ISO 8583 messages (added / removed / changed)."""
    body: dict = {"a": a, "b": b}
    if fields is not None:
        body["fields"] = fields
    if encoding is not None:
        body["encoding"] = encoding
    return _request("POST", f"{SCENARIO_API}/iso8583/diff", body)


def iso8583_tlv_build(nodes: list[dict]) -> dict:
    """Encode tag/value nodes into a BER-TLV hex string (e.g. for DE 55).
    ``nodes``: ``[{tag, value(hex)} | {tag, children:[...]}]``."""
    return _request("POST", f"{SCENARIO_API}/iso8583/tlv/build", {"nodes": nodes})


# -- runs ---------------------------------------------------------------------

def run_scenario(environment_name: str = "mock",
                 scenario_ids: list[str] | None = None,
                 scenarios: list[dict] | None = None,
                 label: str | None = None, debug: bool = False) -> dict:
    """Start a run against an environment (default: mock). Provide saved
    ``scenario_ids`` or inline ``scenarios``. Pass ``debug=True`` to start a
    step-through run you then drive with the ``debug_*`` tools. Returns
    ``{run_id, status}``."""
    body: dict = {"environment_name": environment_name}
    if scenario_ids:
        body["scenario_ids"] = scenario_ids
    if scenarios:
        body["scenarios"] = scenarios
    if label:
        body["label"] = label
    if debug:
        body["debug"] = True
    return _request("POST", f"{RUN_API}/runs", body)


def get_run(run_id: str) -> dict:
    """Full run detail: per-scenario / per-step results + summary."""
    return _request("GET", f"{RUN_API}/runs/{run_id}")


def run_trend(days: int = 30) -> list[dict]:
    """Regression-health trend (per-day pass-rate) from run history."""
    return _request("GET", f"{RUN_API}/runs/trend?days={days}")


def list_runs() -> list[dict]:
    """All runs (summaries), newest first. Use to browse run history; pass an
    id from here to :func:`get_run` or :func:`diagnose_run`."""
    return _request("GET", f"{RUN_API}/runs")


def cancel_run(run_id: str) -> dict:
    """Cancel an in-flight run. Returns ``{id, status}`` (e.g. ``cancelling``)."""
    return _request("POST", f"{RUN_API}/runs/{run_id}/cancel")


def diagnose_run(run_id: str) -> dict:
    """Triage a finished run: classify the failure against a taxonomy and return
    likely causes + suggested fixes. Read this before digging into raw step
    output to understand *why* a run failed."""
    return _request("GET", f"{RUN_API}/runs/{run_id}/diagnose")


def list_environments() -> list[dict]:
    """Environments a run can target (the valid ``environment_name`` values for
    :func:`run_scenario`). Read this first to know what you can run against."""
    return _request("GET", f"{RUN_API}/environments")


# -- connections (live test) --------------------------------------------------

def test_connection(config: dict) -> dict:
    """Open a connection to a worker-shaped adapter ``config`` (host/port/
    protocol/...), sign on / health-check it, and report whether it's reachable.
    Use to verify an endpoint before running scenarios against it."""
    return _request("POST", f"{RUN_API}/connections/test", {"config": config})


def diagnose_platform(
    layers: str | None = None,
    connection: str | None = None,
    environment: str | None = None,
) -> dict:
    """Platform doctor: run a layered connectivity diagnosis of the whole stack
    and return a check tree with a fix hint per failure. Layers: ``services``
    (core service links + Redis), ``connections`` (every saved connection:
    config → DNS → live probe, with environment overrides applied),
    ``listeners`` (simulator/participant ports actually accepting), ``runs``
    (recent failures bucketed and correlated with live connection state). Use
    this FIRST when the user reports "connection issues" — it pinpoints the
    broken hop; then drill in with :func:`test_connection` or
    :func:`diagnose_run`. Optionally scope with ``layers`` (comma-separated),
    one ``connection`` name, or an ``environment`` whose overrides apply."""
    params = [f"{k}={v}" for k, v in
              (("layers", layers), ("connection", connection),
               ("environment", environment)) if v]
    qs = ("?" + "&".join(params)) if params else ""
    return _request("GET", f"{RUN_API}/diagnostics{qs}")


# -- simulators ---------------------------------------------------------------

def list_simulators() -> list[dict]:
    """Running host/responder simulators (rules-driven TcpResponders)."""
    return _request("GET", f"{RUN_API}/simulators")


def get_simulator(sid: str) -> dict:
    """Detail for one simulator (config + listen address + hit counters)."""
    return _request("GET", f"{RUN_API}/simulators/{sid}")


def start_simulator(config: dict, label: str = "simulator") -> dict:
    """Start a host/responder simulator. ``config`` is a TcpResponder config
    (listen port + match/respond rules). Returns the simulator info."""
    return _request("POST", f"{RUN_API}/simulators",
                    {"label": label, "config": config})


def stop_simulator(sid: str) -> dict:
    """Stop and remove a running simulator."""
    return _request("DELETE", f"{RUN_API}/simulators/{sid}")


def save_running_simulator(sid: str, enabled: bool = True) -> dict:
    """Persist a *running* (ad-hoc) simulator as a saved config so it shows on the
    portal Simulators page and, when ``enabled``, auto-starts on boot. The live
    responder keeps running under the new saved id. Returns the saved info."""
    return _request("POST", f"{RUN_API}/simulators/{sid}/save?enabled={str(enabled).lower()}")


# -- saved simulator configs (persisted; shown on the portal Simulators page) --

def list_saved_simulators() -> list[dict]:
    """Saved simulator configs (the persisted registry the portal lists), each
    with its live running state."""
    return _request("GET", f"{RUN_API}/simulator-configs")


def get_saved_simulator(cid: str) -> dict:
    """One saved simulator config by id (config + enabled + running state)."""
    return _request("GET", f"{RUN_API}/simulator-configs/{cid}")


def create_saved_simulator(config: dict, label: str = "simulator",
                           enabled: bool = False) -> dict:
    """Persist a new simulator config (TcpResponder or payShield config) so it
    appears on the portal Simulators page. ``enabled`` auto-starts it on boot.
    Use ``start_saved_simulator`` to bring it up now. Returns the saved info."""
    return _request("POST", f"{RUN_API}/simulator-configs",
                    {"label": label, "config": config, "enabled": enabled})


def start_saved_simulator(cid: str) -> dict:
    """Start (bind the listener for) a saved simulator config."""
    return _request("POST", f"{RUN_API}/simulator-configs/{cid}/start")


def stop_saved_simulator(cid: str) -> dict:
    """Stop a saved simulator's listener (keeps the saved config)."""
    return _request("POST", f"{RUN_API}/simulator-configs/{cid}/stop")


def set_saved_simulator_enabled(cid: str, enabled: bool = True) -> dict:
    """Toggle a saved simulator's auto-start-on-boot flag."""
    return _request("POST",
                    f"{RUN_API}/simulator-configs/{cid}/enabled?enabled={str(enabled).lower()}")


def delete_saved_simulator(cid: str) -> dict:
    """Delete a saved simulator config (stopping it first if running)."""
    return _request("DELETE", f"{RUN_API}/simulator-configs/{cid}")


# -- payShield 10K HSM simulator ----------------------------------------------

def start_payshield_simulator(
    label: str = "payshield-hsm",
    port: int = 1500,
    header_bytes: int = 4,
    lmk: str | None = None,
    firmware: str | None = None,
    variant_lmk: bool = False,
    disabled_commands: list[str] | None = None,
    pci_hsm: bool = False,
    persist: bool = True,
) -> dict:
    """Start a Thales payShield 10K HSM simulator (host-command responder with
    real payment crypto under a test LMK) on ``port`` (classic host port 1500).

    ``persist`` (default True) saves it as a config first so it shows on the
    portal Simulators page and auto-starts on boot, then starts it; set
    ``persist=False`` for an ephemeral instance that won't appear in the saved
    registry. ``variant_lmk`` enforces key separation; ``disabled_commands``
    makes listed commands return error 68; ``pci_hsm`` is the flag the NO status
    command reports. Returns the simulator info (incl. its ``id``)."""
    config: dict[str, Any] = {
        "protocol": "payshield",
        "host": "0.0.0.0",
        "port": port,
        "header_bytes": header_bytes,
        "framing": {"length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii"},
        "variant_lmk": variant_lmk,
        "pci_hsm": pci_hsm,
    }
    if lmk:
        config["lmk"] = lmk
    if firmware:
        config["firmware"] = firmware
    if disabled_commands:
        config["disabled_commands"] = disabled_commands
    if not persist:
        return _request("POST", f"{RUN_API}/simulators", {"label": label, "config": config})
    saved = _request("POST", f"{RUN_API}/simulator-configs",
                     {"label": label, "config": config, "enabled": True})
    return _request("POST", f"{RUN_API}/simulator-configs/{saved['id']}/start")


def hsm_command(
    host: str = "127.0.0.1",
    port: int = 1500,
    action: str = "nc",
    params: dict | None = None,
    command: str | None = None,
    data: str | None = None,
    header: str = "HDR1",
) -> dict:
    """Send one payShield host command to a running HSM simulator (or a real
    appliance) and return the parsed reply.

    ``action`` is a high-level verb the client adapter builds — ``nc``,
    ``hsm_status``, ``generate_key``, ``key_check``, ``generate_cvv``,
    ``verify_cvv``, ``translate_pin``, ``translate_pin_zpk_zpk``, ``verify_pin``,
    ``dukpt_translate_pin``, ``generate_mac``, ``verify_mac``, ``import_key``,
    ``export_key``, ``verify_arqc`` — with fields in ``params`` (e.g.
    ``{"cvk": "U..", "pan": "..", "expiry": "2512", "service_code": "000"}``).
    Use ``action="raw"`` with ``command`` (2-char) + ``data`` to send a literal
    command. The response carries ``response_code``, ``error_code``, ``ok`` and
    an action-specific field (``cvv``, ``key``, ``kcv`` …)."""
    body: dict = {"host": host, "port": port, "action": action,
                  "params": params or {}, "header": header}
    if command:
        body["command"] = command
    if data is not None:
        body["data"] = data
    return _request("POST", f"{RUN_API}/hsm/command", body)


#: The bundled payShield smoke scenario (NC -> generate CVK -> CVV -> verify),
#: run inline so the tool is self-contained (no saved scenario required).
_PAYSHIELD_SMOKE_SCENARIO = {
    "id": "mcp-hsm-smoke",
    "name": "payshield_hsm_smoke",
    "description": "NC sign-on, generate a CVK, generate a CVV, verify it.",
    "test_class": "e2e",
    "steps": [
        {"id": "nc", "target": "hsm", "action": "nc", "payload": {},
         "assertions": [{"field": "error_code", "operator": "eq", "expected": "00"}]},
        {"id": "gen_cvk", "target": "hsm", "action": "generate_key",
         "payload": {"key_type": "402", "scheme": "U"},
         "assertions": [{"field": "ok", "operator": "eq", "expected": True}]},
        {"id": "gen_cvv", "target": "hsm", "action": "generate_cvv",
         "payload": {"cvk": "${gen_cvk.response.key}", "pan": "4000000000000002",
                     "expiry": "2512", "service_code": "000"},
         "assertions": [{"field": "cvv", "operator": "present"}]},
        {"id": "verify_cvv", "target": "hsm", "action": "verify_cvv",
         "payload": {"cvk": "${gen_cvk.response.key}", "cvv": "${gen_cvv.response.cvv}",
                     "pan": "4000000000000002", "expiry": "2512", "service_code": "000"},
         "assertions": [{"field": "error_code", "operator": "eq", "expected": "00"}]},
    ],
}


def run_hsm_example(environment_name: str = "payshield-sim", label: str | None = None) -> dict:
    """Run the bundled payShield smoke scenario (NC → generate CVK → generate
    CVV → verify CVV) against an environment whose ``hsm`` target points at a
    running payShield simulator (the bundled ``payshield-sim`` environment does).
    Start the simulator first with ``start_payshield_simulator``. Returns
    ``{run_id, status}`` — poll ``get_run`` for the per-step outcome."""
    body: dict = {"environment_name": environment_name,
                  "scenarios": [_PAYSHIELD_SMOKE_SCENARIO]}
    if label:
        body["label"] = label
    return _request("POST", f"{RUN_API}/runs", body)


# -- participant flows, groups & topologies (the simulated-network map) --------

def list_participant_flows() -> list[dict]:
    """Saved participant flows (reactive listening stand-ins: trigger→logic→reply)."""
    return _request("GET", f"{SCENARIO_API}/participant-flows")


def list_participant_groups() -> list[dict]:
    """Participant groups (fleets): member connections + a selection policy."""
    return _request("GET", f"{SCENARIO_API}/participant-groups")


def get_participant_group(gid: str) -> dict:
    """One participant group by id (members + selection policy)."""
    return _request("GET", f"{SCENARIO_API}/participant-groups/{gid}")


def create_participant_group(draft: dict) -> dict:
    """Create a participant group (fleet). ``draft`` is a ParticipantGroupDraft:
    ``{name, description, adapter_type, members: [{connection, weight}],
    selection: {policy, key}}`` where policy ∈ round_robin|weighted|random|
    failover|sticky. Every member connection must resolve to the same adapter
    type (validated server-side). Returns the created group (with id)."""
    return _request("POST", f"{SCENARIO_API}/participant-groups", draft)


def save_participant_group(gid: str, draft: dict) -> dict:
    """Create or replace the participant group ``gid``. ``draft`` is a
    ParticipantGroupDraft (see :func:`create_participant_group`). Use this to
    change a group's members or its ``selection.policy`` — e.g. flip a fleet from
    ``round_robin`` to ``failover`` so a dropped member is retried on a sibling."""
    return _request("PUT", f"{SCENARIO_API}/participant-groups/{gid}", draft)


def delete_participant_group(gid: str) -> dict:
    """Delete a participant group."""
    return _request("DELETE", f"{SCENARIO_API}/participant-groups/{gid}")


def list_participants() -> list[dict]:
    """Running participant-flow listeners: bound endpoint, owner (standalone vs a
    topology run), protocol and message count — the live nodes of the network."""
    return _request("GET", f"{RUN_API}/participants")


def list_topology_runs() -> list[dict]:
    """Live network-flow runs: per-run readiness (health: total/live/ready) and
    the bound endpoints of every instance — the data behind a global topology
    map. Runs are created by :func:`start_network_flow`."""
    return _request("GET", f"{RUN_API}/topology-runs")


def list_network_traces(limit: int = 100, q: str | None = None) -> dict:
    """Recent transactions across every live listener — local and
    fleet-hosted. One row per correlation id (newest first) with flows touched,
    hop count and an ok/fail flag; drill into one with
    :func:`get_network_trace`. ``q`` filters by correlation-id substring (e.g.
    a STAN). Capture is OFF by default — enable it with
    :func:`set_trace_capture` before driving traffic."""
    params = f"?limit={limit}" + (f"&q={q}" if q else "")
    return _request("GET", f"{RUN_API}/participants/traces{params}")


def get_network_trace(correlation_id: str) -> dict:
    """One transaction's cross-hop correlated trace: every hop tagged with this
    correlation id across all listeners (scenario → switch → issuer → back),
    stitched in time order — fleet-hosted hops included."""
    return _request("GET", f"{RUN_API}/participants/trace/{correlation_id}")


def set_trace_capture(enabled: bool) -> dict:
    """Pause or resume Network Trace recording across every live listener
    (fleet hosts pick the gate up on their next heartbeat). Listeners keep
    serving — only trace recording is gated. Returns the new capture state."""
    return _request("POST", f"{RUN_API}/participants/capture", {"enabled": enabled})


# -- network flows (ADR-0004: canvas-authored composites, successor of topologies)

def list_network_flows() -> list[dict]:
    """Saved network flows — canvas-authored composites of the simulated
    network: ``participant`` nodes (a participant flow × instances),
    ``scenario`` nodes (traffic initiators), ``simulator`` nodes (saved
    simulators brought up with the network) and ``group`` nodes (wiring
    targets). Edges are wiring ("sends traffic to"); start order is derived
    callees-first. Successor of topologies (same ids after migration)."""
    return _request("GET", f"{SCENARIO_API}/network-flows")


def get_network_flow(nid: str) -> dict:
    """One network flow by id: nodes, wiring edges, canvas layout."""
    return _request("GET", f"{SCENARIO_API}/network-flows/{nid}")


def save_network_flow(nid: str, draft: dict) -> dict:
    """Create or replace network flow ``nid``. ``draft`` is
    ``{name, description, nodes: [{id, kind, config}], edges: [{source,
    target}], layout?}`` with kind ∈ participant|scenario|simulator|group and
    config carrying ``flow_id``/``instances``/``port`` (participant),
    ``scenario_id``/``environment``/``autostart`` (scenario),
    ``simulator_id`` (simulator) or ``group_id`` (group). Edges mean "source
    sends traffic to target" — callees are started first. Rejected (422) on
    structural errors; run :func:`validate_network_flow` first for a full
    issue list."""
    return _request("PUT", f"{SCENARIO_API}/network-flows/{nid}", draft)


def delete_network_flow(nid: str) -> dict:
    """Delete a network flow (does not touch the participant flows /
    scenarios / simulators it referenced)."""
    return _request("DELETE", f"{SCENARIO_API}/network-flows/{nid}")


def validate_network_flow(draft: dict) -> dict:
    """Validate a network-flow draft without saving: shape (node configs,
    edge direction, cycles) plus dangling flow/scenario/group references.
    Returns ``{valid, issues: [{severity, step_id, message}]}``."""
    return _request("POST", f"{SCENARIO_API}/network-flows/validate", draft)


def plan_network_flow(nid: str) -> dict:
    """The resolved launch plan of a network flow: participants in
    callees-first start order (topological sort of the wiring; node list order
    when unwired), plus initiators and simulators. Dry-run companion of
    :func:`start_network_flow`."""
    return _request("GET", f"{SCENARIO_API}/network-flows/{nid}/plan")


def start_network_flow(nid: str) -> dict:
    """Start a network flow: brings up its ``simulator`` nodes, then every
    ``participant`` node's instances callees-first, then fires the autostart
    ``scenario`` initiators. Returns the run (id, health, endpoints,
    simulators, initiator_runs). Stop it with :func:`stop_network_flow`."""
    return _request("POST", f"{RUN_API}/network-flows/{nid}/start")


def stop_network_flow(run_id: str) -> dict:
    """Stop a network-flow run: cancels its initiator runs, tears down its
    participant listeners and stops any simulators the run started (never ones
    it found already running). ``run_id`` is the run id from
    :func:`start_network_flow` / :func:`list_topology_runs`."""
    return _request("DELETE", f"{RUN_API}/topology-runs/{run_id}")


def install_showcase(start: bool = False) -> dict:
    """Build the bundled **showcase** demo network — a switch, three issuers and
    a payShield HSM, wired with a driver scenario — through the public APIs.
    Idempotent (re-running updates the same artifacts). Set ``start=True`` to
    bring it up immediately. THE fastest way to see PayProbe end to end;
    returns the network id (``showcase-net``) + the run when started."""
    return _request("POST", f"{RUN_API}/showcase/install", {"start": start})


# -- schedules ----------------------------------------------------------------

def list_schedules() -> list[dict]:
    """All scheduled regression runs (interval or daily)."""
    return _request("GET", f"{RUN_API}/schedules")


def create_schedule(label: str, request: dict | None = None,
                    interval_sec: int | None = None,
                    daily_at: str | None = None, enabled: bool = True) -> dict:
    """Create a scheduled run. ``request`` is a run body (same selectors as
    :func:`run_scenario`). Trigger via either ``interval_sec`` (every N seconds)
    or ``daily_at`` ("HH:MM" UTC)."""
    body: dict = {"label": label, "request": request or {}, "enabled": enabled}
    if interval_sec is not None:
        body["interval_sec"] = interval_sec
    if daily_at is not None:
        body["daily_at"] = daily_at
    return _request("POST", f"{RUN_API}/schedules", body)


def run_schedule_now(sid: str) -> dict:
    """Trigger a schedule immediately (out of band), returning the started run."""
    return _request("POST", f"{RUN_API}/schedules/{sid}/run")


def set_schedule_enabled(sid: str, enabled: bool = True) -> dict:
    """Enable or pause a schedule without deleting it."""
    return _request("POST", f"{RUN_API}/schedules/{sid}/enabled?enabled={str(enabled).lower()}")


def delete_schedule(sid: str) -> dict:
    """Delete a schedule."""
    return _request("DELETE", f"{RUN_API}/schedules/{sid}")


# -- message formats (CRUD) ---------------------------------------------------

def get_format(fid: str) -> dict:
    """Full message-format document (DE table, header, etc.) by id."""
    return _request("GET", f"{SCENARIO_API}/formats/{fid}")


def create_format(draft: dict) -> dict:
    """Register a new ISO 8583 / ISO 20022 message format. ``draft`` is a
    MessageFormatDraft (name, version, fields, ...)."""
    return _request("POST", f"{SCENARIO_API}/formats", draft)


def update_format(fid: str, draft: dict) -> dict:
    """Replace an existing message format with ``draft``."""
    return _request("PUT", f"{SCENARIO_API}/formats/{fid}", draft)


def clone_format(fid: str, name: str | None = None,
                 version: str | None = None) -> dict:
    """Clone a format into a new one (optionally renaming / re-versioning)."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if version is not None:
        body["version"] = version
    return _request("POST", f"{SCENARIO_API}/formats/{fid}/clone", body)


def delete_format(fid: str) -> dict:
    """Delete a message format."""
    return _request("DELETE", f"{SCENARIO_API}/formats/{fid}")


# -- test-data manager --------------------------------------------------------

def list_card_pools() -> list[dict]:
    """Saved card pools (sets of PANs/cards a scenario can draw from)."""
    return _request("GET", f"{SCENARIO_API}/test-data/card-pools")


def get_card_pool(name: str) -> dict:
    """One card pool by name."""
    return _request("GET", f"{SCENARIO_API}/test-data/card-pools/{name}")


def save_card_pool(name: str, draft: dict) -> dict:
    """Create or replace a card pool. ``draft`` is a CardPoolDraft (cards, ...)."""
    return _request("PUT", f"{SCENARIO_API}/test-data/card-pools/{name}", draft)


def delete_card_pool(name: str) -> dict:
    """Delete a card pool."""
    return _request("DELETE", f"{SCENARIO_API}/test-data/card-pools/{name}")


def list_bin_ranges() -> list[dict]:
    """Saved BIN ranges (generate Luhn-valid PANs from these)."""
    return _request("GET", f"{SCENARIO_API}/test-data/bin-ranges")


def get_bin_range(name: str) -> dict:
    """One BIN range by name."""
    return _request("GET", f"{SCENARIO_API}/test-data/bin-ranges/{name}")


def save_bin_range(name: str, draft: dict) -> dict:
    """Create or replace a BIN range. ``draft`` is a BinRangeDraft."""
    return _request("PUT", f"{SCENARIO_API}/test-data/bin-ranges/{name}", draft)


def delete_bin_range(name: str) -> dict:
    """Delete a BIN range."""
    return _request("DELETE", f"{SCENARIO_API}/test-data/bin-ranges/{name}")


def generate_bin_pans(name: str, count: int = 10, as_pool: str | None = None,
                      seed: str | None = None) -> dict:
    """Generate ``count`` (1–10000) Luhn-valid PANs from a BIN range. Pass
    ``as_pool`` to also persist them as a named card pool; ``seed`` for
    reproducible output. Returns ``{pans, saved_pool}``."""
    qs = f"count={count}"
    if as_pool is not None:
        qs += f"&as_pool={as_pool}"
    if seed is not None:
        qs += f"&seed={seed}"
    return _request("POST", f"{SCENARIO_API}/test-data/bin-ranges/{name}/generate?{qs}")


def list_terminal_pools() -> list[dict]:
    """Saved terminal pools (TIDs/MIDs a scenario can draw from)."""
    return _request("GET", f"{SCENARIO_API}/test-data/terminal-pools")


def get_terminal_pool(name: str) -> dict:
    """One terminal pool by name."""
    return _request("GET", f"{SCENARIO_API}/test-data/terminal-pools/{name}")


def save_terminal_pool(name: str, draft: dict) -> dict:
    """Create or replace a terminal pool. ``draft`` is a TerminalPoolDraft."""
    return _request("PUT", f"{SCENARIO_API}/test-data/terminal-pools/{name}", draft)


def delete_terminal_pool(name: str) -> dict:
    """Delete a terminal pool."""
    return _request("DELETE", f"{SCENARIO_API}/test-data/terminal-pools/{name}")


def list_test_keys() -> list[dict]:
    """Registered cryptographic keys (values masked; never returns plaintext)."""
    return _request("GET", f"{SCENARIO_API}/test-data/keys")


def get_test_key(name: str) -> dict:
    """One key registration by name (masked)."""
    return _request("GET", f"{SCENARIO_API}/test-data/keys/{name}")


def save_test_key(name: str, draft: dict) -> dict:
    """Create or replace a key registration. ``draft`` is a KeyDraft; the value
    is encrypted at rest and masked on read."""
    return _request("PUT", f"{SCENARIO_API}/test-data/keys/{name}", draft)


def delete_test_key(name: str) -> dict:
    """Delete a key registration."""
    return _request("DELETE", f"{SCENARIO_API}/test-data/keys/{name}")


# -- packs --------------------------------------------------------------------

def list_packs() -> list[dict]:
    """Curated test-case packs available to install."""
    return _request("GET", f"{SCENARIO_API}/packs")


def get_pack(pack_id: str) -> dict:
    """Detail for one pack (its scenarios + metadata)."""
    return _request("GET", f"{SCENARIO_API}/packs/{pack_id}")


def install_pack(pack_id: str) -> dict:
    """Import a pack's scenarios into a new project so they run as a suite."""
    return _request("POST", f"{SCENARIO_API}/packs/{pack_id}/install")


# -- run reports --------------------------------------------------------------

def compare_runs(run_id: str, to: str | None = None) -> dict:
    """Diff a run against another (default: the previous like-for-like run).
    Reports per-step status changes — chiefly ``regressed`` (was passing, now
    failing). Pass ``to`` to compare against a specific run id."""
    url = f"{RUN_API}/runs/{run_id}/compare"
    if to is not None:
        url += f"?to={to}"
    return _request("GET", url)


def run_certification(run_id: str, pack: str) -> dict:
    """Certification summary: compliance % of a run against a test-case ``pack``."""
    return _request("GET", f"{RUN_API}/runs/{run_id}/certification?pack={pack}")


def run_junit(run_id: str) -> str:
    """Run results as a JUnit XML string (for CI ingestion). Raw XML."""
    return _request("GET", f"{RUN_API}/runs/{run_id}/junit", raw=True)


def run_flakiness() -> list[dict]:
    """Per-scenario flakiness: scenarios that intermittently pass and fail, with
    a flip-based flakiness score. Surfaces unstable tests across run history."""
    return _request("GET", f"{RUN_API}/runs/flakiness")


# -- model insights (ADR-0005, advisory) --------------------------------------

def get_run_insights(run_id: str) -> dict:
    """ADVISORY model insights for one run's failures: each failed step
    categorized (heuristic taxonomy + learned clusters when trained; ``novel``
    flags shapes no rule anticipated) with an evidence-pack ``explanation`` —
    the failure's place in the scenario's history, prior identical-signature
    failures, whether the scenario later recovered, and a suggested fix. A
    model OPINION over run history: use it to triage, never as a verdict —
    gates and sign-off stay deterministic."""
    return _request(
        "GET",
        f"{INSIGHT_API}/insights/failures/{urllib.parse.quote(run_id, safe='')}")


def list_insight_predictions(environment: str | None = None) -> dict:
    """ADVISORY per-scenario outcome predictions, riskiest first:
    ``p_fail_next`` / ``p_flaky`` estimated from run history (recency-weighted
    frequency baseline; a learned model only where it beats that baseline),
    with ``n_history`` and ``top_factors`` so thin evidence is visible. Good
    for picking what to re-run first; never a reason to skip a run."""
    q = f"?environment={urllib.parse.quote(environment)}" if environment else ""
    return _request("GET", f"{INSIGHT_API}/insights/predictions{q}")


def train_insights() -> dict:
    """Ingest new completed runs into the insight corpus and (re)fit the
    learned categorizer. Incremental and idempotent — safe to call any time.
    Writes only to the insight service's own model store, nothing else."""
    return _request("POST", f"{INSIGHT_API}/train", {})


# -- Go/No-Go sign-off --------------------------------------------------------

def certify_run(run_id: str, pack: str, policy: dict | None = None,
                project: str | None = None) -> dict:
    """Gate a run for production: evaluate explicit Go/No-Go gates against a
    test-case ``pack``, stamp provenance, and freeze an immutable sign-off
    snapshot. Returns the snapshot incl. ``verdict`` (``GO``/``NO_GO``), the
    per-gate results, the provenance stamp, and a tamper-evident content hash.

    ``policy`` (optional) overrides the gate thresholds, e.g.
    ``{"tiers": {"P0": 100, "P1": 95}, "max_regressions": 0,
    "allowed_environments": ["staging"]}``. The regression gate compares against
    the last GO run for this ``(project, pack, environment)``; a GO advances that
    baseline. Use this — not :func:`run_certification` — when the question is
    *may we ship*, not *what is the compliance %*."""
    body: dict = {"pack": pack}
    if policy is not None:
        body["policy"] = policy
    if project is not None:
        body["project"] = project
    return _request("POST", f"{RUN_API}/runs/{run_id}/certify", body)


def list_signoffs(run_id: str | None = None, project: str | None = None,
                  verdict: str | None = None) -> dict:
    """List Go/No-Go sign-off snapshots, newest first. Filter by ``run_id``,
    ``project`` or ``verdict`` (``GO``/``NO_GO``)."""
    params = {k: v for k, v in
              {"run_id": run_id, "project": project, "verdict": verdict}.items()
              if v is not None}
    url = f"{RUN_API}/signoffs"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request("GET", url)


def get_signoff(signoff_id: str) -> dict:
    """Full immutable sign-off snapshot by id: verdict, gates, provenance,
    content hash, and the approval trail."""
    return _request("GET", f"{RUN_API}/signoffs/{signoff_id}")


def approve_signoff(signoff_id: str, decision: str = "approved",
                    note: str = "") -> dict:
    """Append an entry to a sign-off's approval trail (the frozen evidence is
    untouched, so its content hash stays valid)."""
    return _request("POST", f"{RUN_API}/signoffs/{signoff_id}/approve",
                    {"decision": decision, "note": note})


# -- load testing -------------------------------------------------------------

def list_load_runs() -> list[dict]:
    """All load/stress runs (summaries)."""
    return _request("GET", f"{RUN_API}/load-runs")


def get_load_run(run_id: str) -> dict:
    """Live or persisted detail for one load run (TPS, latency, errors)."""
    return _request("GET", f"{RUN_API}/load-runs/{run_id}")


def compare_load_runs(run_id: str, to: str | None = None) -> dict:
    """Diff a load run against another (default: the previous like-for-like load
    run): p50/p95/p99 latency, error-rate, achieved TPS, TPS stability (cv), and
    a top error-category breakdown — for detecting performance regressions
    run-over-run. Pass ``to`` to compare against a specific load run id."""
    url = f"{RUN_API}/load-runs/{run_id}/compare"
    if to is not None:
        url += f"?to={to}"
    return _request("GET", url)


def start_load_run(scenario_ids: list[str] | None = None,
                   scenarios: list[dict] | None = None,
                   environment_name: str | None = None,
                   profile_type: str = "steady", duration_s: float = 60.0,
                   workers: int = 1, target_tps: float = 0.0,
                   label: str | None = None, extra: dict | None = None) -> dict:
    """Start a distributed load test. The transaction is resolved like a normal
    run (``scenario_ids`` or inline ``scenarios`` + ``environment_name``) then
    driven by a profile across ``workers`` processes. ``profile_type``: steady |
    ramp | spike | soak. For ramp/spike/soak params (start_tps, end_tps, ramp_s,
    spike_tps, connections, ...) pass them in ``extra``."""
    body: dict = {"type": profile_type, "duration_s": duration_s,
                  "workers": workers, "target_tps": target_tps}
    if scenario_ids:
        body["scenario_ids"] = scenario_ids
    if scenarios:
        body["scenarios"] = scenarios
    if environment_name:
        body["environment_name"] = environment_name
    if label:
        body["label"] = label
    if extra:
        body.update(extra)
    return _request("POST", f"{RUN_API}/load-runs", body)


def stop_load_run(run_id: str) -> dict:
    """Stop an in-flight load run (workers drain in-flight work and exit)."""
    return _request("POST", f"{RUN_API}/load-runs/{run_id}/stop")


# -- chaos injection & resilience runs ----------------------------------------
#
# Fault-injection dial + timed storms on a *running* simulator, and resilience
# runs (chaos storm + live load-run observation → pass/fail certificate).
# Chaos block keys: seed, drop_pct/drop, latency_ms, malformed_pct/malformed/
# malformed_mode, partial_pct/partial/partial_bytes.

def get_simulator_chaos(sid: str) -> dict:
    """The live chaos dial for a running simulator: current global chaos block,
    cumulative fault count, and storm status."""
    return _request("GET", f"{RUN_API}/simulators/{sid}/chaos")


def set_simulator_chaos(sid: str, chaos: dict) -> dict:
    """Apply a chaos block to a RUNNING simulator (effective next reply, no
    restart). ``chaos`` keys: drop_pct, latency_ms, malformed_pct,
    malformed_mode, partial_pct, partial_bytes, seed. An empty ``{}`` turns
    chaos off. Cancels any active storm on that simulator."""
    return _request("PUT", f"{RUN_API}/simulators/{sid}/chaos", chaos)


def start_chaos_storm(sid: str, phases: list[dict], repeat: int = 1) -> dict:
    """Run a timed chaos sequence against a running simulator. ``phases`` is a
    list of ``{duration_s, chaos: {...}, label?}`` (a ``{}`` chaos block = a calm
    phase, e.g. the 'up' half of a flap). ``repeat`` replays the phase list N
    times (flapping = 2 phases × N). The pre-storm chaos is restored when the
    storm ends or is cancelled."""
    return _request(
        "POST", f"{RUN_API}/simulators/{sid}/chaos/storm",
        {"phases": phases, "repeat": repeat},
    )


def cancel_chaos_storm(sid: str) -> dict:
    """Cancel a running storm and restore the pre-storm chaos block."""
    return _request("DELETE", f"{RUN_API}/simulators/{sid}/chaos/storm")


def list_resilience_runs() -> list[dict]:
    """Resilience-run history (newest first), each with its headline verdict
    (score / grade / verdict)."""
    return _request("GET", f"{RUN_API}/resilience/runs")


def get_resilience_run(rid: str) -> dict:
    """Live/finished resilience run — per-second samples plus the scored report
    (availability / absorption / recovery / latency, grade, verdict) when done."""
    return _request("GET", f"{RUN_API}/resilience/runs/{rid}")


def start_resilience_run(target_sim_id: str, load_run_id: str, storm: dict,
                         baseline_s: float = 10.0, recovery_s: float = 15.0,
                         label: str | None = None,
                         thresholds: dict | None = None) -> dict:
    """Start a resilience run: observe an already-running load run's client while
    a chaos ``storm`` hits ``target_sim_id``, then score how well the client
    survived. Start the load run first (:func:`start_load_run`) and pass its id
    as ``load_run_id``. ``storm`` is a ``{phases: [...], repeat}`` block (same
    shape as :func:`start_chaos_storm`). ``baseline_s`` / ``recovery_s`` are the
    calm observation windows before and after the storm. ``thresholds``
    optionally overrides the scoring gates."""
    body: dict[str, Any] = {
        "target_sim_id": target_sim_id,
        "load_run_id": load_run_id,
        "storm": storm,
        "baseline_s": baseline_s,
        "recovery_s": recovery_s,
    }
    if label is not None:
        body["label"] = label
    if thresholds is not None:
        body["thresholds"] = thresholds
    return _request("POST", f"{RUN_API}/resilience/runs", body)


def cancel_resilience_run(rid: str) -> dict:
    """Cancel a running resilience run and lift any storm it started."""
    return _request("DELETE", f"{RUN_API}/resilience/runs/{rid}")


def list_load_workers() -> list[dict]:
    """Load-worker fleet currently registered with the coordinator."""
    return _request("GET", f"{RUN_API}/load-workers")


def stop_load_worker(worker_id: str) -> dict:
    """Drain one load worker: it finishes its shard's in-flight work and exits."""
    return _request("POST", f"{RUN_API}/load-workers/{worker_id}/stop")


def stop_all_load_workers() -> dict:
    """Drain every load worker currently in the fleet."""
    return _request("POST", f"{RUN_API}/load-workers/stop-all")


# -- code / node execution ----------------------------------------------------

def validate_code(language: str, code: str = "") -> dict:
    """Syntax-check a code-node snippet without running it (editor diagnostics)."""
    return _request("POST", f"{RUN_API}/code/validate",
                    {"language": language, "code": code})


def execute_node(node: dict, context: dict | None = None,
                 environment: dict | None = None) -> dict:
    """Execute a single flow node in isolation (the editor's Execute picker).
    ``context`` is accumulated upstream results ``{step_id: {request, response}}``.
    Returns the node's request/response."""
    body: dict = {"node": node, "context": context or {}}
    if environment is not None:
        body["environment"] = environment
    return _request("POST", f"{RUN_API}/nodes/execute", body)


# -- playground (ADR-0007: unified ad-hoc execution surface) -------------------

def playground_targets() -> dict:
    """Everything callable ad-hoc right now, merged from the live registries:
    registered connections (× their override-matrix environments), running
    simulators, network participants (incl. port-less NATS subjects),
    participant groups, and local function families (crypto ops). Also carries
    ``samples``: ready-made example interactions per element family
    (``samples.wire[protocol-or-adapter]`` for wire targets,
    ``samples.functions.crypto`` per operation) — start from one instead of
    hand-building a payload. Read this before :func:`playground_execute` to
    pick a target by reference."""
    return _request("GET", f"{RUN_API}/playground/targets")


def playground_execute(target: dict, action: str, payload: dict | None = None,
                       message_format_id: str | None = None,
                       label: str | None = None) -> dict:
    """Fire ONE ad-hoc message/command at a target BY REFERENCE — no scenario
    authoring needed. ``target`` is ``{kind: connection|group|simulator|
    participant|raw|function, id, environment?, host?, port?, protocol?}``. The
    orchestrator resolves the reference server-side (connection ⊕ override
    matrix — secrets never leave the server) and delegates to the worker
    engine; echoed request/response payloads come back MASKED.
    ``message_format_id`` pins the ISO 8583 dialect. Sends REAL traffic to the
    resolved endpoint (running-simulator hits are tagged in its stats); never
    starts or stops anything."""
    return _request("POST", f"{RUN_API}/playground/execute", {
        "target": target, "action": action, "payload": payload or {},
        "message_format_id": message_format_id, "label": label})


# -- variables ----------------------------------------------------------------

def get_global_variables() -> dict:
    """Global variables (+ which names are secret)."""
    return _request("GET", f"{SCENARIO_API}/variables/global")


def set_global_variables(variables: dict, secrets: list[str] | None = None) -> dict:
    """Replace global variables. ``secrets`` lists names whose values are secret."""
    return _request("PUT", f"{SCENARIO_API}/variables/global",
                    {"variables": variables, "secrets": secrets or []})


def get_project_variables(project_id: str) -> dict:
    """Variables scoped to a project (+ secret names)."""
    return _request("GET", f"{SCENARIO_API}/variables/project/{project_id}")


def set_project_variables(project_id: str, variables: dict,
                          secrets: list[str] | None = None) -> dict:
    """Replace a project's variables."""
    return _request("PUT", f"{SCENARIO_API}/variables/project/{project_id}",
                    {"variables": variables, "secrets": secrets or []})


def get_set_variables(set_id: str) -> dict:
    """Variables scoped to a named set (+ secret names)."""
    return _request("GET", f"{SCENARIO_API}/variables/set/{set_id}")


def set_set_variables(set_id: str, variables: dict,
                      secrets: list[str] | None = None) -> dict:
    """Replace a set's variables."""
    return _request("PUT", f"{SCENARIO_API}/variables/set/{set_id}",
                    {"variables": variables, "secrets": secrets or []})


def resolve_variables(project_id: str | None = None,
                      set_ids: list[str] | None = None,
                      variables: dict | None = None,
                      secret_vars: list[str] | None = None) -> dict:
    """Resolve the effective variable map for a given scope combination (global +
    project + sets + scenario-scoped overrides). Useful to preview what a
    scenario will see before running it."""
    return _request("POST", f"{SCENARIO_API}/variables/resolve", {
        "project_id": project_id, "set_ids": set_ids or [],
        "variables": variables or {}, "secret_vars": secret_vars or []})


def scenario_variables(scenario_id: str) -> dict:
    """Effective variables for a stored scenario (merged across all scopes)."""
    return _request("GET", f"{SCENARIO_API}/scenarios/{scenario_id}/variables")


# -- data tables --------------------------------------------------------------

def list_tables() -> list[dict]:
    """Saved data tables (lookup/parametrization tables)."""
    return _request("GET", f"{SCENARIO_API}/tables")


def runtime_tables() -> dict:
    """Runtime-resolved view of the data tables (as the engine sees them)."""
    return _request("GET", f"{SCENARIO_API}/tables/runtime")


def get_table(name: str) -> dict:
    """One data table by name."""
    return _request("GET", f"{SCENARIO_API}/tables/{name}")


def save_table(name: str, draft: dict) -> dict:
    """Create or replace a data table. ``draft`` is a DataTableDraft (rows, ...)."""
    return _request("PUT", f"{SCENARIO_API}/tables/{name}", draft)


def delete_table(name: str) -> dict:
    """Delete a data table."""
    return _request("DELETE", f"{SCENARIO_API}/tables/{name}")


# -- starter flows ------------------------------------------------------------

def list_starter_flows() -> list[dict]:
    """Saved starter flows (palette building blocks for the editor)."""
    return _request("GET", f"{SCENARIO_API}/starter-flows")


def get_starter_flow(fid: str) -> dict:
    """One starter flow by id."""
    return _request("GET", f"{SCENARIO_API}/starter-flows/{fid}")


def create_starter_flow(draft: dict) -> dict:
    """Create a starter flow. ``draft`` is a StarterFlowDraft (name, steps, ...)."""
    return _request("POST", f"{SCENARIO_API}/starter-flows", draft)


def update_starter_flow(fid: str, draft: dict) -> dict:
    """Replace a starter flow."""
    return _request("PUT", f"{SCENARIO_API}/starter-flows/{fid}", draft)


def delete_starter_flow(fid: str) -> dict:
    """Delete a starter flow."""
    return _request("DELETE", f"{SCENARIO_API}/starter-flows/{fid}")


# -- projects / search / secrets / export -------------------------------------

def list_projects() -> list[dict]:
    """All projects (a project groups scenarios into a suite)."""
    return _request("GET", f"{SCENARIO_API}/projects")


def get_project(project_id: str) -> dict:
    """One project by id (its sets + test cases)."""
    return _request("GET", f"{SCENARIO_API}/projects/{project_id}")


def search(q: str, project_id: str | None = None) -> dict:
    """Case-insensitive substring search across projects, sets and test cases.
    Scope to one project with ``project_id``."""
    url = f"{SCENARIO_API}/search?q={q}"
    if project_id is not None:
        url += f"&project_id={project_id}"
    return _request("GET", url)


def list_secrets() -> dict:
    """Masked inventory of every secret across connections, variables and keys,
    plus encrypted-at-rest status. Never returns plaintext."""
    return _request("GET", f"{SCENARIO_API}/secrets")


def export_scenario(scenario_id: str, format: str = "yaml") -> str:
    """Export a scenario as a portable document (no server metadata). ``format``:
    ``yaml`` or ``json``. Returns the raw document text."""
    return _request("GET",
                    f"{SCENARIO_API}/scenarios/{scenario_id}/export?format={format}",
                    raw=True)


# -- debug session (step-through runs) ----------------------------------------

def debug_set_breakpoints(run_id: str, breakpoints: list[str]) -> dict:
    """Set the node ids the debug run should break at. Start the run with
    ``run_scenario(..., debug=True)`` first. Returns the active breakpoints."""
    return _request("POST", f"{RUN_API}/runs/{run_id}/debug/breakpoints",
                    {"breakpoints": breakpoints})


def debug_step(run_id: str) -> dict:
    """Run the currently-paused node and pause again at the next one."""
    return _request("POST", f"{RUN_API}/runs/{run_id}/debug/step")


def debug_continue(run_id: str) -> dict:
    """Resume a paused debug run until the next breakpoint (or the end)."""
    return _request("POST", f"{RUN_API}/runs/{run_id}/debug/continue")


def debug_pause(run_id: str) -> dict:
    """Request the debug run to break in at the next node."""
    return _request("POST", f"{RUN_API}/runs/{run_id}/debug/pause")


# -- catalog management -------------------------------------------------------

def manage_catalog() -> dict:
    """Every step target with provenance flags (builtin / custom / overridden /
    hidden). The editable view behind :func:`list_step_catalog`."""
    return _request("GET", f"{SCENARIO_API}/catalog/manage")


def save_catalog_target(target: str, spec: dict) -> dict:
    """Create or override a step target. ``spec`` is a TargetSpec; its ``target``
    field must equal ``target``."""
    return _request("PUT", f"{SCENARIO_API}/catalog/targets/{target}", spec)


def delete_catalog_target(target: str) -> dict:
    """Delete a custom target, or hide a built-in one."""
    return _request("DELETE", f"{SCENARIO_API}/catalog/targets/{target}")


def restore_catalog_target(target: str) -> list[dict]:
    """Drop an override and/or unhide — return a built-in target to its default."""
    return _request("POST", f"{SCENARIO_API}/catalog/targets/{target}/restore")


# -- environments (registry / management) -------------------------------------
# NB: :func:`list_environments` returns the orchestrator's *runnable* view. The
# tools below manage the scenario-service *registry* (the editable source of
# truth) — create/clone/tweak dev/staging/prod profiles instead of hand-editing
# the bundled ``examples/environments/*.json`` files.

def get_environment(name: str) -> dict:
    """One environment profile from the registry (adapters + config) by name."""
    return _request("GET", f"{SCENARIO_API}/environments/{name}")


def save_environment(name: str, draft: dict) -> dict:
    """Create or replace an environment profile. ``name`` is the registry key
    (slug); ``draft`` is an EnvironmentDraft (display ``name``, ``description``,
    ``adapters`` map + arbitrary adapter-specific keys). Secret-looking fields
    are encrypted at rest."""
    return _request("PUT", f"{SCENARIO_API}/environments/{name}", draft)


def delete_environment(name: str) -> dict:
    """Delete an environment profile from the registry."""
    return _request("DELETE", f"{SCENARIO_API}/environments/{name}")


# -- projects (management) ----------------------------------------------------

def create_project(draft: dict) -> dict:
    """Create a project (groups scenarios into a suite). ``draft`` is a
    ProjectDraft: ``{name, description}``. Returns the created Project (with id)."""
    return _request("POST", f"{SCENARIO_API}/projects", draft)


def update_project(project_id: str, draft: dict) -> dict:
    """Rename / re-describe a project. ``draft`` is a ProjectDraft."""
    return _request("PUT", f"{SCENARIO_API}/projects/{project_id}", draft)


def delete_project(project_id: str) -> dict:
    """Delete a project. Rejected with 409 if it still contains test cases —
    move or delete them first."""
    return _request("DELETE", f"{SCENARIO_API}/projects/{project_id}")
