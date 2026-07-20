"""The showcase demo network — pure document builders, one source of truth.

These functions produce the exact connections, participant flows, group,
driver scenario and network that make up the "PayProbe in five minutes" demo.
They are pure (no I/O), so three consumers share them:

* ``scripts/showcase.py`` — the CLI that POSTs them to a running stack;
* ``scripts/test_showcase.py`` — validates them against the real models;
* ``orchestrator.api`` ``POST /showcase/install`` — the portal's one-click load.

Keep this the single definition. Ids: flows/groups/networks take a chosen id
(PUT by id); scenarios and saved simulators get server-assigned ids, resolved
by name at install time.
"""
from __future__ import annotations

NET_ID = "showcase-net"
ISSUER_FLOW = "showcase-issuer"
SWITCH_FLOW = "showcase-switch"
GROUP_ID = "showcase-issuers"
SIM_LABEL = "Showcase payShield"
DRIVER_NAME = "Showcase driver"
ENV_NAME = "showcase"

CONNS: dict[str, dict] = {
    "showcase_switch_in": {"adapter": "tcp", "protocol": "iso8583",
                           "mode": "inbound", "host": "127.0.0.1", "port": 9401},
    # the driver dials the switch here (its inbound port) — an explicit outbound
    # connection so the scenario resolves to the switch, not a type default.
    "showcase_switch_out": {"adapter": "tcp", "protocol": "iso8583",
                            "mode": "outbound", "host": "127.0.0.1", "port": 9401},
    "showcase_issuer_in": {"adapter": "tcp", "protocol": "iso8583",
                           "mode": "inbound", "host": "127.0.0.1", "port": 9410},
    "showcase_issuer_out": {"adapter": "tcp", "protocol": "iso8583",
                            "mode": "outbound", "host": "127.0.0.1", "port": 9410},
}


def issuer_flow() -> dict:
    """A pure responder: 0200 in → approved 0210 out (DE39=00), echoing key fields."""
    return {
        "name": "Showcase issuer",
        "description": "Approves every 0200 with DE39=00 (demo issuer stand-in).",
        "trigger": {"connection": "showcase_issuer_in"},
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "reply", "kind": "reply",
             "payload": {"mti": "0210", "echo": ["11", "37", "41"],
                         "set": {"39": "00"}}},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "reply"}],
    }


def switch_flow() -> dict:
    """Receives 0200, forwards to the issuer group, returns the issuer's DE39."""
    return {
        "name": "Showcase switch",
        "description": "Routes 0200 to the issuer fleet and relays the response.",
        "trigger": {"connection": "showcase_switch_in"},
        "nodes": [
            {"id": "t", "kind": "trigger"},
            {"id": "call", "kind": "action", "target": GROUP_ID,
             "action": "send_message",
             "config": {"connection": GROUP_ID},
             "payload": {"mti": "0200", "echo": ["2", "4", "11", "37", "41"]}},
            {"id": "reply", "kind": "reply",
             "payload": {"mti": "0210", "echo": ["11", "37", "41"],
                         "set": {"39": "${call.response.response_code}"}}},
        ],
        "edges": [{"source": "t", "source_port": "out", "target": "call"},
                  {"source": "call", "source_port": "out", "target": "reply"}],
    }


def issuer_group() -> dict:
    return {"name": "Showcase issuers", "adapter_type": "iso8583",
            "members": [{"connection": "showcase_issuer_out"}],
            "selection": {"policy": "round_robin"}}


def driver_scenario() -> dict:
    return {
        "name": DRIVER_NAME,
        "description": "Sends a purchase 0200 at the switch and asserts approval.",
        "steps": [
            {"id": "purchase", "target": "tcp_iso8583", "action": "send_message",
             # select the switch connection explicitly (don't rely on the
             # deployment's default-connection resolution)
             "config": {"connection": "showcase_switch_out"},
             "payload": {"mti": "0200",
                         "values": {"2": "4111111111111111", "4": "000000010000",
                                    "11": "000001", "41": "TERM0001"}},
             "assertions": [{"field": "response_code", "operator": "eq",
                             "expected": "00"}]},
        ],
    }


def showcase_environment() -> dict:
    """A live *selector* environment (no ``mode: mock``): every step uses
    its own connection, so the driver dials the real switch instead of the
    mock adapter. The autostart initiator runs against this, not ``mock``."""
    return {
        "name": ENV_NAME,
        "description": ("Live selector environment for the showcase network "
                        "(no mock mode \u2014 the driver dials the real switch)."),
        "adapters": {},
    }


def simulator_config() -> dict:
    return {"label": SIM_LABEL,
            "config": {"protocol": "payshield", "host": "127.0.0.1", "port": 9500}}


def network_doc(driver_id: str, sim_id: str) -> dict:
    """participant switch → group → participant issuers (×3), + payShield sim +
    the driver as an autostart initiator. Wiring gives the callee-first order.
    ``driver_id``/``sim_id`` are the server-assigned ids resolved at install."""
    return {
        "name": "Showcase network",
        "description": "Acquiring demo: driver → switch → 3 issuers, payShield alongside.",
        "nodes": [
            {"id": "hsm", "kind": "simulator", "config": {"simulator_id": sim_id}},
            {"id": "issuers", "kind": "participant",
             "config": {"flow_id": ISSUER_FLOW, "instances": 3}},
            {"id": "fleet", "kind": "group", "config": {"group_id": GROUP_ID}},
            {"id": "switch", "kind": "participant",
             "config": {"flow_id": SWITCH_FLOW, "instances": 1}},
            {"id": "driver", "kind": "scenario",
             "config": {"scenario_id": driver_id, "autostart": True,
                        "environment": ENV_NAME}},
        ],
        "edges": [
            {"source": "switch", "target": "fleet"},   # switch → group …
            {"source": "fleet", "target": "issuers"},  # … → issuer fleet
            {"source": "driver", "target": "switch"},  # driver drives the switch
        ],
    }
