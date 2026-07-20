import { Injectable, inject } from "@angular/core";

import { EndpointConfig, RuntimeConfigService } from "./runtime-config.service";

export type ProbeStatus = "online" | "degraded" | "offline" | "unknown";

export interface ProbeResult {
  status: ProbeStatus;
  /** Round-trip latency in ms, when measurable. */
  latencyMs?: number;
  /** Short human detail (e.g. "200 OK", "timeout", "unreachable"). */
  detail: string;
}

const DEFAULT_TIMEOUT_MS = 4000;

/**
 * Browser-side reachability probe for the configured endpoints.
 *
 * HTTP endpoints (portal, orchestrator, scenario, auth) are hit directly at
 * their health path and timed. Redis can't be reached from a browser, so its
 * status is read from the orchestrator's /status aggregator, which pings Redis
 * server-side. Probes never throw — every failure becomes an offline/unknown
 * result so the monitor degrades gracefully.
 */
@Injectable({ providedIn: "root" })
export class EndpointHealthService {
  private readonly cfg = inject(RuntimeConfigService);

  /** Probe a single endpoint. */
  async probe(ep: EndpointConfig): Promise<ProbeResult> {
    if (ep.kind === "redis") return this.probeViaOrchestrator("redis");
    if (ep.kind === "mcp") return this.probeViaOrchestrator("mcp-server");
    return this.probeHttp(ep);
  }

  /** Probe every configured endpoint in parallel. */
  async probeAll(): Promise<Map<string, ProbeResult>> {
    const eps = this.cfg.endpoints();
    const entries = await Promise.all(
      eps.map(async (ep) => [ep.key, await this.probe(ep)] as const),
    );
    return new Map(entries);
  }

  private async probeHttp(ep: EndpointConfig): Promise<ProbeResult> {
    const url = `${this.cfg.baseUrl(ep)}${ep.healthPath ?? "/"}`;
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        method: "GET",
        signal: controller.signal,
        // Health endpoints are unauthenticated and same-or-cross origin; we only
        // care that the service answers, not about reading credentialed data.
        cache: "no-store",
      });
      const latencyMs = Math.round(performance.now() - started);
      if (res.ok)
        return { status: "online", latencyMs, detail: `${res.status} OK` };
      return {
        status: "degraded",
        latencyMs,
        detail: `HTTP ${res.status}`,
      };
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      return {
        status: "offline",
        detail: aborted ? "timeout" : "unreachable",
      };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Health for endpoints a browser can't reach directly (Redis over TCP, the
   * MCP server's Streamable HTTP stream behind CORS). The orchestrator probes them
   * server-side and reports them under the given key in its /status payload.
   */
  private async probeViaOrchestrator(serviceKey: string): Promise<ProbeResult> {
    const url = `${this.cfg.runApiBase}/status`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!res.ok) return { status: "unknown", detail: "orchestrator down" };
      const body = (await res.json()) as {
        services?: Record<string, { status?: string; detail?: string }>;
      };
      const svc = body.services?.[serviceKey];
      if (!svc) return { status: "unknown", detail: "not reported" };
      if (svc.status === "ok")
        return { status: "online", detail: svc.detail ?? "reachable" };
      if (svc.status === "disabled")
        return { status: "unknown", detail: svc.detail ?? "not configured" };
      return { status: "offline", detail: svc.detail ?? "no reply" };
    } catch {
      return { status: "unknown", detail: "orchestrator unreachable" };
    } finally {
      clearTimeout(timer);
    }
  }
}
