import { Injectable, computed, signal } from "@angular/core";

import { environment } from "../../environments/environment";

/** Stable identity for each service the portal talks to / monitors. */
export type EndpointKey =
  | "portal"
  | "orchestrator"
  | "scenario"
  | "auth"
  | "assistant"
  | "insight"
  | "redis"
  | "mcp";

/** Editable connection details for one endpoint. */
export interface EndpointConfig {
  key: EndpointKey;
  /** Human label shown in the UI. */
  label: string;
  /** http | https | redis */
  scheme: string;
  host: string;
  port: number;
  /**
   * "http" endpoints are probed directly from the browser. "redis" and "mcp"
   * can't be reached from a browser (TCP / SSE + CORS), so they're probed
   * server-side via the orchestrator's /status aggregator.
   */
  kind: "http" | "redis" | "mcp";
  /** Health path for http endpoints (e.g. "/health"). */
  healthPath?: string;
  /**
   * monitorOnly endpoints (portal, redis) are shown in the monitor but are not
   * used as an API base by the app's HTTP services.
   */
  monitorOnly: boolean;
  /** Short description shown under the label. */
  hint: string;
}

/** Persisted override: a user-set scheme/host/port for one endpoint. */
type Override = Pick<EndpointConfig, "scheme" | "host" | "port">;

const STORAGE_KEY = "payprobe.endpoints";

/** Parse an absolute base URL into scheme/host/port, else null (relative path). */
function parseBase(
  base: string,
  fallbackPort: number,
): { scheme: string; host: string; port: number } | null {
  if (!/^https?:\/\//.test(base)) return null;
  try {
    const u = new URL(base);
    const port = u.port
      ? Number(u.port)
      : u.protocol === "https:"
        ? 443
        : fallbackPort;
    return { scheme: u.protocol.replace(":", ""), host: u.hostname, port };
  } catch {
    return null;
  }
}

/**
 * Single source of truth for where the portal reaches its backend services,
 * and the canonical list of endpoints surfaced in the health monitor.
 *
 * Defaults come from `environment` (dev: absolute localhost URLs; prod: same-
 * origin relative paths). A user can override scheme/host/port per endpoint
 * from Settings → Endpoints; overrides persist in localStorage and take effect
 * immediately because every API service resolves its base from this service on
 * each request (no rebuild, no reload required).
 *
 * In production, an endpoint with no explicit override keeps its relative
 * environment base (so the nginx same-origin routing is preserved); only
 * endpoints the operator deliberately overrides switch to an absolute URL.
 */
@Injectable({ providedIn: "root" })
export class RuntimeConfigService {
  /** Built-in defaults, derived from the compiled environment + sensible ports. */
  private readonly defaults: EndpointConfig[] = this.buildDefaults();

  /** User overrides, keyed by endpoint. Empty until the operator customizes. */
  private readonly overrides = signal<Partial<Record<EndpointKey, Override>>>(
    this.load(),
  );

  /** The effective, merged endpoint list (defaults + overrides). */
  readonly endpoints = computed<EndpointConfig[]>(() => {
    const ov = this.overrides();
    return this.defaults.map((d) => {
      const o = ov[d.key];
      return o ? { ...d, ...o } : { ...d };
    });
  });

  /** True when the operator has customized at least one endpoint. */
  readonly isCustomized = computed(
    () => Object.keys(this.overrides()).length > 0,
  );

  // -- API bases used by the HTTP services -----------------------------------

  get scenarioApiBase(): string {
    return this.baseFor("scenario", environment.scenarioApiBase);
  }

  get runApiBase(): string {
    return this.baseFor("orchestrator", environment.runApiBase);
  }

  get authApiBase(): string {
    return this.baseFor("auth", environment.authApiBase);
  }

  /** Where the assistant *chat* is sent. An explicit "assistant" endpoint
   *  override (Settings → Endpoints) wins; otherwise the environment default —
   *  the standalone payprobe-assistant (:8400 dev, /api/assistant prod) since
   *  the 2026-07-07 cutover. Overriding to the scenario service (:8000) falls
   *  back to the deprecated in-process shim. */
  get assistantApiBase(): string {
    const o = this.endpoint("assistant");
    return this.hasOverride("assistant")
      ? this.baseUrl(o)
      : (environment.assistantApiBase ?? this.scenarioApiBase);
  }

  /** Advisory ML insight service (ADR-0005). Optional deployment — callers
   *  must degrade gracefully when it isn't reachable. */
  get insightApiBase(): string {
    return this.baseFor("insight", environment.insightApiBase);
  }

  /** Build an absolute base from a config's scheme/host/port. */
  baseUrl(cfg: EndpointConfig): string {
    return `${cfg.scheme}://${cfg.host}:${cfg.port}`;
  }

  /** The current endpoint config for one key. */
  endpoint(key: EndpointKey): EndpointConfig {
    return this.endpoints().find((e) => e.key === key)!;
  }

  /** True when this endpoint currently has a user override. */
  hasOverride(key: EndpointKey): boolean {
    return !!this.overrides()[key];
  }

  // -- mutations -------------------------------------------------------------

  /** Persist a new scheme/host/port for one endpoint (takes effect at once). */
  update(key: EndpointKey, value: Override): void {
    this.overrides.update((cur) => {
      const next = { ...cur, [key]: { ...value, port: Number(value.port) } };
      this.persist(next);
      return next;
    });
  }

  /** Apply a batch of overrides at once. */
  updateMany(values: Partial<Record<EndpointKey, Override>>): void {
    this.overrides.update((cur) => {
      const next: Partial<Record<EndpointKey, Override>> = { ...cur };
      for (const [k, v] of Object.entries(values)) {
        if (v) next[k as EndpointKey] = { ...v, port: Number(v.port) };
      }
      this.persist(next);
      return next;
    });
  }

  /** Drop the override for one endpoint, reverting it to the built-in default. */
  reset(key: EndpointKey): void {
    this.overrides.update((cur) => {
      const next = { ...cur };
      delete next[key];
      this.persist(next);
      return next;
    });
  }

  /** Drop all overrides. */
  resetAll(): void {
    this.overrides.set({});
    this.persist({});
  }

  // -- internals -------------------------------------------------------------

  /**
   * Resolve a service base. If the operator overrode this endpoint, return the
   * absolute URL built from their scheme/host/port. Otherwise return the
   * compiled environment value verbatim (preserves prod's relative same-origin
   * paths and dev's absolute localhost URLs).
   */
  private baseFor(key: EndpointKey, envDefault: string): string {
    const o = this.overrides()[key];
    if (o) return `${o.scheme}://${o.host}:${o.port}`;
    return envDefault;
  }

  private buildDefaults(): EndpointConfig[] {
    const portal =
      typeof window !== "undefined" && window.location
        ? {
            scheme: window.location.protocol.replace(":", ""),
            host: window.location.hostname || "localhost",
            port: Number(window.location.port) || 8080,
          }
        : { scheme: "http", host: "localhost", port: 8080 };

    const orch = parseBase(environment.runApiBase, 8100) ?? {
      scheme: "http",
      host: "localhost",
      port: 8100,
    };
    const scenario = parseBase(environment.scenarioApiBase, 8000) ?? {
      scheme: "http",
      host: "localhost",
      port: 8000,
    };
    const auth = parseBase(environment.authApiBase, 8300) ?? {
      scheme: "http",
      host: "localhost",
      port: 8300,
    };
    // The standalone payprobe-assistant container's own address (not the chat
    // routing target, which may point at the scenario-service shim). Probed at
    // /health so its status shows even before chat is switched over to it.
    const assistant = { scheme: "http", host: "localhost", port: 8400 };
    const insight = parseBase(environment.insightApiBase, 8500) ?? {
      scheme: "http",
      host: "localhost",
      port: 8500,
    };

    return [
      {
        key: "portal",
        label: "Portal",
        ...portal,
        kind: "http",
        monitorOnly: true,
        hint: "This web app",
      },
      {
        key: "orchestrator",
        label: "Orchestrator API",
        ...orch,
        kind: "http",
        healthPath: "/status",
        monitorOnly: false,
        hint: "Run orchestrator (load runs)",
      },
      {
        key: "scenario",
        label: "Scenario service",
        ...scenario,
        kind: "http",
        healthPath: "/health",
        monitorOnly: false,
        hint: "Scenarios, formats & registries",
      },
      {
        key: "auth",
        label: "Auth service",
        ...auth,
        kind: "http",
        healthPath: "/health",
        monitorOnly: false,
        hint: "JWT issuance & user store",
      },
      {
        key: "assistant",
        label: "Assistant",
        ...assistant,
        kind: "http",
        healthPath: "/health",
        monitorOnly: false,
        hint: "Config assistant & LLM gateway",
      },
      {
        key: "insight",
        label: "Insight service",
        ...insight,
        kind: "http",
        healthPath: "/health",
        monitorOnly: false,
        hint: "Advisory ML insights — categorize / explain / predict (ADR-0005)",
      },
      {
        key: "redis",
        label: "Redis (load bus)",
        scheme: "redis",
        host: "localhost",
        port: 6379,
        kind: "redis",
        monitorOnly: true,
        hint: "Event backbone & load coordination",
      },
      {
        key: "mcp",
        label: "MCP server",
        scheme: "http",
        host: "localhost",
        port: 8200,
        kind: "mcp",
        healthPath: "/mcp",
        monitorOnly: true,
        hint: "Scenario catalog & ops over MCP (Streamable HTTP — connect at /mcp)",
      },
    ];
  }

  private load(): Partial<Record<EndpointKey, Override>> {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw) as Partial<Record<EndpointKey, Override>>;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  private persist(value: Partial<Record<EndpointKey, Override>>): void {
    try {
      if (Object.keys(value).length === 0) localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } catch {
      /* storage unavailable — overrides stay in-memory for this session */
    }
  }
}
