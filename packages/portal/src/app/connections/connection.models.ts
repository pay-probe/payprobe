/** Configuration model for a named adapter connection (a "connection").
 *
 * Mirrors the worker AdapterRegistry config shape. The builder edits these in
 * a friendly form; `toAdapterConfig` emits the worker-shaped JSON that goes in
 * an environment file's `adapters` map.
 */

export type AdapterImpl =
  | "tcp"
  | "grpc"
  | "http"
  | "nats"
  | "payshield"
  | "db_probe_core"
  | "db_probe_switch";
export type TcpProtocol = "iso8583" | "header_echo";
export type ByteOrder = "big" | "little";

/** A gRPC call metadata header (key/value), edited as rows in the form. */
export interface GrpcMetaEntry {
  key: string;
  value: string;
}

/** A friendly action name mapped to a `package.Service/Method`. */
export interface GrpcActionEntry {
  name: string;
  method: string;
}

/** How the gRPC adapter learns the service schema (methods + message types). */
export type GrpcSchemaSource = "descriptor" | "proto" | "reflection";

/** One custom-declared .proto file. The `name` is the filename other files
 * `import` by, so a set of inter-related protos resolves once they're all here. */
export interface GrpcProtoFile {
  name: string;
  text: string;
}

/** gRPC-specific connection settings (used when adapter = "grpc"). */
export interface GrpcConfig {
  /** "host:port" — takes precedence over the shared host/port fields. */
  target: string;
  /** Which schema source the form is editing (descriptor set / .proto / reflection). */
  schemaSource: GrpcSchemaSource;
  /** Path to a compiled FileDescriptorSet (.pb) on the worker host. */
  descriptorSetPath: string;
  /** …or the descriptor set inline as base64 (wins over the path). */
  descriptorSetB64: string;
  /** Custom-declared .proto files (pasted / dropped), compiled on the fly. Each
   * keeps its filename so cross-file `import`s resolve. */
  protoFiles: GrpcProtoFile[];
  /** .proto files on the worker host (one per line, or comma-separated). */
  protoPaths: string;
  /** Extra `-I` import roots for proto compilation (one per line). */
  protoIncludeDirs: string;
  tlsEnabled: boolean;
  tlsRootCertsPath: string;
  timeoutSec: number;
  metadata: GrpcMetaEntry[];
  actions: GrpcActionEntry[];
}

/** Split a multi-line / comma-separated textarea into a trimmed list. */
function splitList(s: string): string[] {
  return s
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

/** Rebuild the editable proto-file list from a stored `proto_inline` value,
 * which may be a single string (legacy) or a { filename: text } map. */
function protoFilesFromConfig(raw: unknown): GrpcProtoFile[] {
  if (typeof raw === "string") {
    return raw.trim() ? [{ name: "service.proto", text: raw }] : [];
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, unknown>).map(
      ([name, text]) => ({
        name,
        text: String(text),
      }),
    );
  }
  return [];
}

/** How the length prefix carries the length: a binary integer (the ISO 8583
 *  norm) or zero-padded ASCII digits, one per prefix byte (e.g. `0043`). */
/** Length-prefix encodings the worker frames with (mirror of
 *  `payprobe_common.link_framing.LENGTH_ENCODINGS`). */
export type LengthEncoding = "binary" | "ascii" | "bcd" | "hex";
/** How frames are delimited: a length prefix (default) or a start/end byte
 *  envelope (STX … ETX LRC) with no length at all. */
export type FramingMode = "length_prefix" | "delimited";
/** Generic, structure-named framing presets (mirror of
 *  `payprobe_common.link_framing.FRAMING_PRESETS`; a preset is a bundle of
 *  defaults the backend expands into explicit keys on save). */
export const FRAMING_PRESET_NAMES = [
  "binary2",
  "binary2_inclusive",
  "binary2_pad2",
  "binary4_pad14_inclusive",
  "bcd2",
  "ascii2",
  "ascii4",
  "ascii5",
  "ascii6",
  "ascii4_trailer",
  "hex4",
  "stx_etx_lrc",
] as const;
export type FramingPreset = (typeof FRAMING_PRESET_NAMES)[number];

export interface FramingConfig {
  /** Generic preset name ("" = none). The backend expands it into the explicit
   *  keys below on save and keeps the label; explicit keys set beside it win. */
  preset?: FramingPreset | "";
  /** Compact link-level string copied off a terminal tool / switch screen,
   *  `<enc>:<counts-prefix>:<counts-header>:<order>:<width>:<header-hex>`,
   *  e.g. `CHARDEC:false:false:BIG:4:`. Parsed into explicit keys on save. */
  linkLevel?: string;
  mode?: FramingMode;
  lengthPrefixBytes: number;
  lengthEncoding: LengthEncoding;
  byteOrder: ByteOrder;
  lengthIncludesPrefix: boolean;
  lengthIncludesHeader: boolean;
  tpduBytes: number;
  tpduOutboundHex: string;
  /** Fixed message header after the length prefix, as hex ("" = none). */
  headerHex?: string;
  /** Fixed trailer after the body (e.g. "03" = mandatory ETX), never counted. */
  trailerHex?: string;
  /** Delimited mode only: start / end bytes (hex) and the checksum after the end. */
  startHex?: string;
  endHex?: string;
  checksum?: "none" | "lrc";
  encoding: string;
}

export interface CorrelationConfig {
  field: string;
  autoGenerate: boolean;
}

export interface HeaderEchoConfig {
  headerBytes: number;
  responseCommandBytes: number;
  errorFieldBytes: number;
  diagnosticCommand: string;
}

export interface KeepaliveConfig {
  enabled: boolean;
  intervalSec: number;
}

/** REST/HTTP adapter settings (used when adapter = "http"). The endpoint lives
 * in `baseUrl` (not the shared host/port). */
export interface RestConfig {
  baseUrl: string;
  apiKey: string;
  authType: "" | "bearer" | "basic";
  username: string;
  password: string;
  timeoutMs: number;
}

/** payShield host-command client settings (used when adapter = "payshield").
 * Uses the shared host/port plus a host-command header. */
export interface PayShieldConfig {
  header: string;
  responseTimeoutSec: number;
}

/** Database-probe settings (used when adapter = "db_probe_core" /
 * "db_probe_switch"; ADR-0012). Uses the shared host/port plus engine +
 * database. Read-only by the database session; writes are a per-environment
 * opt-in with declared cleanup. Named queries carry the SQL (schema knowledge
 * is connection data, overridable per environment). */
export interface DbConfig {
  engine: string;
  dbname: string;
  user: string;
  password: string;
  /** Full connection string (secret); alternative to host/port/dbname/user. */
  dsn: string;
  /** "" (read-only) | "true" (writes with declared cleanup) | "permanent". */
  writes: string;
  /** Row cap per statement (default 100) and statement timeout (default 5000 ms). */
  maxRows: number;
  statementTimeoutMs: number;
  /** Let a load run query this probe at the target TPS (refused otherwise). */
  loadOk: boolean;
  /** Named queries as JSON: {"query_transaction": {"sql": "...", "params": ["rrn"]}}. */
  queriesJson: string;
  /** SQLite only: statements run once at connect (one per line) to seed an example. */
  initSql: string;
}

/** NATS adapter settings (ADR-0006; used when adapter = "nats"). The shared
 * host/port address the broker (built as nats://host:port); NATS is broker-
 * mediated so this is not a bind target. auth.* and jetstream.* ride the extra-
 * allow connection model. */
export interface NatsConfig {
  /** Explicit multi-URL cluster (one per line); wins over the shared host/port. */
  servers: string;
  /** Default subject for publish/request. */
  subject: string;
  /** Inbound: subjects a responder subscribes to (comma/newline separated). */
  subjects: string;
  /** Inbound: load-balanced queue group for fan-out across instances. */
  queueGroup: string;
  codec: "json" | "bytes" | "iso8583";
  requestTimeoutSec: number;
  /** Auth (secrets — masked on read, encrypted at rest). */
  authToken: string;
  authNkeySeed: string;
  authCreds: string;
  /** JetStream: declare a stream/consumer (created-if-missing, ownership-tracked). */
  jetstreamStream: string;
  jetstreamSubjects: string;
  jetstreamDurable: string;
}

/** What *Test connection* / diagnostics put on the wire (mirror of
 *  `worker.adapters.tcp.HEALTH_MODES`). */
export type HealthCheckMode = "probe" | "connect" | "message";
export const HEALTH_CHECK_MODES: readonly HealthCheckMode[] = [
  "probe",
  "connect",
  "message",
];

/** The `health_check` section of a TCP connection. `payloadJson` is the
 *  message to send in `message` mode, edited as JSON text. */
export interface HealthCheckConfig {
  mode: HealthCheckMode;
  action: string;
  payloadJson: string;
  /** `connect` mode: ms the socket must stay open (worker default 200). */
  settleMs: number | null;
}

export interface Connection {
  name: string;
  adapter: AdapterImpl;
  /** Free text shown in lists and returned by the API; not adapter config. */
  description: string;
  protocol: TcpProtocol;
  host: string;
  port: number;
  /** Per-environment parameter overrides, keyed by environment name/slug. Each
   * value is a partial worker-shaped config shallow-merged over the base when
   * the connection runs under that environment. Editable from both the
   * Connections and Environments pages; not part of the worker adapter config
   * emitted by {@link toAdapterConfig}. */
  environmentOverrides: Record<string, Record<string, unknown>>;
  /** Direction. "outbound" (default) = client: the worker connects to host:port.
   * "inbound" = server: a participant flow binds and listens on the same `port`
   * (0 = pick a free port at bind time). One port serves both directions. */
  mode: "outbound" | "inbound";
  /** Administratively disabled — skipped as a group member (out of rotation)
   * without being deleted, so it can be re-enabled later. */
  disabled?: boolean;
  /** The default instance for this adapter type: a step that names no connection
   * resolves to its type's default. At most one default per type (the backend
   * moves the flag when another is set). Metadata, not emitted in adapter config. */
  default?: boolean;
  extends?: string;
  poolSize?: number;
  responseTimeoutSec: number;
  connectTimeoutSec: number;
  framing: FramingConfig;
  correlation: CorrelationConfig; // used when protocol = iso8583
  headerEcho: HeaderEchoConfig; // used when protocol = header_echo
  signOn: boolean;
  keepalive: KeepaliveConfig;
  /** TCP: the health check mode and message (`health_check`). */
  healthCheck: HealthCheckConfig;
  /** TCP / ISO 8583: the Message Format the socket decodes with
   *  (`message_format_id`; "" = the stock ISO 8583:1987 table). */
  messageFormatId: string;
  /** Every stored key this form has no field for (`mac`, `emv`, `tls`,
   *  `fields`, `external`, a vendor `mhdr`, …), top-level and, under the
   *  section's name, the unmodelled sub-keys of a modelled section
   *  (`sign_on.payload`, `framing.header`, …). Re-emitted untouched by
   *  {@link toAdapterConfig} so a save from this form never drops what the
   *  API or MCP `save_connection` wrote. Editable as JSON on the page. */
  extra: Record<string, unknown>;
  grpc: GrpcConfig; // used when adapter = "grpc"
  rest: RestConfig; // used when adapter = "http"
  nats: NatsConfig; // used when adapter = "nats"
  payshield: PayShieldConfig; // used when adapter = "payshield"
  db: DbConfig; // used when adapter = "db_probe_core" | "db_probe_switch"
}

export const DEFAULT_GRPC: GrpcConfig = {
  target: "",
  schemaSource: "descriptor",
  descriptorSetPath: "",
  descriptorSetB64: "",
  protoFiles: [],
  protoPaths: "",
  protoIncludeDirs: "",
  tlsEnabled: false,
  tlsRootCertsPath: "",
  timeoutSec: 30,
  metadata: [],
  actions: [],
};

export const DEFAULT_REST: RestConfig = {
  baseUrl: "",
  apiKey: "",
  authType: "",
  username: "",
  password: "",
  timeoutMs: 5000,
};

export const DEFAULT_PAYSHIELD: PayShieldConfig = {
  header: "",
  responseTimeoutSec: 5,
};

export const DEFAULT_HEALTH_CHECK: HealthCheckConfig = {
  mode: "probe",
  action: "",
  payloadJson: "",
  settleMs: null,
};

export const DEFAULT_DB: DbConfig = {
  engine: "postgresql",
  dbname: "",
  user: "",
  password: "",
  dsn: "",
  writes: "",
  maxRows: 100,
  statementTimeoutMs: 5000,
  loadOk: false,
  queriesJson: "",
  initSql: "",
};

/** Parse the named-queries JSON; null when empty or not an object. */
export function parseDbQueries(text: string): Record<string, unknown> | null {
  const t = (text ?? "").trim();
  if (!t) return null;
  try {
    const v = JSON.parse(t);
    return v && typeof v === "object" && !Array.isArray(v)
      ? (v as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export const DEFAULT_NATS: NatsConfig = {
  servers: "",
  subject: "",
  subjects: "",
  queueGroup: "",
  codec: "json",
  requestTimeoutSec: 2,
  authToken: "",
  authNkeySeed: "",
  authCreds: "",
  jetstreamStream: "",
  jetstreamSubjects: "",
  jetstreamDurable: "",
};

export const DEFAULT_FRAMING: FramingConfig = {
  preset: "",
  linkLevel: "",
  mode: "length_prefix",
  lengthPrefixBytes: 2,
  lengthEncoding: "binary",
  byteOrder: "big",
  lengthIncludesPrefix: false,
  lengthIncludesHeader: true,
  tpduBytes: 0,
  tpduOutboundHex: "",
  headerHex: "",
  trailerHex: "",
  startHex: "02",
  endHex: "03",
  checksum: "none",
  encoding: "ascii",
};

/** The `framing` object of an adapter config (snake_case keys) from a draft.
 *  Sparse by default (defaults omitted, the way connections are stored);
 *  `full` writes every key, for a config a human reads as JSON. */
export function framingToApi(
  f: FramingConfig,
  full = false,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (f.preset) out["preset"] = f.preset;
  if (f.linkLevel) out["link_level"] = f.linkLevel;
  const delimited = f.mode === "delimited";
  if (full || delimited) out["mode"] = f.mode ?? "length_prefix";
  if (delimited) {
    // a start/end envelope has no length prefix, header or trailer
    if (full || (f.startHex && f.startHex !== DEFAULT_FRAMING.startHex))
      out["start_hex"] = f.startHex || DEFAULT_FRAMING.startHex;
    if (full || (f.endHex && f.endHex !== DEFAULT_FRAMING.endHex))
      out["end_hex"] = f.endHex || DEFAULT_FRAMING.endHex;
    if (full || (f.checksum && f.checksum !== "none"))
      out["checksum"] = f.checksum ?? "none";
    if (full || f.encoding !== DEFAULT_FRAMING.encoding)
      out["encoding"] = f.encoding;
    return out;
  }
  if (full || f.lengthPrefixBytes !== DEFAULT_FRAMING.lengthPrefixBytes)
    out["length_prefix_bytes"] = f.lengthPrefixBytes;
  if (full || f.lengthEncoding !== DEFAULT_FRAMING.lengthEncoding)
    out["length_encoding"] = f.lengthEncoding;
  if (full || f.byteOrder !== DEFAULT_FRAMING.byteOrder)
    out["length_byte_order"] = f.byteOrder;
  if (full || f.lengthIncludesPrefix)
    out["length_includes_prefix"] = !!f.lengthIncludesPrefix;
  if (full || !f.lengthIncludesHeader)
    out["length_includes_header"] = !!f.lengthIncludesHeader;
  if (full || f.tpduBytes) out["tpdu_bytes"] = f.tpduBytes || 0;
  if (full || f.tpduOutboundHex)
    out["tpdu_outbound_hex"] = f.tpduOutboundHex || "";
  if (full || f.headerHex) out["header_hex"] = f.headerHex || "";
  if (full || f.trailerHex) out["trailer_hex"] = f.trailerHex || "";
  if (full || f.encoding !== DEFAULT_FRAMING.encoding)
    out["encoding"] = f.encoding;
  return out;
}

/** A framing draft from an adapter config's `framing` object (or nothing). */
export function framingFromApi(
  f: Record<string, any> | undefined | null,
): FramingConfig {
  const src = f ?? {};
  return {
    preset: (src["preset"] ?? "") as FramingPreset | "",
    linkLevel: src["link_level"] ?? "",
    mode: (src["mode"] ?? DEFAULT_FRAMING.mode) as FramingMode,
    headerHex: src["header_hex"] ?? "",
    trailerHex: src["trailer_hex"] ?? "",
    startHex: src["start_hex"] ?? DEFAULT_FRAMING.startHex,
    endHex: src["end_hex"] ?? DEFAULT_FRAMING.endHex,
    checksum: (src["checksum"] ?? "none") as "none" | "lrc",
    lengthPrefixBytes:
      src["length_prefix_bytes"] ?? DEFAULT_FRAMING.lengthPrefixBytes,
    lengthEncoding: (src["length_encoding"] ??
      DEFAULT_FRAMING.lengthEncoding) as LengthEncoding,
    byteOrder: (src["length_byte_order"] ??
      DEFAULT_FRAMING.byteOrder) as ByteOrder,
    lengthIncludesPrefix: src["length_includes_prefix"] ?? false,
    lengthIncludesHeader: src["length_includes_header"] ?? true,
    tpduBytes: src["tpdu_bytes"] ?? 0,
    tpduOutboundHex: src["tpdu_outbound_hex"] ?? "",
    encoding: src["encoding"] ?? DEFAULT_FRAMING.encoding,
  };
}

export function blankConnection(name = ""): Connection {
  return {
    name,
    adapter: "tcp",
    protocol: "iso8583",
    host: "",
    port: 7000,
    mode: "outbound",
    disabled: false,
    default: false,
    environmentOverrides: {},
    poolSize: undefined,
    responseTimeoutSec: 30,
    connectTimeoutSec: 10,
    framing: { ...DEFAULT_FRAMING },
    correlation: { field: "11", autoGenerate: true },
    headerEcho: {
      headerBytes: 4,
      responseCommandBytes: 2,
      errorFieldBytes: 2,
      diagnosticCommand: "NC",
    },
    signOn: true,
    keepalive: { enabled: true, intervalSec: 30 },
    healthCheck: { ...DEFAULT_HEALTH_CHECK },
    messageFormatId: "",
    description: "",
    extra: {},
    grpc: structuredClone(DEFAULT_GRPC),
    rest: { ...DEFAULT_REST },
    nats: { ...DEFAULT_NATS },
    payshield: { ...DEFAULT_PAYSHIELD },
    db: { ...DEFAULT_DB },
  };
}

// -- passthrough of keys the form does not model -----------------------------
//
// A stored connection may carry keys this form has no field for: written by the
// API / MCP `save_connection`, by a provider pack, or by an older portal. The
// backend PUT replaces the whole record, so a save that only re-emits the form's
// own fields drops them (that is how a `health_check` and a `message_format_id`
// vanished from a live connection on 2026-11-09). Every branch below therefore
// keeps the complement of its *modelled* keys in `extra` and spreads it back
// first on save; the modelled keys win. "Modelled" means the form has a field
// for the key, not that it is emitted: a modelled key reset to its default is
// omitted on save and must never be resurrected from `extra`.

/** Keys the service body / backend own (never adapter config, never extra). */
const META_KEYS = [
  "name",
  "type",
  "adapter",
  "extends",
  "listen_port",
  "mode",
  "disabled",
  "default",
  "environment_overrides",
  "description",
];
const TCP_KEYS = [
  "protocol",
  "host",
  "port",
  "framing",
  "correlation",
  "header_echo",
  "sign_on",
  "keepalive",
  "health_check",
  "message_format_id",
  "response_timeout_sec",
  "connect_timeout_sec",
  "pool_size",
];
const TCP_SECTIONS: Record<string, string[]> = {
  framing: [
    "preset",
    "link_level",
    "mode",
    "start_hex",
    "end_hex",
    "checksum",
    "length_prefix_bytes",
    "length_encoding",
    "length_byte_order",
    "length_includes_prefix",
    "length_includes_header",
    "tpdu_bytes",
    "tpdu_outbound_hex",
    "header_hex",
    "trailer_hex",
    "encoding",
  ],
  // match_mti / response_mti_map were removed 2026-11-12 (they stranded every
  // MTI outside a fixed map); listed so a stored value is dropped on save.
  correlation: ["field", "auto_generate", "match_mti", "response_mti_map"],
  header_echo: [
    "header_bytes",
    "response_command_bytes",
    "error_field_bytes",
    "diagnostic_command",
  ],
  sign_on: ["enabled"],
  keepalive: ["enabled", "interval_sec"],
  health_check: ["mode", "action", "payload", "settle_ms"],
};
const GRPC_KEYS = [
  "host",
  "port",
  "target",
  "reflection",
  "proto_inline",
  "proto_paths",
  "proto_include_dirs",
  "descriptor_set_b64",
  "descriptor_set_path",
  "tls",
  "metadata",
  "actions",
  "timeout_sec",
];
const GRPC_SECTIONS: Record<string, string[]> = {
  tls: ["enabled", "root_certs_path"],
};
const HTTP_KEYS = [
  "base_url",
  "api_key",
  "auth_type",
  "username",
  "password",
  "timeout_ms",
];
const NATS_KEYS = [
  "host",
  "port",
  "servers",
  "subject",
  "subjects",
  "queue_group",
  "codec",
  "request_timeout_sec",
  "auth",
  "jetstream",
];
const NATS_SECTIONS: Record<string, string[]> = {
  auth: ["token", "nkey_seed", "creds"],
  jetstream: ["stream", "subjects", "durable"],
};
const PAYSHIELD_KEYS = ["host", "port", "header", "response_timeout_sec"];
const DB_KEYS = [
  "host",
  "port",
  "engine",
  "dbname",
  "user",
  "password",
  "dsn",
  "writes",
  "max_rows",
  "statement_timeout_ms",
  "load_ok",
  "queries",
  "init_sql",
];

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/** The entries of `src` not in `modelled`. Nulls are skipped (the API returns
 *  every response-model field, `listen_port: null` included), and so is the
 *  backend default `external: false`, so a record never grows noise. */
function restOf(
  src: Record<string, unknown> | undefined | null,
  modelled: readonly string[],
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(src ?? {})) {
    if (v === null || v === undefined) continue;
    if (modelled.includes(k)) continue;
    if (k === "external" && v === false) continue;
    out[k] = v;
  }
  return out;
}

/** Build `extra` for a stored config: top-level keys outside `modelled` (and
 *  outside the backend's own metadata), plus, per modelled section, the
 *  sub-keys the form does not edit. */
export function extraOf(
  cfg: Record<string, unknown>,
  modelled: readonly string[],
  sections: Record<string, string[]> = {},
): Record<string, unknown> {
  const extra = restOf(cfg, [...META_KEYS, ...modelled]);
  for (const [name, sub] of Object.entries(sections)) {
    const own = cfg[name];
    if (!isPlainObject(own)) continue;
    const rest = restOf(own, sub);
    if (Object.keys(rest).length) extra[name] = rest;
  }
  return extra;
}

/** `cfg` (the form's own emission) over `extra`: unknown top-level keys are
 *  restored, and a section's unmodelled sub-keys are merged back under the
 *  section's emitted keys (the emitted ones win). The adapter stays first. */
function withExtra(
  cfg: Record<string, unknown>,
  extra: Record<string, unknown> | undefined,
  sections: Record<string, string[]> = {},
): Record<string, unknown> {
  const x = extra ?? {};
  const out: Record<string, unknown> = { adapter: cfg["adapter"] };
  for (const [k, v] of Object.entries(x)) {
    if (k in sections) continue;
    if (META_KEYS.includes(k)) continue;
    out[k] = v;
  }
  Object.assign(out, cfg);
  for (const name of Object.keys(sections)) {
    const rest = x[name];
    if (!isPlainObject(rest) || !Object.keys(rest).length) continue;
    const own = cfg[name];
    out[name] = { ...rest, ...(isPlainObject(own) ? own : {}) };
  }
  return out;
}

/** Parse JSON text that must be an object; null when empty or not an object.
 *  Throws on malformed JSON so the caller can name the field. */
export function parseJsonObject(text: string): Record<string, unknown> | null {
  const t = (text ?? "").trim();
  if (!t) return null;
  const v = JSON.parse(t);
  return isPlainObject(v) ? v : null;
}

/** The names of the keys {@link toAdapterConfig} re-emits from `extra`
 *  (dotted for section sub-keys), for a summary line. */
export function extraKeyNames(extra: Record<string, unknown>): string[] {
  const names: string[] = [];
  for (const [k, v] of Object.entries(extra ?? {})) {
    if (isPlainObject(v) && k in TCP_SECTIONS)
      names.push(...Object.keys(v).map((sub) => `${k}.${sub}`));
    else names.push(k);
  }
  return names.sort();
}

/** Emit the worker-shaped gRPC config object (defaults omitted). */
function grpcToAdapterConfig(i: Connection): Record<string, unknown> {
  const g = i.grpc;
  const cfg: Record<string, unknown> = { adapter: "grpc" };
  if (i.extends) cfg["extends"] = i.extends;

  if (g.target.trim()) cfg["target"] = g.target.trim();
  else {
    if (i.host) cfg["host"] = i.host;
    if (i.port) cfg["port"] = i.port;
  }

  // Schema source: descriptor set / custom .proto / server reflection.
  if (g.schemaSource === "reflection") {
    cfg["reflection"] = true;
  } else if (g.schemaSource === "proto") {
    // Emit pasted/dropped files as a { filename: text } map so cross-file
    // imports resolve; the worker compiles them together.
    const files: Record<string, string> = {};
    for (const f of g.protoFiles) {
      const name = f.name.trim();
      if (name && f.text.trim()) files[name] = f.text;
    }
    if (Object.keys(files).length) cfg["proto_inline"] = files;
    const paths = splitList(g.protoPaths);
    if (paths.length) cfg["proto_paths"] = paths;
    const incs = splitList(g.protoIncludeDirs);
    if (incs.length) cfg["proto_include_dirs"] = incs;
  } else {
    if (g.descriptorSetB64.trim())
      cfg["descriptor_set_b64"] = g.descriptorSetB64.trim();
    else if (g.descriptorSetPath.trim())
      cfg["descriptor_set_path"] = g.descriptorSetPath.trim();
  }

  if (g.tlsEnabled) {
    const tls: Record<string, unknown> = { enabled: true };
    if (g.tlsRootCertsPath.trim())
      tls["root_certs_path"] = g.tlsRootCertsPath.trim();
    cfg["tls"] = tls;
  }

  const meta: Record<string, string> = {};
  for (const m of g.metadata) {
    if (m.key.trim()) meta[m.key.trim()] = m.value;
  }
  if (Object.keys(meta).length) cfg["metadata"] = meta;

  const actions: Record<string, unknown> = {};
  for (const a of g.actions) {
    if (a.name.trim() && a.method.trim())
      actions[a.name.trim()] = { method: a.method.trim() };
  }
  if (Object.keys(actions).length) cfg["actions"] = actions;

  if (g.timeoutSec !== DEFAULT_GRPC.timeoutSec)
    cfg["timeout_sec"] = g.timeoutSec;
  return withExtra(cfg, i.extra, GRPC_SECTIONS);
}

/** Emit the worker-shaped REST/http config (defaults omitted). */
function restToAdapterConfig(i: Connection): Record<string, unknown> {
  const r = i.rest;
  const cfg: Record<string, unknown> = { adapter: "http" };
  if (i.extends) cfg["extends"] = i.extends;
  if (r.baseUrl.trim()) cfg["base_url"] = r.baseUrl.trim();
  if (r.apiKey.trim()) cfg["api_key"] = r.apiKey.trim();
  if (r.authType) cfg["auth_type"] = r.authType;
  if (r.username.trim()) cfg["username"] = r.username.trim();
  if (r.password) cfg["password"] = r.password;
  if (r.timeoutMs !== DEFAULT_REST.timeoutMs) cfg["timeout_ms"] = r.timeoutMs;
  return withExtra(cfg, i.extra);
}

/** Emit the worker-shaped NATS config (ADR-0006; defaults omitted). */
function natsToAdapterConfig(i: Connection): Record<string, unknown> {
  const n = i.nats;
  const cfg: Record<string, unknown> = { adapter: "nats" };
  if (i.extends) cfg["extends"] = i.extends;
  const servers = splitList(n.servers);
  if (servers.length) cfg["servers"] = servers;
  else {
    if (i.host) cfg["host"] = i.host;
    if (i.port) cfg["port"] = i.port;
  }
  if (n.subject.trim()) cfg["subject"] = n.subject.trim();
  const subjects = splitList(n.subjects);
  if (subjects.length) cfg["subjects"] = subjects;
  if (n.queueGroup.trim()) cfg["queue_group"] = n.queueGroup.trim();
  if (n.codec && n.codec !== "json") cfg["codec"] = n.codec;
  if (n.requestTimeoutSec !== DEFAULT_NATS.requestTimeoutSec)
    cfg["request_timeout_sec"] = n.requestTimeoutSec;

  const auth: Record<string, unknown> = {};
  if (n.authToken.trim()) auth["token"] = n.authToken.trim();
  if (n.authNkeySeed.trim()) auth["nkey_seed"] = n.authNkeySeed.trim();
  if (n.authCreds.trim()) auth["creds"] = n.authCreds.trim();
  if (Object.keys(auth).length) cfg["auth"] = auth;

  if (n.jetstreamStream.trim()) {
    const js: Record<string, unknown> = { stream: n.jetstreamStream.trim() };
    const jsSubs = splitList(n.jetstreamSubjects);
    if (jsSubs.length) js["subjects"] = jsSubs;
    if (n.jetstreamDurable.trim()) js["durable"] = n.jetstreamDurable.trim();
    cfg["jetstream"] = js;
  }
  return withExtra(cfg, i.extra, NATS_SECTIONS);
}

/** Emit the worker-shaped payShield-client config (defaults omitted). */
function payshieldToAdapterConfig(i: Connection): Record<string, unknown> {
  const p = i.payshield;
  const cfg: Record<string, unknown> = { adapter: "payshield" };
  if (i.extends) cfg["extends"] = i.extends;
  if (i.host) cfg["host"] = i.host;
  if (i.port) cfg["port"] = i.port;
  if (p.header.trim()) cfg["header"] = p.header.trim();
  if (p.responseTimeoutSec !== DEFAULT_PAYSHIELD.responseTimeoutSec)
    cfg["response_timeout_sec"] = p.responseTimeoutSec;
  return withExtra(cfg, i.extra);
}

/** Emit the worker-shaped DB-probe config (defaults omitted). */
function dbToAdapterConfig(i: Connection): Record<string, unknown> {
  const db = i.db;
  const cfg: Record<string, unknown> = { adapter: i.adapter };
  if (i.extends) cfg["extends"] = i.extends;
  if (db.engine) cfg["engine"] = db.engine;
  if (i.host) cfg["host"] = i.host;
  if (i.port) cfg["port"] = i.port;
  if (db.dbname.trim()) cfg["dbname"] = db.dbname.trim();
  if (db.user.trim()) cfg["user"] = db.user.trim();
  if (db.password) cfg["password"] = db.password;
  if (db.dsn?.trim()) cfg["dsn"] = db.dsn.trim();
  if (db.writes === "permanent") cfg["writes"] = "permanent";
  else if (db.writes === "true") cfg["writes"] = true;
  if (db.maxRows && db.maxRows !== DEFAULT_DB.maxRows)
    cfg["max_rows"] = db.maxRows;
  if (
    db.statementTimeoutMs &&
    db.statementTimeoutMs !== DEFAULT_DB.statementTimeoutMs
  )
    cfg["statement_timeout_ms"] = db.statementTimeoutMs;
  if (db.loadOk) cfg["load_ok"] = true;
  const queries = parseDbQueries(db.queriesJson);
  if (queries && Object.keys(queries).length) cfg["queries"] = queries;
  const init = (db.initSql ?? "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (db.engine === "sqlite" && init.length) cfg["init_sql"] = init;
  return withExtra(cfg, i.extra);
}

/** Emit the worker-shaped config object for one connection (defaults omitted). */
export function toAdapterConfig(i: Connection): Record<string, unknown> {
  if (i.adapter === "grpc") return grpcToAdapterConfig(i);
  if (i.adapter === "http") return restToAdapterConfig(i);
  if (i.adapter === "nats") return natsToAdapterConfig(i);
  if (i.adapter === "payshield") return payshieldToAdapterConfig(i);
  if (i.adapter === "db_probe_core" || i.adapter === "db_probe_switch")
    return dbToAdapterConfig(i);
  // An `extends` connection only needs the keys it overrides.
  const cfg: Record<string, unknown> = { adapter: i.adapter };
  if (i.extends) cfg["extends"] = i.extends;
  else cfg["protocol"] = i.protocol;

  if (i.host) cfg["host"] = i.host;
  if (i.port) cfg["port"] = i.port;

  const framing = framingToApi(i.framing);
  if (Object.keys(framing).length) cfg["framing"] = framing;

  if (!i.extends && i.protocol === "iso8583") {
    const c: Record<string, unknown> = {};
    if (i.correlation.field !== "11") c["field"] = i.correlation.field;
    if (!i.correlation.autoGenerate) c["auto_generate"] = false;
    if (Object.keys(c).length) cfg["correlation"] = c;
  }

  if (!i.extends && i.protocol === "header_echo") {
    const h = i.headerEcho;
    const he: Record<string, unknown> = {};
    if (h.headerBytes !== 4) he["header_bytes"] = h.headerBytes;
    if (h.responseCommandBytes !== 2)
      he["response_command_bytes"] = h.responseCommandBytes;
    if (h.errorFieldBytes !== 2) he["error_field_bytes"] = h.errorFieldBytes;
    if (h.diagnosticCommand !== "NC")
      he["diagnostic_command"] = h.diagnosticCommand;
    if (Object.keys(he).length) cfg["header_echo"] = he;
  }

  if (i.signOn) cfg["sign_on"] = { enabled: true };
  if (i.keepalive.enabled)
    cfg["keepalive"] = { enabled: true, interval_sec: i.keepalive.intervalSec };

  if (i.responseTimeoutSec !== 30)
    cfg["response_timeout_sec"] = i.responseTimeoutSec;
  if (i.connectTimeoutSec !== 10)
    cfg["connect_timeout_sec"] = i.connectTimeoutSec;
  if (i.poolSize) cfg["pool_size"] = i.poolSize;

  const hc = healthCheckToApi(i.healthCheck);
  if (Object.keys(hc).length) cfg["health_check"] = hc;
  if (i.messageFormatId.trim())
    cfg["message_format_id"] = i.messageFormatId.trim();

  return withExtra(cfg, i.extra, TCP_SECTIONS);
}

/** The `health_check` section from the form (sparse: `probe` with nothing
 *  else is the worker default and emits nothing). Malformed payload JSON
 *  throws; {@link ConnectionsComponent.save} validates first. */
export function healthCheckToApi(
  h: HealthCheckConfig,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (h.mode && h.mode !== "probe") out["mode"] = h.mode;
  if (h.action.trim()) out["action"] = h.action.trim();
  const payload = parseJsonObject(h.payloadJson);
  if (payload) out["payload"] = payload;
  if (h.settleMs !== null && h.settleMs !== undefined && h.settleMs > 0)
    out["settle_ms"] = h.settleMs;
  return out;
}

/** The form's health-check model from a stored `health_check` section. */
export function healthCheckFromApi(
  raw: Record<string, any> | undefined | null,
): HealthCheckConfig {
  const hc = raw ?? {};
  const mode = String(hc["mode"] ?? "probe").toLowerCase();
  return {
    mode: (HEALTH_CHECK_MODES as readonly string[]).includes(mode)
      ? (mode as HealthCheckMode)
      : "probe",
    action: hc["action"] ?? "",
    payloadJson: isPlainObject(hc["payload"])
      ? JSON.stringify(hc["payload"], null, 2)
      : "",
    settleMs:
      hc["settle_ms"] === undefined || hc["settle_ms"] === null
        ? null
        : Number(hc["settle_ms"]),
  };
}

/** Parse a stored worker-shaped config back into the editable form model
 * (inverse of {@link toAdapterConfig}; missing keys fall back to defaults). */
export function fromAdapterConfig(
  name: string,
  cfg: Record<string, any>,
): Connection {
  const base = blankConnection(name);

  if ((cfg["adapter"] ?? cfg["type"]) === "grpc") {
    const tls = (cfg["tls"] ?? {}) as Record<string, any>;
    const meta = (cfg["metadata"] ?? {}) as Record<string, any>;
    const acts = (cfg["actions"] ?? {}) as Record<string, any>;
    return {
      ...base,
      name,
      description: cfg["description"] ?? "",
      extra: extraOf(cfg, GRPC_KEYS, GRPC_SECTIONS),
      adapter: "grpc",
      mode: cfg["mode"] ?? "outbound",
      disabled: cfg["disabled"] === true,
      default: cfg["default"] === true,
      environmentOverrides: cfg["environment_overrides"] ?? {},
      extends: cfg["extends"],
      host: cfg["host"] ?? "",
      // fold legacy listen_port into the single port
      port: cfg["listen_port"] ?? cfg["port"] ?? 0,
      grpc: {
        target: cfg["target"] ?? "",
        schemaSource: cfg["reflection"]
          ? "reflection"
          : cfg["proto_inline"] || cfg["proto_paths"]
            ? "proto"
            : "descriptor",
        descriptorSetPath: cfg["descriptor_set_path"] ?? "",
        descriptorSetB64: cfg["descriptor_set_b64"] ?? "",
        protoFiles: protoFilesFromConfig(cfg["proto_inline"]),
        protoPaths: (cfg["proto_paths"] ?? []).join("\n"),
        protoIncludeDirs: (cfg["proto_include_dirs"] ?? []).join("\n"),
        tlsEnabled: tls["enabled"] === true,
        tlsRootCertsPath: tls["root_certs_path"] ?? "",
        timeoutSec: cfg["timeout_sec"] ?? DEFAULT_GRPC.timeoutSec,
        metadata: Object.entries(meta).map(([key, value]) => ({
          key,
          value: String(value),
        })),
        actions: Object.entries(acts).map(([aname, spec]) => ({
          name: aname,
          method: (spec as Record<string, any>)?.["method"] ?? "",
        })),
      },
    };
  }

  const impl = (cfg["adapter"] ?? cfg["type"]) as string | undefined;
  if (impl === "http") {
    return {
      ...base,
      name,
      description: cfg["description"] ?? "",
      extra: extraOf(cfg, HTTP_KEYS),
      adapter: "http",
      mode: cfg["mode"] ?? "outbound",
      disabled: cfg["disabled"] === true,
      default: cfg["default"] === true,
      environmentOverrides: cfg["environment_overrides"] ?? {},
      extends: cfg["extends"],
      rest: {
        baseUrl: cfg["base_url"] ?? "",
        apiKey: cfg["api_key"] ?? "",
        authType: (cfg["auth_type"] ?? "") as RestConfig["authType"],
        username: cfg["username"] ?? "",
        password: cfg["password"] ?? "",
        timeoutMs: cfg["timeout_ms"] ?? DEFAULT_REST.timeoutMs,
      },
    };
  }
  if (impl === "nats") {
    const auth = (cfg["auth"] ?? {}) as Record<string, any>;
    const js = (cfg["jetstream"] ?? {}) as Record<string, any>;
    const servers = cfg["servers"] ?? [];
    const subjects = cfg["subjects"] ?? [];
    return {
      ...base,
      name,
      description: cfg["description"] ?? "",
      extra: extraOf(cfg, NATS_KEYS, NATS_SECTIONS),
      adapter: "nats",
      mode: cfg["mode"] ?? "outbound",
      disabled: cfg["disabled"] === true,
      default: cfg["default"] === true,
      environmentOverrides: cfg["environment_overrides"] ?? {},
      extends: cfg["extends"],
      host: cfg["host"] ?? "",
      port: cfg["listen_port"] ?? cfg["port"] ?? 4222,
      nats: {
        servers: (Array.isArray(servers) ? servers : [servers]).join("\n"),
        subject: cfg["subject"] ?? "",
        subjects: (Array.isArray(subjects) ? subjects : [subjects]).join("\n"),
        queueGroup: cfg["queue_group"] ?? "",
        codec: (cfg["codec"] ?? "json") as NatsConfig["codec"],
        requestTimeoutSec:
          cfg["request_timeout_sec"] ?? DEFAULT_NATS.requestTimeoutSec,
        authToken: auth["token"] ?? "",
        authNkeySeed: auth["nkey_seed"] ?? "",
        authCreds: auth["creds"] ?? "",
        jetstreamStream: js["stream"] ?? "",
        jetstreamSubjects: (js["subjects"] ?? []).join("\n"),
        jetstreamDurable: js["durable"] ?? "",
      },
    };
  }
  if (impl === "payshield" || impl === "hsm_client") {
    return {
      ...base,
      name,
      description: cfg["description"] ?? "",
      extra: extraOf(cfg, PAYSHIELD_KEYS),
      adapter: "payshield",
      mode: cfg["mode"] ?? "outbound",
      disabled: cfg["disabled"] === true,
      default: cfg["default"] === true,
      environmentOverrides: cfg["environment_overrides"] ?? {},
      extends: cfg["extends"],
      host: cfg["host"] ?? "",
      port: cfg["listen_port"] ?? cfg["port"] ?? 0,
      payshield: {
        header: cfg["header"] ?? "",
        responseTimeoutSec:
          cfg["response_timeout_sec"] ?? DEFAULT_PAYSHIELD.responseTimeoutSec,
      },
    };
  }
  if (impl === "db_probe_core" || impl === "db_probe_switch") {
    return {
      ...base,
      name,
      description: cfg["description"] ?? "",
      extra: extraOf(cfg, DB_KEYS),
      adapter: impl,
      mode: cfg["mode"] ?? "outbound",
      disabled: cfg["disabled"] === true,
      default: cfg["default"] === true,
      environmentOverrides: cfg["environment_overrides"] ?? {},
      extends: cfg["extends"],
      host: cfg["host"] ?? "",
      port: cfg["listen_port"] ?? cfg["port"] ?? 0,
      db: {
        engine: cfg["engine"] ?? "postgresql",
        dbname: cfg["dbname"] ?? "",
        user: cfg["user"] ?? "",
        password: cfg["password"] ?? "",
        dsn: cfg["dsn"] ?? "",
        writes:
          cfg["writes"] === "permanent"
            ? "permanent"
            : cfg["writes"] === true || cfg["writes"] === "true"
              ? "true"
              : "",
        maxRows: Number(cfg["max_rows"] ?? DEFAULT_DB.maxRows),
        statementTimeoutMs: Number(
          cfg["statement_timeout_ms"] ?? DEFAULT_DB.statementTimeoutMs,
        ),
        loadOk: cfg["load_ok"] === true,
        queriesJson: cfg["queries"]
          ? JSON.stringify(cfg["queries"], null, 2)
          : "",
        initSql: Array.isArray(cfg["init_sql"])
          ? (cfg["init_sql"] as unknown[]).map(String).join("\n")
          : "",
      },
    };
  }

  const f = (cfg["framing"] ?? {}) as Record<string, any>;
  const c = (cfg["correlation"] ?? {}) as Record<string, any>;
  const he = (cfg["header_echo"] ?? {}) as Record<string, any>;
  const ka = (cfg["keepalive"] ?? {}) as Record<string, any>;
  return {
    name,
    adapter: "tcp",
    description: cfg["description"] ?? "",
    extra: extraOf(cfg, TCP_KEYS, TCP_SECTIONS),
    healthCheck: healthCheckFromApi(cfg["health_check"]),
    messageFormatId: cfg["message_format_id"] ?? "",
    protocol: (cfg["protocol"] ?? "iso8583") as TcpProtocol,
    host: cfg["host"] ?? "",
    // fold legacy listen_port into the single port (inbound bind = same port)
    port: cfg["listen_port"] ?? cfg["port"] ?? 7000,
    mode: cfg["mode"] ?? "outbound",
    disabled: cfg["disabled"] === true,
    default: cfg["default"] === true,
    environmentOverrides: cfg["environment_overrides"] ?? {},
    extends: cfg["extends"],
    poolSize: cfg["pool_size"],
    responseTimeoutSec: cfg["response_timeout_sec"] ?? 30,
    connectTimeoutSec: cfg["connect_timeout_sec"] ?? 10,
    framing: framingFromApi(f),
    correlation: {
      field: c["field"] ?? "11",
      autoGenerate: c["auto_generate"] !== false,
    },
    headerEcho: {
      headerBytes: he["header_bytes"] ?? 4,
      responseCommandBytes: he["response_command_bytes"] ?? 2,
      errorFieldBytes: he["error_field_bytes"] ?? 2,
      diagnosticCommand: he["diagnostic_command"] ?? "NC",
    },
    signOn: cfg["sign_on"]?.["enabled"] === true,
    keepalive: {
      enabled: ka["enabled"] === true,
      intervalSec: ka["interval_sec"] ?? 30,
    },
    grpc: structuredClone(DEFAULT_GRPC),
    rest: { ...DEFAULT_REST },
    nats: { ...DEFAULT_NATS },
    payshield: { ...DEFAULT_PAYSHIELD },
    db: { ...DEFAULT_DB },
  };
}
