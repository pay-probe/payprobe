/**
 * Adapter catalog — the single source of truth for the Adapters reference
 * pages (the `/adapters` grid and the `/adapters/:slug` detail page).
 *
 * This mirrors the worker AdapterRegistry (packages/worker/adapters/registry.py)
 * and the adapter sources it resolves to. It is a *read-only* reference: real
 * availability on a given worker depends on the installed optional dependencies
 * and the active environment config. Each entry carries deep, source-derived
 * detail — config keys (with types/defaults), actions, protocol/framing notes,
 * worked examples and error behaviour — so the detail page is an accurate spec.
 */

export type Availability = "builtin" | "optional" | "planned";

export interface ConfigKey {
  key: string;
  /** Value type, e.g. "string", "int", "bool", "object", "string[]". */
  type?: string;
  required?: boolean;
  /** Default applied by the adapter when the key is omitted. */
  default?: string;
  desc: string;
}

export interface ConfigGroup {
  title: string;
  /** Optional one-line note about when this group applies. */
  note?: string;
  keys: ConfigKey[];
}

export interface ActionSpec {
  name: string;
  summary: string;
  /** Key payload parameters this action reads. */
  params?: string;
  /** Notable response fields produced. */
  returns?: string;
}

export interface CodeExample {
  title: string;
  lang: "json" | "yaml" | "text";
  code: string;
}

export interface ExtraSection {
  /** Anchor id (also used in the on-this-page nav). */
  id: string;
  title: string;
  paragraphs: string[];
  /** Optional bullet points rendered under the paragraphs. */
  points?: string[];
}

export interface AdapterSpec {
  /** Route key used at /adapters/:slug. */
  slug: string;
  /** Target key(s) used in environment config / scenario steps. */
  targets: string[];
  label: string;
  /** Short grouping shown as a chip, e.g. "Transport", "Routing". */
  category: string;
  /** One-line summary for the catalog grid. */
  description: string;
  availability: Availability;
  /** Wire protocol(s) this adapter speaks. */
  protocols?: string[];
  /** "outbound" (client/dial), "inbound" (listener), or both. */
  direction?: string;
  /** Optional dependency required to enable it. */
  dependency?: string;
  /** Worker source file backing this adapter. */
  source?: string;
  /** Longer prose overview, paragraph per entry. */
  overview: string[];
  /** Protocol / framing / correlation detail, paragraph per entry. */
  protocolNotes?: string[];
  /** Additional rich sections rendered after the protocol notes. */
  extraSections?: ExtraSection[];
  /** Grouped config tables. */
  configGroups?: ConfigGroup[];
  /** Actions a step / flow node can invoke. */
  actions?: ActionSpec[];
  /** Worked examples (env config, step, …). */
  examples?: CodeExample[];
  /** Error / failure behaviour notes. */
  errors?: string[];
  /** Free-text caveats. */
  notes?: string;
  /** Related adapter slugs. */
  seeAlso?: string[];
}

/**
 * Shared "Dialects & validation" subsection for the ISO 8583 transports
 * (tcp / switch / acquirer). `lead` adapts the opening sentence per adapter.
 */
function isoDialectSection(lead: string): ExtraSection {
  return {
    id: "dialects",
    title: "Dialects & validation",
    paragraphs: [
      lead +
        " The DE table the message is packed with is a dialect: a step can snapshot a saved Message Format into its fields so the same flow targets different integrations just by switching the format. With no per-step format, packing falls back to the connection's default field table (field_map maps friendly keys like pan / amount / stan to DEs, values{} sets DEs directly, and auto_fields stamps the transmission date/time elements).",
      "The codec packs and parses fixed-width and variable-length DEs with 2-, 3-, 4- and 5-digit ASCII length prefixes (llvar / lllvar / llllvar / lllllvar), so wide data elements round-trip correctly. Maintain dialects under Message Formats and inspect/build raw messages with the ISO 8583 Inspector.",
      "A Message Format can also carry a presence matrix — the mandatory (and conditional) DEs per MTI. When the peer is a PayProbe simulator with that format bound, it validates each inbound message against the matrix: warn mode records violations on the decoded message (visible in the execution trace) but still answers; reject mode short-circuits to an error reply (e.g. DE 39 = 30) echoing DE 11 / 37. This gives end-to-end dialect conformance without hand-writing assertions.",
    ],
    points: [
      "ISO 8583:1987 — classic 0xxx MTIs",
      "ISO 8583:1993 — 1xxx MTIs",
      "VISA — VISA field set; clone any builtin into a custom dialect",
    ],
  };
}

export const ADAPTERS: AdapterSpec[] = [
  // ---------------------------------------------------------------- mock ----
  {
    slug: "mock",
    targets: ["mock"],
    label: "Mock",
    category: "Utility",
    description:
      "Built-in stand-in that returns canned responses — no real system required. Used in mock-mode environments and CI.",
    availability: "builtin",
    direction: "outbound",
    protocols: ["in-memory"],
    source: "packages/worker/adapters/mock/adapter.py",
    overview: [
      "MockAdapter is an in-memory adapter for local development and CI. It returns configurable canned responses with a small simulated latency and never touches a real target, so flows can be authored and exercised before any host is wired up.",
      "It is selected automatically when an environment runs in mock mode, or per-target when a target opts into mock. Every action reports success unless the adapter is explicitly forced unhealthy.",
    ],
    configGroups: [
      {
        title: "Config",
        keys: [
          {
            key: "responses",
            type: "object",
            desc: "Canned responses keyed by action name. Overrides the built-in defaults for that action.",
          },
          {
            key: "latency_ms",
            type: "int",
            default: "5",
            desc: "Simulated per-call latency, applied to both execute() and health_check().",
          },
          {
            key: "force_unhealthy",
            type: "bool",
            default: "false",
            desc: "When true, health_check() returns false — useful to test degraded-target handling.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "(any action)",
        summary:
          'Returns the matching entry from responses, else the built-in default for that action, else {"status": "ok"}.',
        returns: "The canned response object",
      },
      {
        name: "send_0200 / send_0100 / send_0420",
        summary:
          "ISO-style canned replies (e.g. send_0200 → {mti: 0210, response_code: 00, rrn}).",
      },
      {
        name: "echo_test",
        summary:
          "Heartbeat reply carrying both status and response_code ({status: ok, response_code: 00}).",
      },
      {
        name: "send_auth_request / send_refund / send_reversal",
        summary:
          "RestPay-style payment-lifecycle canned replies with response_code / auth_code / rrn.",
      },
    ],
    examples: [
      {
        title: "Environment adapter entry",
        lang: "json",
        code: `{
  "adapter": "mock",
  "latency_ms": 20,
  "responses": {
    "send_0200": { "mti": "0210", "response_code": "00", "auth_code": "654321" }
  }
}`,
      },
    ],
    errors: [
      "execute() always reports success — Mock is for happy-path wiring, not failure injection.",
      "health_check() returns false only when force_unhealthy is set.",
    ],
    seeAlso: ["http", "tcp"],
  },

  // ----------------------------------------------------------------- mcp ----
  {
    slug: "mcp",
    targets: ["mcp"],
    label: "MCP client (provider control plane)",
    category: "Intelligence",
    description:
      "Generic Model Context Protocol client: call tools on any MCP server — Stripe/PayPal remote, Adyen self-hosted, or any future provider — as scenario steps.",
    availability: "optional",
    direction: "outbound",
    protocols: ["MCP over streamable HTTP", "MCP over stdio"],
    source: "packages/worker/adapters/mcp_client/adapter.py",
    overview: [
      "The control plane of ADR-0009 (phase 3). Every major payment provider now publishes an MCP server; this one adapter reaches all of them — and any future one — with zero per-provider code. PayProbe already serves MCP (packages/mcp-server); this is the symmetric half: PayProbe as MCP client.",
      'What it is FOR: fixture provisioning and provider-side verification as scenario steps — "create a test customer before the run", "after the capture scenario, search the provider\'s records and assert the payment exists". It is not a payment transport: scenarios drive payments through the http data plane (provider packs), and remote provider MCP connections ship external: true so the load engine refuses them.',
      "Sessions are per call (open → initialize → operate → close), not held across calls: the mcp SDK's transports are anyio task groups bound to the entering task, and the engine may execute steps from different tasks. For a control-plane adapter the extra handshake per step is the honest trade.",
    ],
    protocolNotes: [
      "A tool reply lands in the response as { tool, is_error, content: [...], structured: {...}, text } — `text` joins the text blocks so simple assertions need no indices, while content[0].text / structured.* stay reachable via bracket-index assertion paths.",
      "A tool-level error (isError) fails the step — that is the point of a verification step.",
      "The `mcp` SDK is an optional dependency (grpcio/nats-py precedent): the adapter registers only when it is installed.",
    ],
    configGroups: [
      {
        title: "Transport",
        keys: [
          {
            key: "transport",
            type: "string",
            default: "http",
            desc: '"http" (streamable HTTP) or "stdio" (spawn a local server process).',
          },
          {
            key: "base_url",
            type: "string",
            desc: "MCP server URL for the http transport (e.g. https://mcp.stripe.com).",
          },
          {
            key: "command",
            type: "string",
            desc: "Executable for the stdio transport (e.g. npx).",
          },
          {
            key: "args",
            type: "list",
            desc: 'Arguments for the stdio command (e.g. ["-y", "@stripe/mcp"]).',
          },
          {
            key: "env",
            type: "object",
            desc: "Extra environment for the stdio server process.",
          },
        ],
      },
      {
        title: "Auth & limits",
        keys: [
          {
            key: "authentication",
            type: "object",
            desc: '{type: "bearer", token} or {type: "header", headerName, headerValue} — secret-named fields stored encrypted.',
          },
          {
            key: "headers",
            type: "object",
            desc: "Extra HTTP headers for the http transport.",
          },
          {
            key: "request_timeout_sec",
            type: "float",
            default: "30",
            desc: "Per-call deadline (handshake + tool call).",
          },
          {
            key: "external",
            type: "bool",
            default: "false",
            desc: "Marks a REAL provider surface: the load engine refuses it (ADR-0009 guardrail; ship it true on remote provider presets).",
          },
        ],
      },
    ],
    actions: [
      {
        name: "list_tools",
        summary:
          "The server's tool inventory: {tools: [{name, description, input_schema}], count}.",
      },
      {
        name: "call_tool",
        summary: "Explicit form: payload {name, arguments} calls one tool.",
      },
      {
        name: "(any tool name)",
        summary:
          'Convenience form: the action IS the tool name and the payload IS its arguments — action: "create_customer" just works (mirrors the http adapter\'s bare-action fallback).',
      },
    ],
    errors: [
      "The mcp SDK missing → the adapter never registers; install it (pip install mcp) to enable.",
      "isError tool replies fail the step with the tool's error text.",
      "Cancel-scope/task-affinity errors are designed out: sessions never span calls.",
    ],
    seeAlso: ["http", "insight"],
  },

  // ---------------------------------------------------------------- http ----
  {
    slug: "insight",
    targets: ["insight"],
    label: "Model Insight (predict step)",
    category: "Intelligence",
    description:
      "Mid-flow model inference against the advisory insight service: categorize a message/failure or predict a scenario's outcome, then branch on the answer.",
    availability: "builtin",
    direction: "outbound",
    protocols: ["REST / HTTP(S)"],
    source: "packages/worker/adapters/insight/adapter.py",
    overview: [
      "The scenario `predict` step (ADR-0005 extension). A call node with this adapter POSTs to the insight service's /infer endpoint and exposes the model's answer as ${step.response.*}, so downstream if/switch nodes can branch on it — pre-flight risk checks, categorize-then-route flows, chaos-aware assertions.",
      "The advise-only boundary holds: the output feeds the scenario author's own control flow, never the report gates or Go/No-Go sign-off. Categorization precedence is the service's usual chain — active custom model, then discovered clusters, then the heuristic taxonomy.",
    ],
    protocolNotes: [
      "categorize needs a `text` param (typically ${some_step.response.error}) and returns { category, label, heuristic, confidence, novel, model_version }.",
      "predict_outcome needs `scenario_id` (+ optional environment) and returns { p_fail_next, p_flaky, basis, n_history, model_version }. Inference is side-effect-free — step calls are not logged into the service's self-scoring loop.",
    ],
    configGroups: [
      {
        title: "Connection",
        keys: [
          {
            key: "base_url",
            type: "string",
            desc: "Insight service base; defaults to $INSIGHT_API_URL (compose: http://insight-service:8500).",
          },
          {
            key: "token",
            type: "string",
            desc: "Optional static bearer for the service gate; else API_TOKEN / a JWT minted from AUTH_JWT_SECRET.",
          },
          {
            key: "response_timeout_sec",
            type: "float",
            default: "10",
            desc: "Per-inference deadline.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "categorize",
        summary:
          "Classify failure/decline text with the active model chain (or one pinned model).",
        params: "text (e.g. ${auth_step.response.error}), model_id?",
        returns:
          "{ category, label, heuristic, confidence, novel, model_version }",
      },
      {
        name: "explain",
        summary:
          "Categorize AND say why + the suggested fix — attachable to reports/alerts mid-flow.",
        params: "text, model_id?",
        returns:
          "{ category, label, why, fix, explanation, confidence, novel }",
      },
      {
        name: "predict_outcome",
        summary: "Estimate p(fail) / p(flaky) for a scenario from run history.",
        params: "scenario_id, environment?",
        returns:
          "{ p_fail_next, p_flaky, basis, n_history, top_factors, model_version }",
      },
      {
        name: "model_status",
        summary:
          "Pre-flight assert: which models are live before a flow relies on them.",
        params: "(none)",
        returns:
          "{ corpus_size, custom_model_active, custom_model, cluster_model_active, predictor_learned, sklearn_available }",
      },
      {
        name: "train",
        summary:
          "Pipeline step: ingest new completed runs + refit (idempotent, own store only).",
        params: "(none)",
        returns: "{ sync: {runs_ingested, …}, train: {fitted, …} }",
      },
    ],
  },
  {
    slug: "http",
    targets: ["http"],
    label: "HTTP / REST (generic)",
    category: "Connection",
    description:
      "Reusable REST connection: a base URL plus named actions (method + path), callable by name with per-environment overrides.",
    availability: "builtin",
    direction: "outbound",
    protocols: ["REST / HTTP(S)"],
    source: "packages/worker/adapters/http/adapter.py",
    overview: [
      "Where the engine's http node makes a one-off request authored per step, HttpAdapter is a reusable connection — a base URL plus named actions callable from scenarios, participant flows and groups, and overridable per environment.",
      "It reuses the engine's run_http() helper, so request building, authentication, body shaping and ${...} interpolation are identical to the http node. A step names an action and passes a payload; the adapter resolves the action's method + path against base_url and returns the parsed response.",
    ],
    protocolNotes: [
      'Write methods (POST, PUT, PATCH, DELETE) send the payload as a JSON body; GET sends none. An action that isn\'t declared falls back to using its name as a path, so a bare "/health"-style call works without a declared action.',
      "The response_payload is { status_code, headers, body }; success follows the HTTP result's ok flag. Path, headers and body can interpolate ${request.*} from the payload.",
    ],
    configGroups: [
      {
        title: "Connection",
        keys: [
          {
            key: "base_url",
            type: "string",
            required: true,
            desc: "Service base URL; a trailing slash is trimmed.",
          },
          {
            key: "headers",
            type: "object",
            desc: "Default headers sent on every call.",
          },
          {
            key: "authentication",
            type: "object",
            desc: "{ type: bearer | basic | header | query, … }. Secret values (e.g. ${vars.api_key}) are supported.",
          },
          {
            key: "options",
            type: "object",
            desc: "Passed through to the HTTP runner (timeouts, TLS verification, redirects, …).",
          },
        ],
      },
      {
        title: "Actions",
        keys: [
          {
            key: "actions",
            type: "object",
            desc: "name → { method, path }. path may use ${request.*}. Methods default to POST when a payload is present, else GET.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "<your action>",
        summary: "Resolves to the declared METHOD + path against base_url.",
        params:
          "payload → JSON body (write methods); ${request.*} interpolation",
        returns: "{ status_code, headers, body }",
      },
      {
        name: "(bare path)",
        summary:
          "An undeclared action containing a / is treated as the path itself.",
      },
    ],
    examples: [
      {
        title: "Environment adapter entry",
        lang: "json",
        code: `{
  "adapter": "http",
  "base_url": "https://api.example.com/v1",
  "headers": { "X-Tenant": "acme" },
  "authentication": { "type": "bearer", "token": "\${vars.api_key}" },
  "actions": {
    "authorize": { "method": "POST", "path": "/payments" },
    "get_order": { "method": "GET",  "path": "/orders/\${request.id}" }
  }
}`,
      },
    ],
    errors: [
      "Transport / non-2xx outcomes surface as success=false with the runner's error on the StepResult.",
      "health_check() does a best-effort GET against base_url and never raises.",
    ],
    notes:
      "For a one-off request without a saved connection, use the http node instead.",
    seeAlso: ["grpc", "mock"],
  },

  // ---------------------------------------------------------------- grpc ----
  {
    slug: "grpc",
    targets: ["grpc"],
    label: "gRPC (general)",
    category: "Connection",
    description:
      "Descriptor-driven gRPC adapter — calls any service with no generated stubs. Unary and all three streaming shapes.",
    availability: "optional",
    direction: "outbound",
    protocols: ["gRPC / HTTP-2"],
    dependency: "grpcio",
    source: "packages/worker/adapters/grpc/adapter.py",
    overview: [
      "GrpcAdapter calls any method on a gRPC service with no generated stubs. It loads a compiled proto FileDescriptorSet and invokes methods dynamically, supporting all four RPC shapes — unary, server-streaming, client-streaming and bidirectional streaming. Message types and streaming flags are derived from the descriptors, so a step just names a method and passes a JSON-shaped payload.",
      "grpcio is imported lazily (only at connect()), so the worker package imports fine without it installed.",
    ],
    protocolNotes: [
      "The schema can come from three sources, merged in order: (1) a precompiled FileDescriptorSet (descriptor_set_path / _b64); (2) custom .proto sources compiled on the fly (proto_inline / proto_paths); (3) discovery via server reflection (reflection: true). At least one must be configured.",
      "Payload shape: for unary / server-streaming it's the request object as a dict; for client-streaming / bidi it's { messages: [ {...}, {...} ] }. An action not in actions is treated as the method name itself.",
    ],
    configGroups: [
      {
        title: "Target",
        keys: [
          {
            key: "target",
            type: "string",
            desc: '"host:port" — or supply host + port separately.',
            default: "localhost:50051",
          },
          {
            key: "tls",
            type: "object",
            desc: "{ enabled, root_certs_path } — enable TLS with optional root certs.",
          },
          {
            key: "metadata",
            type: "object",
            desc: "Call metadata headers (key/value), e.g. authorization.",
          },
          {
            key: "timeout_sec",
            type: "float",
            default: "30",
            desc: "Default per-call deadline.",
          },
        ],
      },
      {
        title: "Schema source (one or more)",
        keys: [
          {
            key: "descriptor_set_path / descriptor_set_b64",
            type: "string",
            desc: "A precompiled FileDescriptorSet (file path or base64).",
          },
          {
            key: "proto_inline",
            type: "string | string[] | object",
            desc: "Raw .proto text compiled on the fly.",
          },
          {
            key: "proto_paths",
            type: "string[]",
            desc: ".proto files on the worker host.",
          },
          {
            key: "proto_include_dirs",
            type: "string[]",
            desc: "Extra -I import roots for compilation.",
          },
          {
            key: "reflection",
            type: "bool",
            default: "false",
            desc: "Discover the schema from the live server via gRPC reflection.",
          },
        ],
      },
      {
        title: "Actions",
        keys: [
          {
            key: "actions",
            type: "object",
            desc: 'Friendly name → { method: "package.Service/Method" }.',
          },
        ],
      },
    ],
    actions: [
      {
        name: "$discover",
        summary:
          "Returns the catalog of callable methods (aliases: __discover__, __list_methods__). Handy after reflection.",
      },
      {
        name: "<your action>",
        summary: "Invokes the mapped package.Service/Method.",
        params:
          "request dict, or { messages: [...] } for client/bidi streaming",
      },
    ],
    examples: [
      {
        title: "Environment adapter entry (reflection)",
        lang: "json",
        code: `{
  "adapter": "grpc",
  "target": "localhost:50051",
  "reflection": true,
  "metadata": { "authorization": "Bearer \${vars.token}" },
  "actions": {
    "say_hello": { "method": "helloworld.Greeter/SayHello" }
  }
}`,
      },
    ],
    errors: [
      "connect() raises if no schema source is configured.",
      "RPC errors surface on the StepResult; deadlines come from timeout_sec.",
    ],
    seeAlso: ["http"],
  },

  // ---------------------------------------------------------------- nats ----
  {
    slug: "nats",
    targets: ["nats"],
    label: "NATS",
    category: "Transport",
    description:
      "Broker-mediated messaging (ADR-0006): publish / request-reply / JetStream over subjects. Acts as either side — outbound client or a subscriber simulator — against an external NATS cluster.",
    availability: "optional",
    direction: "both",
    protocols: ["nats", "jetstream"],
    dependency: "nats-py (pip install nats-py)",
    source: "packages/worker/adapters/nats/adapter.py",
    overview: [
      "Unlike the point-to-point transports, NATS is broker-mediated: nobody dials anybody. Producers and consumers both connect out to a NATS server and rendezvous on subjects. So a NATS connection's host/port (or servers[]) is the broker address, not a bind target — and NATS participants are port-less (the uniqueness resource is the subject, not a host:port). The broker itself is infrastructure: PayProbe connects to a real external 3-node JetStream cluster (infra/docker/docker-compose.yml) and chaos-tests it from the outside; it never embeds or manages the broker.",
      "The outbound NatsAdapter publishes, does request/reply, and JetStream-publishes with an ack. The inbound NatsResponder subscribes to subjects (optionally in a queue group) and answers request/reply messages with a templated body, reusing the same rules + chaos machinery as the TCP/HTTP responders. A NatsFlowResponder computes each reply by walking a participant flow, so a message on pay.auth can branch on the body, call a downstream switch/issuer/HSM, and shape a reply — the Network Trace records subject / queue group / inbox where TCP records MTI / DE 39.",
      "nats-py is an optional dependency, imported lazily: on a worker without it the adapter is simply unavailable (like grpcio / aiohttp), never an import crash.",
    ],
    protocolNotes: [
      "Payload codecs decouple the structured payload from the bytes on the subject: json (default; dict ↔ UTF-8 JSON), bytes (raw binary from hex / base64 / text), and iso8583 (reuse the Message Format dialect codecs so {mti, de:{…}} packs to ASCII ISO 8583 bytes) — no NATS-only encoding story.",
      "Queue groups are load-balanced fan-out: N responder instances sharing a subject + queue group split the traffic (that is exactly what request/reply load-testing through a queue group exercises). At plan time, the same subject with NO queue group claimed twice is a 409 — both would receive every message. (Phase 1 checks exact subject matches only; wildcard overlap like pay.> vs pay.auth.* is a documented limitation.)",
    ],
    extraSections: [
      {
        id: "jetstream",
        title: "JetStream & stop-ownership",
        paragraphs: [
          "A connection can declare a JetStream stream/consumer (jetstream: {stream, subjects[], durable, ack_wait}). It is created-if-missing and ownership-tracked: teardown deletes only the consumer this connection created and never a stream it found pre-existing or the cluster itself. A stream this connection created is deleted only when jetstream.delete_on_stop is set — streams are durable shared infrastructure other participants may still use.",
          "Resilience recipe: run request/reply load through a queue group, then a chaos storm kills one broker node (docker stop nats-2) while the resilience scorer observes absorption and recovery — the cluster is the chaos target, scored into a pass/fail certificate.",
        ],
      },
    ],
    configGroups: [
      {
        title: "Broker",
        keys: [
          {
            key: "host",
            type: "string",
            desc: "Broker address (single URL built as nats://host:port).",
          },
          {
            key: "port",
            type: "int",
            default: "4222",
            desc: "Broker client port — one port, direction is set by mode (invariant #7).",
          },
          {
            key: "servers",
            type: "string[]",
            desc: 'Explicit multi-URL cluster (wins over host/port), e.g. ["nats://nats-1:4222", "nats://nats-2:4223"].',
          },
          {
            key: "subject",
            type: "string",
            desc: "Default subject for publish/request (payload .subject overrides).",
          },
          {
            key: "subjects",
            type: "string[]",
            desc: "Inbound: subjects a responder subscribes to.",
          },
          {
            key: "queue_group",
            type: "string",
            desc: "Inbound: load-balanced queue group for fan-out across instances.",
          },
          {
            key: "codec",
            type: "string",
            default: "json",
            desc: "Payload codec: json | bytes | iso8583.",
          },
          {
            key: "request_timeout_sec",
            type: "float",
            default: "2.0",
            desc: "Reply timeout for the request action.",
          },
        ],
      },
      {
        title: "Auth (secrets, encrypted at rest)",
        note: "Masked on read; SecretBox-encrypted at rest (invariant #8).",
        keys: [
          { key: "auth.token", type: "string", desc: "Bearer token auth." },
          {
            key: "auth.user / auth.password",
            type: "string",
            desc: "User/password auth.",
          },
          {
            key: "auth.nkey_seed",
            type: "string",
            desc: "NKey seed (string form).",
          },
          {
            key: "auth.creds",
            type: "string",
            desc: "Path to a JWT credentials (.creds) file.",
          },
        ],
      },
      {
        title: "JetStream",
        note: "Optional — declares a stream/consumer, created-if-missing.",
        keys: [
          {
            key: "jetstream.stream",
            type: "string",
            desc: "Stream name to ensure.",
          },
          {
            key: "jetstream.subjects",
            type: "string[]",
            desc: "Subjects the stream binds (defaults to the connection subject).",
          },
          {
            key: "jetstream.durable",
            type: "string",
            desc: "Durable consumer name to ensure.",
          },
          {
            key: "jetstream.ack_wait",
            type: "float",
            desc: "Consumer ack wait (seconds).",
          },
          {
            key: "jetstream.delete_on_stop",
            type: "bool",
            default: "false",
            desc: "Delete a stream THIS connection created on teardown (ephemeral test streams).",
          },
        ],
      },
    ],
    actions: [
      {
        name: "publish",
        summary: "Fire-and-forget publish to subject.",
        params: "subject?, <body>",
        returns: "{subject, published, bytes}",
      },
      {
        name: "request",
        summary:
          "Request/reply: publish and await one reply within request_timeout_sec.",
        params: "subject?, timeout?, <body>",
        returns: "{subject, reply_subject, data}",
      },
      {
        name: "js_publish",
        summary:
          "JetStream publish; returns the broker ack (durable write confirmed).",
        params: "subject?, <body>",
        returns: "{subject, stream, seq, duplicate, acked}",
      },
    ],
    examples: [
      {
        title: "Outbound connection (request/reply)",
        lang: "json",
        code: `{
  "adapter": "nats",
  "host": "nats-1",
  "port": 4222,
  "subject": "pay.auth",
  "codec": "json",
  "request_timeout_sec": 2.0
}`,
      },
      {
        title: "NATS responder simulator (queue group)",
        lang: "json",
        code: `{
  "protocol": "nats",
  "host": "nats-1",
  "port": 4222,
  "subjects": ["pay.auth"],
  "queue_group": "issuers",
  "codec": "json",
  "rules": [
    { "when": { "body": { "amount": { "gte": 100000 } } },
      "respond": { "json": { "decision": "DECLINE" } } }
  ],
  "default": { "json": { "decision": "APPROVE" } }
}`,
      },
    ],
    errors: [
      "Every adapter failure (no subject, request timeout, broker down) becomes a StepResult error — execute() never raises.",
      "health_check() connects + flushes and returns False on any transport error (never raises).",
      "A NATS trigger connection on an orchestrator image without nats-py fails the participant start with a clear 500 (rebuild the image).",
    ],
    notes:
      "Portal builds on the host only — this entry is source-derived, not a claim the portal was rebuilt in-sandbox. TLS to the cluster is deferred (aligns with the ADR-0002 TLS deferral).",
    seeAlso: ["http", "tcp", "group"],
  },

  // ----------------------------------------------------------------- tcp ----
  {
    slug: "tcp",
    targets: ["tcp", "tcp_iso8583"],
    label: "TCP (universal)",
    category: "Transport",
    description:
      "Persistent, multiplexed TCP transport with a pluggable wire protocol (ISO 8583 or header-echo). Backs switch, acquirer and HSM links.",
    availability: "builtin",
    direction: "outbound (dial) or inbound (listen)",
    protocols: ["ISO 8583", "header-echo"],
    source: "packages/worker/adapters/tcp/adapter.py",
    overview: [
      "TcpAdapter is a universal, persistent TCP transport for request/response hosts. One connection is opened at connect() and reused for the whole run; many requests can be in flight at once, correlated back to their caller by a background reader.",
      "The transport is protocol-agnostic — it owns the socket, reader loop, correlation, framing, sign-on, keep-alive and timeouts. What goes on the wire is decided by a pluggable protocol, so the same class backs ISO 8583 switch/acquirer targets and header-echo host-command links (HSMs). Config picks the protocol.",
    ],
    protocolNotes: [
      'protocol: "iso8583" — ASCII ISO 8583. Correlation by a configurable DE (default DE 11 / STAN), optionally also matching the response MTI. Field map turns friendly payload keys (pan, amount, stan…) into DEs; explicit values{} sets DEs directly; a per-step fields table pins a dialect.',
      'protocol: "header_echo" — host-command links where the host echoes a fixed-width leading header used as the correlation key, followed by a response command code and a return/error code.',
      "Framing is fully configurable: a length prefix (width + byte order, whether it counts itself / the header), an optional inbound TPDU to strip and a static outbound TPDU, plus body encoding.",
    ],
    extraSections: [
      isoDialectSection("The ISO 8583 protocol is dialect-driven."),
    ],
    configGroups: [
      {
        title: "Connection",
        keys: [
          {
            key: "host",
            type: "string",
            required: true,
            desc: "Dial target (outbound) or bind address (inbound).",
          },
          {
            key: "port",
            type: "int",
            required: true,
            desc: "Dial port or bind port.",
          },
          {
            key: "protocol",
            type: "string",
            default: "iso8583",
            desc: '"iso8583" | "header_echo".',
          },
          {
            key: "response_timeout_sec",
            type: "float",
            default: "30",
            desc: "Per-request reply timeout.",
          },
          {
            key: "connect_timeout_sec",
            type: "float",
            default: "10",
            desc: "Socket connect timeout.",
          },
          {
            key: "pool_size",
            type: "int",
            desc: "Connection pool size for throughput.",
          },
          {
            key: "reconnect.enabled",
            type: "bool",
            default: "true",
            desc: "Auto-reconnect on transport drop.",
          },
        ],
      },
      {
        title: "Framing",
        keys: [
          {
            key: "framing.length_prefix_bytes",
            type: "int",
            default: "2",
            desc: "Width of the length prefix (>= 1; required for multiplexing).",
          },
          {
            key: "framing.length_byte_order",
            type: "string",
            default: "big",
            desc: '"big" | "little".',
          },
          {
            key: "framing.length_includes_prefix",
            type: "bool",
            default: "false",
            desc: "Does the length count its own prefix?",
          },
          {
            key: "framing.length_includes_header",
            type: "bool",
            default: "true",
            desc: "Does the length count the TPDU header?",
          },
          {
            key: "framing.tpdu_bytes",
            type: "int",
            default: "0",
            desc: "Inbound TPDU header width to strip.",
          },
          {
            key: "framing.tpdu_outbound_hex",
            type: "string",
            desc: "Static TPDU prepended on send (hex).",
          },
          {
            key: "framing.encoding",
            type: "string",
            default: "ascii",
            desc: "Body text encoding.",
          },
        ],
      },
      {
        title: "ISO 8583 (protocol = iso8583)",
        keys: [
          {
            key: "correlation.field",
            type: "string",
            default: "11",
            desc: "DE used to correlate request/response (STAN by default).",
          },
          {
            key: "correlation.auto_generate",
            type: "bool",
            default: "true",
            desc: "Auto-assign the correlation DE when absent.",
          },
          {
            key: "correlation.match_mti",
            type: "bool",
            default: "false",
            desc: "Also require the response MTI to match.",
          },
          {
            key: "correlation.response_mti_map",
            type: "object",
            desc: "Override request→response MTI mapping (e.g. 0200→0210).",
          },
          {
            key: "fields",
            type: "object",
            desc: "DE table / dialect. Defaults to the built-in field set.",
          },
          {
            key: "field_map",
            type: "object",
            desc: "Friendly key → DE number (merged over defaults: pan→2, amount→4, stan→11, response_code→39…).",
          },
          {
            key: "auto_fields",
            type: "bool",
            default: "true",
            desc: "Stamp time-based DEs (transmission date/time) automatically.",
          },
        ],
      },
      {
        title: "Header-echo (protocol = header_echo)",
        keys: [
          {
            key: "header_echo.header_bytes",
            type: "int",
            default: "4",
            desc: "Width of the echoed correlation header.",
          },
          {
            key: "header_echo.auto_header",
            type: "bool",
            default: "true",
            desc: "Auto-assign a sequential header.",
          },
          {
            key: "header_echo.response_command_bytes",
            type: "int",
            default: "2",
            desc: "Width of the reply command code.",
          },
          {
            key: "header_echo.error_field_bytes",
            type: "int",
            default: "2",
            desc: "Width of the reply return/error code.",
          },
          {
            key: "header_echo.ok_codes",
            type: "string[]",
            default: '["00"]',
            desc: "Error codes treated as healthy.",
          },
          {
            key: "header_echo.command_map",
            type: "object",
            desc: "action → command code.",
          },
          {
            key: "header_echo.diagnostic_command",
            type: "string",
            default: "NC",
            desc: "Health-check / sign-on command.",
          },
        ],
      },
      {
        title: "Lifecycle",
        keys: [
          {
            key: "sign_on",
            type: "object",
            desc: "{ enabled, action, payload } — handshake on connect. Defaults to the protocol's health probe (ISO ⇒ 0800; header_echo ⇒ diagnostic).",
          },
          {
            key: "keepalive",
            type: "object",
            desc: "{ enabled, interval_sec (30), action, payload } — periodic keepalive.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "send_0200",
        summary:
          "Financial request (purchase). Maps friendly keys to DEs and correlates on DE 11.",
        params: "pan, processing_code, amount, stan, values{…}, fields",
        returns:
          "mti, response_code (39), stan, rrn (37), auth_code (38), fields",
      },
      {
        name: "send_0100 / 0220 / 0400 / 0420",
        summary: "Other ISO message types (auth, advice, reversal).",
      },
      {
        name: "send_0800",
        summary: "Network-management / echo; the default ISO health probe.",
      },
      {
        name: "echo",
        summary: "Header-echo diagnostic exchange (header_echo protocol).",
        params: "command, data, header",
      },
    ],
    examples: [
      {
        title: "ISO 8583 switch link",
        lang: "json",
        code: `{
  "adapter": "tcp",
  "protocol": "iso8583",
  "host": "10.0.1.50",
  "port": 7000,
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big" },
  "correlation": { "field": "11", "match_mti": true },
  "sign_on": { "enabled": true },
  "keepalive": { "enabled": true, "interval_sec": 30 }
}`,
      },
      {
        title: "Step payload (purchase)",
        lang: "json",
        code: `{
  "action": "send_0200",
  "pan": "4111111111111111",
  "processing_code": "000000",
  "amount": "000000010000",
  "values": { "41": "TERM0001", "49": "840" }
}`,
      },
    ],
    errors: [
      "A transport failure (no reply within response_timeout_sec) returns success=false rather than raising — a group parent uses this to fail over.",
      "ISO encode raises if no correlation value is available and auto_generate is off.",
    ],
    seeAlso: ["switch", "acquirer", "hsm", "group"],
  },

  // -------------------------------------------------------------- switch ----
  {
    slug: "switch",
    targets: ["switch", "switch_iso8583"],
    label: "Switch",
    category: "Transport",
    description:
      "Payment switch over ISO 8583 / TCP — a preset of the universal TCP adapter with protocol = iso8583.",
    availability: "optional",
    direction: "outbound",
    protocols: ["ISO 8583"],
    source: "packages/worker/adapters/tcp/adapter.py",
    overview: [
      "Switch is a naming preset of the universal TCP adapter, fixed to the ISO 8583 protocol. Use it when the target is a payment switch so the connection reads clearly in config and topology views.",
      "All TCP-adapter framing, correlation, sign-on, keep-alive and routing options apply unchanged — see the TCP (universal) adapter for the full surface.",
    ],
    extraSections: [
      isoDialectSection(
        "As an ISO 8583 transport, the switch link is dialect-driven.",
      ),
    ],
    actions: [
      {
        name: "send_0200",
        summary: "Financial request (purchase).",
        returns: "mti, response_code, rrn, auth_code",
      },
      {
        name: "send_0800",
        summary: "Network-management / echo (health probe).",
      },
    ],
    notes: "Backed by TcpAdapter; only the target key differs.",
    seeAlso: ["tcp", "acquirer"],
  },

  // ------------------------------------------------------------ acquirer ----
  {
    slug: "acquirer",
    targets: ["acquirer", "acquirer_host"],
    label: "Acquirer",
    category: "Transport",
    description:
      "Acquirer host over ISO 8583 / TCP — a preset of the universal TCP adapter with protocol = iso8583.",
    availability: "optional",
    direction: "outbound",
    protocols: ["ISO 8583"],
    source: "packages/worker/adapters/tcp/adapter.py",
    overview: [
      "Acquirer is a naming preset of the universal TCP adapter, fixed to the ISO 8583 protocol, for acquirer-host transaction processing.",
      "All TCP-adapter options apply unchanged — see the TCP (universal) adapter for the full surface.",
    ],
    extraSections: [
      isoDialectSection(
        "As an ISO 8583 transport, the acquirer host link is dialect-driven.",
      ),
    ],
    actions: [
      { name: "send_0200", summary: "Financial request (purchase / sale)." },
      { name: "send_0420", summary: "Reversal advice." },
    ],
    notes: "Backed by TcpAdapter; only the target key differs.",
    seeAlso: ["tcp", "switch"],
  },

  // ----------------------------------------------------------------- hsm ----
  {
    slug: "hsm",
    targets: ["hsm", "hsm_tcp"],
    label: "HSM (header-echo)",
    category: "Host command",
    description:
      "Generic HSM / host-command link over TCP — commands correlated by an echoed header. Backed by the universal TCP adapter.",
    availability: "builtin",
    direction: "outbound",
    protocols: ["header-echo (over TCP)"],
    source: "packages/worker/adapters/tcp/protocols.py",
    overview: [
      "This is the universal TCP adapter running its header-echo protocol — a generic host-command link for HSMs (Thales payShield et al.) and similar request/response hosts. Each message carries a fixed-width header the host echoes verbatim; that header is the correlation key.",
      "It works at the wire level: you build the command and data yourself. For high-level payShield commands (generate_cvv, verify_pin, …) with parameter assembly and parsing, use the payShield client adapter instead.",
    ],
    protocolNotes: [
      "A reply is parsed as header + response command code + return/error code + data tail; widths are set by header_bytes / response_command_bytes / error_field_bytes.",
      "ok_codes lists the error codes considered healthy; the diagnostic_command (default NC) is used for sign-on and health checks.",
    ],
    configGroups: [
      {
        title: "Connection",
        keys: [
          {
            key: "host / port",
            type: "string / int",
            required: true,
            desc: "HSM host address.",
          },
          {
            key: "protocol",
            type: "string",
            default: "header_echo",
            desc: "Set to header_echo (or use the hsm target key).",
          },
        ],
      },
      {
        title: "Header-echo",
        keys: [
          {
            key: "header_echo.header_bytes",
            type: "int",
            default: "4",
            desc: "Echoed header width (correlation).",
          },
          {
            key: "header_echo.response_command_bytes",
            type: "int",
            default: "2",
            desc: "Reply command-code width.",
          },
          {
            key: "header_echo.error_field_bytes",
            type: "int",
            default: "2",
            desc: "Reply return/error-code width.",
          },
          {
            key: "header_echo.diagnostic_command",
            type: "string",
            default: "NC",
            desc: "Diagnostic / echo command.",
          },
          {
            key: "header_echo.command_map",
            type: "object",
            desc: "action → command code.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "echo / diagnostic",
        summary: "Send the diagnostic command and read the echoed reply.",
      },
      {
        name: "<mapped action>",
        summary: "Sends command_map[action] + data.",
        params: "command, data, header",
      },
    ],
    examples: [
      {
        title: "Generic header-echo HSM link",
        lang: "json",
        code: `{
  "adapter": "tcp",
  "protocol": "header_echo",
  "host": "127.0.0.1",
  "port": 1500,
  "header_echo": {
    "header_bytes": 4,
    "response_command_bytes": 2,
    "error_field_bytes": 2,
    "diagnostic_command": "NC"
  }
}`,
      },
    ],
    seeAlso: ["payshield", "tcp"],
  },

  // ------------------------------------------------------------ payshield ----
  {
    slug: "payshield",
    targets: ["hsm_client", "payshield"],
    label: "payShield client",
    category: "Host command",
    description:
      "High-level payShield 10K host-command client — names actions (generate_cvv, verify_pin, generate_mac…) and assembles/parses the wire for you.",
    availability: "builtin",
    direction: "outbound",
    protocols: ["payShield host commands (over TCP)"],
    source: "packages/worker/adapters/hsm/adapter.py",
    overview: [
      "HSMAdapter is a payShield 10K host-command client — the sending counterpart to the bundled payShield simulator. A step names a high-level action and supplies parameters; the adapter assembles the command string, frames it (length prefix + header + command + data), sends it and parses the reply into a structured payload. Flows can drive a real payShield or the simulator without hand-building wire strings.",
      "Each command is tagged with an incrementing header (a host-chosen correlation id the HSM echoes back). A background reader demultiplexes replies by header, so many commands can be in flight over one reused connection. Set connections (alias pool_size) > 1 to open several sockets that round-robin for throughput.",
    ],
    protocolNotes: [
      "response_payload carries response_code, error_code, ok (error == 00), the raw data tail, and any action-specific parsed field (e.g. cvv). Point assertions at error_code / ok / cvv etc.",
    ],
    configGroups: [
      {
        title: "Connection",
        keys: [
          {
            key: "host / port",
            type: "string / int",
            required: true,
            desc: "payShield host address (simulator default 127.0.0.1:1500).",
          },
          {
            key: "header_bytes",
            type: "int",
            default: "4",
            desc: "Width of the correlation header.",
          },
          {
            key: "connections",
            type: "int",
            default: "1",
            desc: "Sockets to open (round-robin); alias pool_size. >1 for load.",
          },
          {
            key: "length_prefix_bytes",
            type: "int",
            default: "2",
            desc: "Framing length-prefix width.",
          },
          {
            key: "length_byte_order",
            type: "string",
            default: "big",
            desc: '"big" | "little".',
          },
          {
            key: "encoding",
            type: "string",
            default: "ascii",
            desc: "Command/data encoding.",
          },
          {
            key: "response_timeout_sec",
            type: "float",
            default: "5",
            desc: "Per-command reply timeout.",
          },
        ],
      },
    ],
    actions: [
      {
        name: "nc / diagnostics",
        summary: "NC diagnostics command.",
        returns: "lmk check value",
      },
      { name: "hsm_status", summary: "NO status command." },
      {
        name: "generate_key",
        summary: "A0 — generate a key.",
        params: "mode, key_type, scheme",
      },
      {
        name: "key_check",
        summary: "BU — key check value.",
        params: "key_type_code, length_flag, key",
      },
      { name: "generate_cvv", summary: "Generate a CVV/CVC.", returns: "cvv" },
      { name: "verify_cvv", summary: "Verify a CVV/CVC." },
      {
        name: "translate_pin / translate_pin_tpk_zpk / translate_pin_zpk_zpk",
        summary: "Translate a PIN block between keys.",
      },
      {
        name: "verify_pin / verify_pin_pvv",
        summary: "Verify a PIN (PVV method).",
      },
      { name: "dukpt_translate_pin", summary: "Translate a DUKPT PIN block." },
      {
        name: "generate_mac / verify_mac",
        summary: "Generate / verify a message MAC.",
      },
      {
        name: "import_key / export_key",
        summary: "Import / export a key under a key-encrypting key.",
      },
      { name: "verify_arqc", summary: "Verify an EMV ARQC cryptogram." },
    ],
    examples: [
      {
        title: "Environment adapter entry",
        lang: "json",
        code: `{
  "adapter": "payshield",
  "host": "127.0.0.1",
  "port": 1500,
  "connections": 1,
  "response_timeout_sec": 5
}`,
      },
      {
        title: "Step payload (generate CVV)",
        lang: "json",
        code: `{ "action": "generate_cvv", "pan": "4111111111111111", "expiry": "2512", "service_code": "201" }`,
      },
    ],
    errors: [
      "A non-00 error_code is returned with ok=false rather than raising — assert on error_code / ok.",
      "An unknown action falls back to its first two characters as the command code.",
    ],
    seeAlso: ["hsm", "tcp"],
  },

  // ------------------------------------------------------------- db_probe ---
  {
    slug: "db_probe",
    targets: ["db_probe_core", "db_probe_switch"],
    label: "DB Probe",
    category: "Verification",
    description:
      "Read-only database adapter for cross-system assertions (verify a transaction landed downstream). Planned — not yet implemented.",
    availability: "planned",
    direction: "outbound",
    protocols: ["SQL"],
    source: "packages/worker/adapters/db_probe/",
    overview: [
      "A read-only database adapter for cross-system assertions — verify that a transaction processed by the payment server appears correctly in a downstream database. By design it never writes: every query must be a SELECT.",
      "Registered behind a try-import, but the implementation module is not present yet, so the adapter is unavailable on current workers.",
    ],
    configGroups: [
      {
        title: "Config (planned)",
        keys: [
          {
            key: "engine",
            type: "string",
            desc: "Database engine, e.g. postgresql.",
          },
          {
            key: "host / port / dbname / user",
            type: "string / int",
            desc: "Connection coordinates.",
          },
          {
            key: "dsn",
            type: "string",
            desc: "Full connection string (secret).",
          },
        ],
      },
    ],
    actions: [
      {
        name: "query",
        summary:
          "Run a SELECT and assert on the rows (planned). Non-SELECT is rejected.",
      },
    ],
    notes:
      "Planned adapter — directory currently holds only a README and package stub.",
  },

  // ---------------------------------------------------------------- group ---
  {
    slug: "group",
    targets: ["group"],
    label: "Participant Group",
    category: "Routing",
    description:
      "One logical participant backed by several member connections (a fleet of issuers/hosts), selecting a member per dispatch by policy.",
    availability: "builtin",
    direction: "outbound (routing)",
    source: "packages/worker/adapters/group.py",
    overview: [
      "GroupAdapter makes N connection instances act as one logical participant — a fleet of issuers, several acquirer hosts. A group selects among members, each a full adapter config and possibly a different adapter class.",
      "The orchestrator inlines each member's resolved config into the group's members list at run-build time, so the worker needs no registry lookups. Members connect lazily on first dispatch.",
    ],
    protocolNotes: [
      "Selection policies: round_robin, weighted, random, failover, sticky (with a request-field key). Under failover a transport error or no-response result drops the member and advances to the next; a stopped→restarted member is reconnected automatically.",
    ],
    configGroups: [
      {
        title: "Config",
        keys: [
          {
            key: "members[]",
            type: "object[]",
            required: true,
            desc: "{ connection | name, config, weight } per member.",
          },
          {
            key: "selection.policy",
            type: "string",
            default: "round_robin",
            desc: "round_robin | weighted | random | failover | sticky.",
          },
          {
            key: "selection.key",
            type: "string",
            desc: "Request field for sticky routing (e.g. de.2).",
          },
        ],
      },
    ],
    actions: [
      {
        name: "(delegates)",
        summary:
          "Forwards the action/payload to the selected member's adapter.",
      },
    ],
    examples: [
      {
        title: "Weighted issuer fleet",
        lang: "json",
        code: `{
  "adapter": "group",
  "selection": { "policy": "weighted" },
  "members": [
    { "name": "issuer_a", "weight": 3, "config": { "adapter": "tcp", "host": "10.0.0.1", "port": 7000 } },
    { "name": "issuer_b", "weight": 1, "config": { "adapter": "tcp", "host": "10.0.0.2", "port": 7000 } }
  ]
}`,
      },
    ],
    seeAlso: ["tcp"],
  },
];

const BY_SLUG: Record<string, AdapterSpec> = Object.fromEntries(
  ADAPTERS.map((a) => [a.slug, a]),
);

export function getAdapter(slug: string): AdapterSpec | undefined {
  return BY_SLUG[slug];
}

export function availabilityLabel(a: Availability): string {
  return a === "builtin"
    ? "Built-in"
    : a === "optional"
      ? "Optional"
      : "Planned";
}
