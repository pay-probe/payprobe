"""Step catalog — the node types the visual constructor offers.

Served at GET /catalog so the portal palette and the variable-reference
autocomplete are data-driven. ``response_fields`` lists the fields a step's
response exposes for ``${step_xxx.response.<field>}`` references.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ParamSpec(BaseModel):
    """A typed configuration parameter for a step action.

    Lets the editor render the right control (text / number / toggle / dropdown)
    instead of free-text JSON. ``options`` makes a field selectable (enum).

    ``format`` renders a Message Format (dialect) picker: choosing one snapshots
    its DE table into the step (``fields``) and records the id in this param.
    ``protocol`` filters which formats are offered. ``options_from_format`` marks
    an ``enum`` param whose choices come from the selected dialect (e.g. the MTI
    list on ``definition.mti``), falling back to the static ``options`` when no
    dialect is bound — this is how the MTI menu follows the chosen dialect.
    """

    name: str
    label: str = ""
    type: Literal["string", "number", "boolean", "enum", "json", "format"] = "string"
    options: list[str] = []
    required: bool = False
    default: Any = None
    placeholder: str = ""
    #: For ``type == "format"``: only offer formats of this protocol.
    protocol: str = ""
    #: For ``type == "enum"``: source options from the bound dialect's ``mti``.
    options_from_format: bool = False


class ActionSpec(BaseModel):
    name: str
    label: str
    payload_hint: dict[str, str] = {}
    response_fields: list[str] = []
    #: Typed parameter schema (preferred over ``payload_hint`` by the editor).
    params: list[ParamSpec] = []
    #: How a step using this action runs. ``None``/``adapter`` => the worker
    #: dispatches to the backend adapter named by the target (built-in steps).
    #: ``http`` / ``code`` => a custom, self-contained step: dropping it on the
    #: canvas materialises an ``http`` / ``code`` node from ``behavior.template``,
    #: so it runs with no adapter required. Shape::
    #:   {"kind": "http",  "template": { ...http node config... }}
    #:   {"kind": "code",  "template": { "language": "python", "code": "..." }}
    behavior: dict[str, Any] | None = None


class TargetSpec(BaseModel):
    target: str
    label: str
    category: str
    color: str
    #: One-line human description of the target, shown under its palette group
    #: (what protocol/system it drives and what the actions do).
    description: str = ""
    #: Category logo — a name from the portal's ``pp-icon`` set (e.g. ``lock``
    #: for HSM/crypto, ``globe`` for HTTP, ``network`` for ISO 8583, ``message``
    #: for NATS). Blank ⇒ the palette falls back to a plain colour dot.
    icon: str = ""
    actions: list[ActionSpec] = Field(default_factory=list)
    #: True when this target is a user-defined or user-overridden catalog entry
    #: (set by the merge layer; persisted custom docs may omit it).
    custom: bool = False


# Common enums so frequent fields are selectable instead of free text.
_CURRENCIES = ["GEL", "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "CNY", "TRY"]
_CARD_PROFILES = [
    "visa_credit_standard", "visa_credit", "visa_debit",
    "mastercard_credit", "mastercard_debit", "amex", "discover",
]
_POS_ENTRY_MODES = ["011", "021", "051", "071", "081", "091"]  # manual/magstripe/chip/contactless/…


def _p_card_profile() -> ParamSpec:
    return ParamSpec(name="card_profile", label="Card profile", type="enum",
                     options=_CARD_PROFILES, required=True, default="visa_credit_standard")


def _p_amount() -> ParamSpec:
    return ParamSpec(name="amount", label="Amount (minor units)", type="number",
                     required=True, placeholder="10000")


def _p_currency() -> ParamSpec:
    return ParamSpec(name="currency", label="Currency", type="enum",
                     options=_CURRENCIES, default="GEL")


STEP_CATALOG: list[TargetSpec] = [
    TargetSpec(
        target="http",
        label="HTTP / REST",
        category="Payment Server",
        color="#00d4aa",
        icon="globe",
        description="REST/HTTP payment API — send auth, reversal and refund "
                    "requests or an echo test against a payment server.",
        actions=[
            ActionSpec(
                name="send_auth_request",
                label="Auth request",
                payload_hint={"amount": "int (minor units)", "currency": "string", "card_profile": "string"},
                params=[_p_amount(), _p_currency(), _p_card_profile()],
                response_fields=["response_code", "auth_code", "rrn", "stan", "duration_ms"],
            ),
            ActionSpec(
                name="send_reversal",
                label="Reversal",
                payload_hint={"rrn": "string"},
                response_fields=["response_code", "rrn", "duration_ms"],
            ),
            ActionSpec(
                name="send_refund",
                label="Refund",
                payload_hint={"rrn": "string", "amount": "int (minor units)"},
                response_fields=["response_code", "rrn", "duration_ms"],
            ),
            ActionSpec(
                name="echo_test",
                label="Echo test",
                response_fields=["response_code", "duration_ms"],
            ),
        ],
    ),
    TargetSpec(
        # The scenario `predict` step (ADR-0005): mid-flow model inference
        # against the advisory insight service. The response feeds downstream
        # if/switch nodes — the author's control flow. Gates never see it.
        target="insight",
        label="Model Insight",
        category="Intelligence",
        color="#f0883e",
        icon="sparkles",
        description="Advisory ML insight service — categorize or explain a "
                    "failure and predict a scenario's outcome, mid-flow.",
        actions=[
            ActionSpec(
                name="categorize",
                label="Categorize message / failure",
                payload_hint={"text": "string (e.g. ${step.response.error})",
                              "model_id": "string (optional: pin one model)",
                              "context": "optional: target/action/response_code/"
                                         "environment/duration_ms → feature channels"},
                params=[
                    ParamSpec(name="text", label="Text to categorize",
                              type="string", required=True,
                              placeholder="${auth_step.response.error}"),
                    ParamSpec(name="model_id", label="Model (optional)",
                              type="string",
                              placeholder="cm-… — blank = active chain"),
                ],
                response_fields=["category", "label", "heuristic",
                                 "confidence", "novel", "model_version"],
            ),
            ActionSpec(
                name="explain",
                label="Explain message / failure",
                payload_hint={"text": "string (e.g. ${step.response.error})",
                              "model_id": "string (optional)"},
                params=[
                    ParamSpec(name="text", label="Text to explain",
                              type="string", required=True,
                              placeholder="${auth_step.response.error}"),
                    ParamSpec(name="model_id", label="Model (optional)",
                              type="string",
                              placeholder="cm-… — blank = active chain"),
                ],
                response_fields=["category", "label", "why", "fix",
                                 "explanation", "confidence", "novel"],
            ),
            ActionSpec(
                name="predict_outcome",
                label="Predict scenario outcome",
                payload_hint={"scenario_id": "string",
                              "environment": "string (optional)"},
                params=[
                    ParamSpec(name="scenario_id", label="Scenario id",
                              type="string", required=True,
                              placeholder="sc-42"),
                    ParamSpec(name="environment", label="Environment (optional)",
                              type="string", placeholder="staging"),
                ],
                response_fields=["p_fail_next", "p_flaky", "basis",
                                 "n_history", "top_factors", "model_version"],
            ),
            ActionSpec(
                name="model_status",
                label="Model status (pre-flight)",
                response_fields=["corpus_size", "custom_model_active",
                                 "custom_model", "cluster_model_active",
                                 "predictor_learned", "sklearn_available"],
            ),
            ActionSpec(
                name="train",
                label="Ingest + retrain (pipeline step)",
                response_fields=["sync", "train"],
            ),
        ],
    ),
    TargetSpec(
        # HSM over TCP (header-echo protocol on the universal TCP adapter): the
        # host echoes a leading header used to correlate the reply.
        # ``send_command`` is the universal form; the higher-level ops are
        # conveniences (and resolve against the mock adapter for local runs).
        # Bind a header_echo Connection to target a specific HSM.
        target="hsm",
        label="HSM",
        category="Crypto",
        color="#d2a8ff",
        icon="lock",
        description="Hardware Security Module over TCP — send host commands, "
                    "generate/translate keys and verify PIN blocks.",
        actions=[
            ActionSpec(
                name="send_command",
                label="Send host command",
                payload_hint={"command": "string", "data": "string"},
                params=[
                    ParamSpec(name="command", label="Command code", type="string",
                              required=True, placeholder="A0 (e.g. generate key)"),
                    ParamSpec(name="data", label="Command data", type="string",
                              placeholder="command-specific payload"),
                    ParamSpec(name="header", label="Header (optional)", type="string",
                              placeholder="auto-generated if blank"),
                ],
                response_fields=["header", "command", "response_code", "data"],
            ),
            ActionSpec(
                name="diagnostics",
                label="Diagnostics / health",
                payload_hint={"command": "string"},
                params=[ParamSpec(name="command", label="Command code",
                                  type="string", default="NC")],
                response_fields=["header", "command", "response_code", "data"],
            ),
            ActionSpec(
                name="verify_pin_block",
                label="Verify PIN block",
                payload_hint={"pin_block": "hex", "pan": "string"},
                response_fields=["status"],
            ),
            ActionSpec(
                name="generate_key",
                label="Generate key",
                payload_hint={"key_type": "string"},
                response_fields=["status", "kcv"],
            ),
            ActionSpec(
                name="translate_pin",
                label="Translate PIN",
                payload_hint={"source_key": "string", "dest_key": "string"},
                response_fields=["status"],
            ),
        ],
    ),
    TargetSpec(
        target="db_probe_core",
        label="DB Probe",
        category="Database",
        color="#79c0ff",
        icon="database",
        description="Query the core banking database to assert on persisted "
                    "transactions and account balances.",
        actions=[
            ActionSpec(
                name="query_transaction",
                label="Query transaction",
                payload_hint={"rrn": "string"},
                response_fields=["status", "amount", "rrn"],
            ),
            ActionSpec(
                name="query_balance",
                label="Query balance",
                payload_hint={"account_id": "string"},
                response_fields=["status", "balance"],
            ),
        ],
    ),
    TargetSpec(
        # Universal persistent TCP / ISO 8583 link. Backed by the worker
        # TcpAdapter: one long-lived socket multiplexes many messages, correlated
        # by STAN (DE 11). This single group covers any ISO 8583 host — switch,
        # acquirer, issuer simulator — over TCP; bind a Connection to target a
        # specific one. Wire format (length prefix, TPDU, sign-on, keepalive) is
        # set per connection/environment; steps just choose an MTI + field values.
        target="tcp_iso8583",
        label="TCP / ISO 8583",
        category="ISO 8583",
        color="#d29922",
        icon="network",
        description="Persistent TCP link to any ISO 8583 host (switch, acquirer "
                    "or issuer) — pick an MTI and data-element values to send.",
        actions=[
            ActionSpec(
                name="send_message",
                label="Send ISO message",
                payload_hint={"mti": "string", "values": "json (DE -> value)"},
                params=[
                    ParamSpec(name="message_format_id", label="Dialect (Message Format)",
                              type="format", protocol="iso8583",
                              placeholder="environment default (ISO 8583:1987)"),
                    # MTI menu follows the chosen dialect (1987 → 0xxx, 1993/NT →
                    # 1xxx); options below are the 1987 fallback when none is set.
                    ParamSpec(name="mti", label="MTI", type="enum",
                              options=["0100", "0200", "0220", "0400", "0420",
                                       "0500", "0800"],
                              options_from_format=True,
                              required=True, default="0200"),
                    ParamSpec(name="values", label="Field values (DE → value)",
                              type="json", default={},
                              placeholder='{"2":"4111111111111111","4":"000000010000"}'),
                    ParamSpec(name="fields", label="DE table override (optional)",
                              type="json", default={},
                              placeholder="leave empty to use the dialect / environment default"),
                ],
                response_fields=["mti", "response_code", "stan", "rrn",
                                 "auth_code", "fields"],
            ),
            ActionSpec(
                name="send_0100",
                label="0100 authorization request",
                payload_hint={"amount": "int (minor units)", "pan": "string"},
                params=[
                    _p_amount(),
                    ParamSpec(name="pan", label="PAN", type="string",
                              placeholder="4111111111111111"),
                    _p_currency(),
                ],
                response_fields=["mti", "response_code", "stan", "auth_code"],
            ),
            ActionSpec(
                name="send_0200",
                label="0200 financial request",
                payload_hint={"amount": "int (minor units)", "pan": "string"},
                params=[
                    _p_amount(),
                    ParamSpec(name="pan", label="PAN", type="string",
                              placeholder="4111111111111111"),
                    ParamSpec(name="pos_entry_mode", label="POS entry mode (DE22)",
                              type="enum", options=_POS_ENTRY_MODES, default="051"),
                    _p_currency(),
                ],
                response_fields=["mti", "response_code", "stan", "rrn", "auth_code"],
            ),
            ActionSpec(
                name="send_0400",
                label="0400 reversal request",
                payload_hint={"rrn": "string", "amount": "int (minor units)"},
                params=[
                    ParamSpec(name="rrn", label="RRN (DE37)", type="string"),
                    _p_amount(),
                ],
                response_fields=["mti", "response_code", "stan"],
            ),
            ActionSpec(
                name="send_0420",
                label="0420 reversal advice",
                payload_hint={"rrn": "string"},
                params=[ParamSpec(name="rrn", label="RRN (DE37)", type="string")],
                response_fields=["mti", "response_code", "stan"],
            ),
            ActionSpec(
                name="send_0500",
                label="0500 settlement / batch",
                response_fields=["mti", "response_code", "stan"],
            ),
            ActionSpec(
                name="send_0800",
                label="0800 network echo / sign-on",
                response_fields=["mti", "response_code"],
            ),
        ],
    ),
    TargetSpec(
        # Broker-mediated NATS messaging (ADR-0006). Backed by the worker
        # NatsAdapter: it connects OUT to the broker and rendezvouses on a
        # subject — bind a NATS Connection to pick the broker + default subject.
        # `subject`/`timeout` are control keys (routing, not body); the request
        # reply lands under `data`.
        target="nats",
        label="NATS (messaging)",
        category="Messaging",
        color="#27aae1",
        icon="message",
        description="Broker-mediated NATS messaging — request/reply, publish "
                    "or ack-checked JetStream publish on a subject.",
        actions=[
            ActionSpec(
                name="request",
                label="Request / reply",
                payload_hint={"subject": "string (optional)",
                              "body": "json (the message)",
                              "timeout": "float seconds (optional)"},
                params=[
                    ParamSpec(name="subject", label="Subject (optional)", type="string",
                              placeholder="pay.auth — else the connection's default subject"),
                    ParamSpec(name="body", label="Request body (JSON)", type="json",
                              default={},
                              placeholder='{"pan":"4111111111111111","amount":4200}'),
                    ParamSpec(name="timeout", label="Reply timeout (s, optional)",
                              type="number",
                              placeholder="connection request_timeout_sec"),
                ],
                response_fields=["subject", "reply_subject", "data"],
            ),
            ActionSpec(
                name="publish",
                label="Publish (fire-and-forget)",
                payload_hint={"subject": "string (optional)", "body": "json (the message)"},
                params=[
                    ParamSpec(name="subject", label="Subject (optional)", type="string",
                              placeholder="pay.event — else the connection's default subject"),
                    ParamSpec(name="body", label="Message body (JSON)", type="json",
                              default={},
                              placeholder='{"event":"captured","rrn":"..."}'),
                ],
                response_fields=["subject", "published", "bytes"],
            ),
            ActionSpec(
                name="js_publish",
                label="JetStream publish (ack-checked)",
                payload_hint={"subject": "string (optional)", "body": "json (the event)"},
                params=[
                    ParamSpec(name="subject", label="Subject (optional)", type="string",
                              placeholder="pay.events"),
                    ParamSpec(name="body", label="Event body (JSON)", type="json",
                              default={}),
                ],
                response_fields=["subject", "stream", "seq", "duplicate", "acked"],
            ),
        ],
    ),
]

# Pre-built EMV / payment helper steps (code-backed: parsers, converters,
# combinators). Appended here so they ship in the default palette.
from .emv_catalog import EMV_TOOLS_TARGET  # noqa: E402
from .emv_crypto_catalog import EMV_CRYPTO_TARGET  # noqa: E402
from .iso_catalog import ISO_MESSAGING_TARGET  # noqa: E402
from .table_catalog import DATA_TABLES_TARGET  # noqa: E402

STEP_CATALOG.append(EMV_TOOLS_TARGET)
STEP_CATALOG.append(EMV_CRYPTO_TARGET)
STEP_CATALOG.append(ISO_MESSAGING_TARGET)
STEP_CATALOG.append(DATA_TABLES_TARGET)
