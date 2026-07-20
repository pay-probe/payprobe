/**
 * Types mirroring the scenario-service API (packages/scenario-service).
 * Field names are snake_case to match the wire format and the scenario
 * JSON documents in examples/scenarios/.
 */

export type Operator =
  | "eq"
  | "ne"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "present"
  | "absent"
  | "contains"
  | "matches";

export const OPERATORS: Operator[] = [
  "eq",
  "ne",
  "lt",
  "lte",
  "gt",
  "gte",
  "present",
  "absent",
  "contains",
  "matches",
];

/** Operators that take no expected value. */
export const UNARY_OPERATORS: ReadonlySet<string> = new Set([
  "present",
  "absent",
]);

export interface Assertion {
  field: string;
  operator: Operator;
  expected?: unknown;
}

/**
 * Node kinds the canvas can place: an adapter action, a custom-code node, or a
 * control-flow node.
 */
export type NodeKind =
  | "action"
  | "if"
  | "switch"
  | "loop"
  | "wait"
  | "merge"
  | "code"
  | "http"
  | "init"
  | "crypto"
  | "call"
  // Participant-flow kinds (only offered in the flow editor's palette).
  | "trigger"
  | "reply"
  | "state"
  | "relay";

export const CONTROL_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  "if",
  "switch",
  "loop",
  "wait",
  "merge",
  "code",
  "http",
  "init",
  "crypto",
  "call",
]);

export type CryptoOperation =
  | "des"
  | "kcv"
  | "pin_block_encode"
  | "pin_block_decode"
  | "retail_mac"
  | "cvv"
  | "arqc"
  | "arpc"
  | "emv_icc_mk"
  | "emv_session_key";

export const CRYPTO_OPERATIONS: { value: CryptoOperation; label: string }[] = [
  { value: "des", label: "DES / 3DES encrypt-decrypt" },
  { value: "kcv", label: "Key Check Value (KCV)" },
  { value: "pin_block_encode", label: "PIN block — encode (ISO-0)" },
  { value: "pin_block_decode", label: "PIN block — decode (ISO-0)" },
  { value: "retail_mac", label: "Retail MAC (ISO 9797-1 alg 3)" },
  { value: "cvv", label: "CVV / CVC (Visa)" },
  { value: "emv_icc_mk", label: "EMV ICC Master Key (Option A)" },
  { value: "emv_session_key", label: "EMV Session Key (Common)" },
  { value: "arqc", label: "ARQC — generate / verify" },
  { value: "arpc", label: "ARPC — generate / verify" },
];

/** Languages a `code` node can be authored in. */
export type CodeLanguage = "python" | "typescript" | "javascript";

export const CODE_LANGUAGES: CodeLanguage[] = [
  "python",
  "typescript",
  "javascript",
];

/** HTTP methods an `http` node can use. */
export type HttpMethod =
  | "GET"
  | "POST"
  | "PUT"
  | "PATCH"
  | "DELETE"
  | "HEAD"
  | "OPTIONS";

export const HTTP_METHODS: HttpMethod[] = [
  "GET",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
  "HEAD",
  "OPTIONS",
];

export type HttpAuthType = "none" | "bearer" | "basic" | "header" | "query";

/** A name/value row used for query params, headers, form bodies and code inputs. */
export interface KeyValue {
  name: string;
  value: string;
  /**
   * When present, the editor renders this row's value as a dropdown of these
   * choices instead of a free-text field (e.g. an encode/decode mode). Carried
   * through from a code-backed step's template; ignored by the runner.
   */
  options?: string[];
  /**
   * When present, the editor renders a Message Format picker for this row's
   * value (the protocol, e.g. "iso8583"). Selecting a format snapshots its
   * definition into `value`; `formatId`/`formatVersion` record the source.
   */
  format?: string;
  formatId?: string;
  formatVersion?: string;
}

export interface HttpAuth {
  type: HttpAuthType;
  token?: string;
  username?: string;
  password?: string;
  headerName?: string;
  headerValue?: string;
  paramName?: string;
  paramValue?: string;
}

export interface HttpOptions {
  timeout_ms?: number;
  followRedirects?: boolean;
  ignoreSsl?: boolean;
  responseFormat?: "auto" | "json" | "text";
}

/** A single condition used by `if` nodes and `while` loops. */
export interface Condition {
  left: string;
  operator: Operator;
  right?: unknown;
}

/** A `switch` routing case (one output port per case). */
export interface SwitchCase {
  value: string;
}

/**
 * Control-flow parameters carried on a control node's `config`. Only the keys
 * relevant to the node's `kind` are populated; the backend stores it as an
 * open object so the shape can evolve without a migration.
 */
export interface NodeConfig {
  // if / while
  left?: string;
  operator?: Operator;
  right?: unknown;
  // switch
  value?: string;
  cases?: SwitchCase[];
  // loop
  mode?: "times" | "forEach" | "while";
  count?: number;
  list?: string;
  condition?: Condition;
  max_iterations?: number;
  // wait
  ms?: number;
  // code
  language?: CodeLanguage;
  code?: string;
  timeout_ms?: number;
  /** Named inputs (literal or ${ref}) resolved and exposed to the snippet as `inputs`. */
  inputs?: KeyValue[];
  // init
  data?: string;
  // crypto (`data` reused from init for the DES payload)
  operation?: CryptoOperation;
  key?: string;
  pin?: string;
  pan?: string;
  pin_block?: string;
  expiry?: string;
  service_code?: string;
  iv?: string;
  cipher_mode?: "ecb" | "cbc";
  cipher_op?: "encrypt" | "decrypt";
  psn?: string;
  atc?: string;
  // ARQC / ARPC
  arqc?: string;
  arc?: string;
  csu?: string;
  proprietary?: string;
  arpc_method?: "1" | "2";
  expected?: string;
  // call (sub-scenario)
  scenario_id?: string;
  /** Variable overrides passed into the called sub-scenario. */
  variables?: KeyValue[];
  // custom catalog step provenance (for label/icon on http/code nodes)
  stepType?: string;
  stepLabel?: string;
  // http
  method?: HttpMethod;
  url?: string;
  authentication?: HttpAuth;
  sendQueryParameters?: boolean;
  queryParameters?: KeyValue[];
  sendHeaders?: boolean;
  headers?: KeyValue[];
  sendBody?: boolean;
  contentType?: "json" | "form" | "raw";
  body?: string;
  bodyParameters?: KeyValue[];
  options?: HttpOptions;
  /** Saved connection (by name) this adapter step runs against; the orchestrator
   * re-points the step's target at it and injects its config at run time. */
  connection?: string;
  // relay / proxy node: forwarding posture
  relay_mode?: "decode" | "raw";
  /** Canvas position persisted with the node (written by the editor on save). */
  _pos?: NodePosition;
}

export interface Step {
  id: string;
  kind?: NodeKind;
  target: string;
  action: string;
  /** Usually an object; a flow reply node may be a whole-string reference
   * (e.g. `"${fwd.response}"`) that resolves to the full response object. */
  payload: Record<string, unknown> | string;
  assertions: Assertion[];
  config?: NodeConfig;
  /** Optional per-step environment override (by name). When set, the orchestrator
   * runs this step against that environment's adapter config instead of the
   * run's default environment. */
  environment_override?: string;
}

/** A directed wire between two nodes, leaving the source through `source_port`. */
export interface Edge {
  source: string;
  source_port: string;
  target: string;
}

/** The output port ids a node exposes given its kind and config. */
export function nodeOutputPorts(step: Step): string[] {
  switch (step.kind) {
    case "reply":
      return []; // terminal: a reply ends the flow — no outgoing wire
    case "if":
      return ["true", "false"];
    case "loop":
      return ["loop", "done"];
    case "switch": {
      const cases = step.config?.cases ?? [];
      return [...cases.map((_, i) => `case_${i}`), "default"];
    }
    default:
      return ["out"];
  }
}

/** Human label shown on a control node's output port. */
export function portLabel(step: Step, port: string): string {
  if (step.kind === "switch" && port.startsWith("case_")) {
    const idx = Number(port.slice(5));
    return step.config?.cases?.[idx]?.value || `case ${idx}`;
  }
  return port;
}

export const CONTROL_NODE_META: Record<
  Exclude<NodeKind, "action">,
  { label: string; glyph: string; color: string; hint: string }
> = {
  if: {
    label: "If",
    glyph: "⑂",
    color: "#d29922",
    hint: "Branch on a condition (true / false)",
  },
  switch: {
    label: "Switch",
    glyph: "⌥",
    color: "#f0883e",
    hint: "Route on a value to one of many cases",
  },
  loop: {
    label: "Loop",
    glyph: "↻",
    color: "#a371f7",
    hint: "Repeat a sub-flow: times / for-each / while",
  },
  wait: {
    label: "Wait",
    glyph: "⏱",
    color: "#58a6ff",
    hint: "Pause for a duration before continuing",
  },
  merge: {
    label: "Merge",
    glyph: "⛙",
    color: "#3fb950",
    hint: "Recombine branches back into one path",
  },
  code: {
    label: "Code",
    glyph: "{ }",
    color: "#7ee787",
    hint: "Run custom Python/TypeScript and return an object",
  },
  http: {
    label: "HTTP Request",
    glyph: "🌐",
    color: "#58a6ff",
    hint: "Call an HTTP endpoint; response available downstream",
  },
  init: {
    label: "Init",
    glyph: "⚑",
    color: "#d2a8ff",
    hint: "Provide an initial JSON object the flow can read via ${id.response.*}",
  },
  crypto: {
    label: "Crypto",
    glyph: "🔐",
    color: "#ff7b72",
    hint: "Payment crypto: PIN block, MAC, CVV, ARQC, DES/3DES, KCV",
  },
  call: {
    label: "Call Scenario",
    glyph: "⮒",
    color: "#79c0ff",
    hint: "Run another scenario as a sub-flow; its output is available via ${id.response.*}",
  },
  // Participant-flow kinds (flow editor only).
  trigger: {
    label: "Trigger",
    glyph: "⚡",
    color: "#3fb950",
    hint: "Entry — fires on each incoming message; expose it as ${request.*}",
  },
  reply: {
    label: "Reply",
    glyph: "↩",
    color: "#79c0ff",
    hint: "Send the response back on the inbound connection (set/echo/generate fields)",
  },
  state: {
    label: "State",
    glyph: "◉",
    color: "#d2a8ff",
    hint: "Mutate the flow's shared state (set/incr/append), read via ${state.*}",
  },
  relay: {
    label: "Relay / Proxy",
    glyph: "⇄",
    color: "#56d4dd",
    hint: "Forward inbound traffic to an upstream connection. Unwired = transparent proxy; wired to a reply = in-graph forward, upstream answer via ${id.response}",
  },
};

/** Node kinds that only make sense in a participant flow (hidden from the
 * scenario palette; shown in the flow editor). */
export const FLOW_NODE_KINDS: NodeKind[] = [
  "trigger",
  "reply",
  "state",
  "relay",
];

/** Canvas position of a step's node in the visual constructor. */
export interface NodePosition {
  x: number;
  y: number;
}

/** Pipeline-aligned classification (component / integration / e2e). */
export type TestClass = "component" | "integration" | "e2e";

export const TEST_CLASSES: TestClass[] = ["component", "integration", "e2e"];

export interface ScenarioDraft {
  name: string;
  description: string;
  /** Presentation metadata: category label + accent color + logo (pp-icon). */
  category?: string;
  color?: string;
  icon?: string;
  tags: string[];
  stop_on_failure: boolean;
  timeout_ms: number;
  test_class: TestClass;
  steps: Step[];
  /** Scenario-scoped variables, referenced anywhere as ${vars.NAME}. */
  variables?: Record<string, unknown>;
  /** Names (subset of variables) whose values are secret — masked + redacted. */
  secret_vars?: string[];
  /** Optional saved test-data pools (Test Data manager). Resolved to inline
   *  cards/terminals by the orchestrator; feed ${pool.card} / ${pool.terminal}. */
  card_pool?: string;
  terminal_pool?: string;
  /** Explicit control-flow wires. Empty ⇒ worker runs steps in list order. */
  edges?: Edge[];
  /** Editor-only node positions keyed by step id. Ignored by the worker. */
  layout?: Record<string, NodePosition>;
}

export interface Scenario extends ScenarioDraft {
  id: string;
  project_id: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ScenarioSummary {
  id: string;
  project_id: string;
  name: string;
  description: string;
  category?: string;
  color?: string;
  icon?: string;
  tags: string[];
  test_class: TestClass;
  step_count: number;
  version: number;
  updated_at: string;
}

// -- organisation: projects and named sets --

export interface ProjectDraft {
  name: string;
  description: string;
  /** Presentation metadata: category label + accent color + logo (pp-icon). */
  category?: string;
  color?: string;
  icon?: string;
}

/**
 * Preset categories for projects and scenarios. Selecting a category stamps a
 * matching logo (a `pp-icon` name) and accent color, so the constructor list
 * can render a recognisable badge per protocol/system (HSM, NATS, ISO 8583…).
 */
export interface CategoryPreset {
  label: string;
  icon: string;
  color: string;
}

export const CATEGORY_PRESETS: CategoryPreset[] = [
  { label: "ISO 8583", icon: "network", color: "#d29922" },
  { label: "HSM", icon: "lock", color: "#d2a8ff" },
  { label: "NATS / Messaging", icon: "message", color: "#27aae1" },
  { label: "HTTP / REST", icon: "globe", color: "#00d4aa" },
  { label: "Crypto / EMV", icon: "key", color: "#f778ba" },
  { label: "Database", icon: "database", color: "#79c0ff" },
  { label: "Intelligence", icon: "sparkles", color: "#f0883e" },
  { label: "Data Tables", icon: "table", color: "#3fb950" },
  { label: "General", icon: "layers", color: "#8b949e" },
];

/** Look up a preset by its category label (case-insensitive). */
export function categoryPreset(label?: string): CategoryPreset | undefined {
  if (!label) return undefined;
  const key = label.trim().toLowerCase();
  return CATEGORY_PRESETS.find((c) => c.label.toLowerCase() === key);
}

export interface Project extends ProjectDraft {
  id: string;
  scenario_count: number;
  set_count: number;
  created_at: string;
  updated_at: string;
}

export interface ScenarioSetDraft {
  name: string;
  description: string;
  scenario_ids: string[];
}

export interface ScenarioSet extends ScenarioSetDraft {
  id: string;
  project_id: string;
  created_at: string;
  updated_at: string;
}

/** Mixed, typed results for the global search box. */
export interface SearchResults {
  query: string;
  projects: Project[];
  sets: ScenarioSet[];
  scenarios: ScenarioSummary[];
}

export const DEFAULT_PROJECT_ID = "prj-default";

export interface ScenarioVersionInfo {
  version: number;
  created_at: string;
  comment: string;
  step_count: number;
}

export interface ValidationIssue {
  severity: "error" | "warning";
  step_id?: string | null;
  message: string;
}

export interface ValidationResult {
  valid: boolean;
  issues: ValidationIssue[];
}

/** How a catalog action runs: a backend adapter, or a self-contained node. */
export type StepBehaviorKind = "adapter" | "http" | "code" | "crypto";

export interface StepBehavior {
  kind: StepBehaviorKind;
  /** For http/code: the node config the dropped step is materialised from. */
  template?: NodeConfig;
}

/** A typed, optionally-selectable configuration parameter for an action. */
export interface ParamSpec {
  name: string;
  label?: string;
  type?: "string" | "number" | "boolean" | "enum" | "json" | "format";
  options?: string[];
  required?: boolean;
  default?: unknown;
  placeholder?: string;
  /** For type "format": only offer Message Formats of this protocol. */
  protocol?: string;
  /** For type "enum": source options from the bound dialect (definition.mti). */
  options_from_format?: boolean;
}

export interface ActionSpec {
  name: string;
  label: string;
  payload_hint: Record<string, string>;
  response_fields: string[];
  params?: ParamSpec[];
  behavior?: StepBehavior | null;
}

export interface TargetSpec {
  target: string;
  label: string;
  category: string;
  color: string;
  /** One-line human description shown under the palette group. */
  description?: string;
  /** Category logo — a `pp-icon` name (e.g. "lock", "globe", "network"). */
  icon?: string;
  actions: ActionSpec[];
  /** True for user-defined or user-overridden catalog entries. */
  custom?: boolean;
}

/** A catalog target as returned by GET /catalog/manage, with provenance flags. */
export interface ManagedTarget extends TargetSpec {
  builtin: boolean;
  overridden: boolean;
  hidden: boolean;
}

// -- message format registry (versioned ISO8583 / ISO20022 specs) --

export type Protocol = "iso8583" | "iso20022";

export interface Iso8583Field {
  name: string;
  len_type: "fixed" | "llvar" | "lllvar" | "llllvar" | "lllllvar";
  length: number;
  type: string;
}

export interface MessageFormatDraft {
  protocol: Protocol;
  name: string;
  version: string;
  /** Optional organisational group within a protocol (e.g. an acquirer name). */
  group?: string;
  description: string;
  definition: Record<string, unknown>;
}

export interface MessageFormat extends MessageFormatDraft {
  id: string;
  builtin: boolean;
}

// -- global named tables --

export interface DataTableDraft {
  description: string;
  columns: string[];
  rows: Record<string, unknown>[];
}

export interface DataTable extends DataTableDraft {
  name: string;
}

/** A curated test-case pack (certification suite). */
export interface PackSummary {
  id: string;
  scheme: string;
  label: string;
  description: string;
  version: string;
  cases: number;
}

export interface PackCase {
  id: string;
  requirement: string;
  scenario: Record<string, unknown>;
}

export interface Pack {
  id: string;
  scheme: string;
  label: string;
  description: string;
  version: string;
  cases: PackCase[];
}

export interface InstallPackResult {
  pack: string;
  project_id: string;
  project_name: string;
  imported: number;
  scenario_names: string[];
}

/** A BER-TLV node from an EMV data element (e.g. DE 55). */
export interface IsoTlvNode {
  tag: string;
  name?: string | null;
  length: number;
  value?: string;
  children?: IsoTlvNode[];
}

/** One decoded data element row from the ISO 8583 analyzer. */
export interface IsoDeRow {
  de: number;
  name?: string;
  type?: string;
  len_type?: string;
  length?: number | null;
  value?: string;
  length_actual?: number;
  error?: string;
  /** human-readable meaning of the value (response code, currency, amount…) */
  interpretation?: string;
  tlv?: IsoTlvNode[];
  tlv_error?: string;
}

export interface IsoMtiInfo {
  mti: string;
  version: string;
  message_class: string;
  function: string;
  origin: string;
}

export interface IsoAnalysis {
  mti: string;
  mti_info?: IsoMtiInfo | null;
  bitmap: {
    primary: string;
    secondary: string | null;
    present: number[];
  } | null;
  fields: IsoDeRow[];
  errors: string[];
  trailing?: string | null;
}

export interface IsoBuildResult {
  message: string | null;
  errors: string[];
}

export interface IsoDiffRow {
  de: number;
  name: string;
  a: string | null;
  b: string | null;
  change: "added" | "removed" | "changed" | "same";
}

export interface IsoDiff {
  mti_a: string;
  mti_b: string;
  mti_changed: boolean;
  fields: IsoDiffRow[];
  summary: { added: number; removed: number; changed: number; same: number };
}

/** A node in a starter flow: a catalog step + config / input overrides. */
export interface FlowStep {
  ref: string;
  target: string;
  action: string;
  config?: Record<string, unknown>;
  inputs?: Record<string, string>;
  /** Optional assertions (used by AI-assisted flows). */
  assertions?: { field: string; operator: string; expected?: unknown }[];
}

export interface AssistResult {
  flow: StarterFlow;
  provider: string;
  mode: "create" | "extend";
  notes: string[];
}

/** Masked view of the AI-assistant LLM settings (no raw key). */
export interface AssistConfig {
  provider: "openai" | "anthropic";
  enabled: boolean;
  base_url: string;
  model: string;
  key_set: boolean;
  key_hint: string;
}

export interface AssistConfigDraft {
  provider: "openai" | "anthropic";
  enabled: boolean;
  base_url: string;
  model: string;
  /** omit / null to keep the stored key; "" to clear it */
  api_key?: string | null;
}

export interface StarterFlowDraft {
  label: string;
  description: string;
  steps: FlowStep[];
}

export interface StarterFlow extends StarterFlowDraft {
  id: string;
  builtin: boolean;
  updated_at?: string | null;
}

/** Matches `${step_001.response.rrn}` style references. */
export const VAR_REF_RE = /\$\{([A-Za-z0-9_-]+)\.([A-Za-z0-9_.[\]-]+)\}/g;

export function emptyDraft(): ScenarioDraft {
  return {
    name: "untitled_scenario",
    description: "",
    tags: [],
    stop_on_failure: true,
    timeout_ms: 10_000,
    test_class: "e2e",
    steps: [],
    variables: {},
    edges: [],
    layout: {},
  };
}
