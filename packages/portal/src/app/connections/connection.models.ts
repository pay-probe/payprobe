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

export interface FramingConfig {
  lengthPrefixBytes: number;
  byteOrder: ByteOrder;
  lengthIncludesPrefix: boolean;
  lengthIncludesHeader: boolean;
  tpduBytes: number;
  tpduOutboundHex: string;
  encoding: string;
}

export interface CorrelationConfig {
  field: string;
  autoGenerate: boolean;
  matchMti: boolean;
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

/** Read-only DB-probe settings (used when adapter = "db_probe_core" /
 * "db_probe_switch"). Uses the shared host/port plus engine + database. */
export interface DbConfig {
  engine: string;
  dbname: string;
  user: string;
  password: string;
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

export interface Connection {
  name: string;
  adapter: AdapterImpl;
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

export const DEFAULT_DB: DbConfig = {
  engine: "postgresql",
  dbname: "",
  user: "",
  password: "",
};

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
  lengthPrefixBytes: 2,
  byteOrder: "big",
  lengthIncludesPrefix: false,
  lengthIncludesHeader: true,
  tpduBytes: 0,
  tpduOutboundHex: "",
  encoding: "ascii",
};

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
    correlation: { field: "11", autoGenerate: true, matchMti: false },
    headerEcho: {
      headerBytes: 4,
      responseCommandBytes: 2,
      errorFieldBytes: 2,
      diagnosticCommand: "NC",
    },
    signOn: true,
    keepalive: { enabled: true, intervalSec: 30 },
    grpc: structuredClone(DEFAULT_GRPC),
    rest: { ...DEFAULT_REST },
    nats: { ...DEFAULT_NATS },
    payshield: { ...DEFAULT_PAYSHIELD },
    db: { ...DEFAULT_DB },
  };
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
  return cfg;
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
  return cfg;
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
  return cfg;
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
  return cfg;
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
  return cfg;
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

  const f = i.framing;
  const framing: Record<string, unknown> = {};
  if (f.lengthPrefixBytes !== DEFAULT_FRAMING.lengthPrefixBytes)
    framing["length_prefix_bytes"] = f.lengthPrefixBytes;
  if (f.byteOrder !== DEFAULT_FRAMING.byteOrder)
    framing["length_byte_order"] = f.byteOrder;
  if (f.lengthIncludesPrefix) framing["length_includes_prefix"] = true;
  if (!f.lengthIncludesHeader) framing["length_includes_header"] = false;
  if (f.tpduBytes) framing["tpdu_bytes"] = f.tpduBytes;
  if (f.tpduOutboundHex) framing["tpdu_outbound_hex"] = f.tpduOutboundHex;
  if (f.encoding !== DEFAULT_FRAMING.encoding) framing["encoding"] = f.encoding;
  if (Object.keys(framing).length) cfg["framing"] = framing;

  if (!i.extends && i.protocol === "iso8583") {
    const c: Record<string, unknown> = {};
    if (i.correlation.field !== "11") c["field"] = i.correlation.field;
    if (!i.correlation.autoGenerate) c["auto_generate"] = false;
    if (i.correlation.matchMti) c["match_mti"] = true;
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

  return cfg;
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
    framing: {
      lengthPrefixBytes:
        f["length_prefix_bytes"] ?? DEFAULT_FRAMING.lengthPrefixBytes,
      byteOrder: (f["length_byte_order"] ??
        DEFAULT_FRAMING.byteOrder) as ByteOrder,
      lengthIncludesPrefix: f["length_includes_prefix"] ?? false,
      lengthIncludesHeader: f["length_includes_header"] ?? true,
      tpduBytes: f["tpdu_bytes"] ?? 0,
      tpduOutboundHex: f["tpdu_outbound_hex"] ?? "",
      encoding: f["encoding"] ?? DEFAULT_FRAMING.encoding,
    },
    correlation: {
      field: c["field"] ?? "11",
      autoGenerate: c["auto_generate"] !== false,
      matchMti: c["match_mti"] === true,
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
