import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable, catchError, map, of } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

export interface SystemInfo {
  environment: string;
  version: string;
  security: {
    auth_enforced: boolean;
    code_sandbox_mode: string;
    code_network_isolation_active: boolean;
    unauth_code_execution_allowed: boolean;
  };
  runtime: {
    event_backbone: string;
    redis_configured: boolean;
    run_store_durable: boolean;
    run_control: string;
    tracing_configured: boolean;
  };
  /** Absent from orchestrators that predate PAYPROBE_LISTEN_HOST. */
  network?: {
    /** Where a listener whose config names no host binds. */
    listen_host: string;
    /** Host to pre-fill when suggesting where to dial a PayProbe listener. */
    advertised_host: string;
  };
  observability: Record<string, string>;
}

export interface StatusInfo {
  status: string;
  services: Record<
    string,
    { status: string; error?: string; detail?: unknown }
  >;
}

/** The dial host suggested when the orchestrator does not say otherwise. */
export const DEFAULT_ADVERTISED_HOST = "127.0.0.1";

/** Reads the orchestrator's runtime posture (/system) and health (/status). */
@Injectable({ providedIn: "root" })
export class SystemService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.runApiBase;
  }

  system(): Observable<SystemInfo> {
    return this.http.get<SystemInfo>(`${this.base}/system`);
  }

  /** Host to pre-fill when suggesting where to dial one of PayProbe's own
   *  listeners (orchestrator `PAYPROBE_ADVERTISED_HOST`). Falls back to
   *  `DEFAULT_ADVERTISED_HOST` when /system is unreachable or does not say.
   *  Never throws. */
  advertisedHost(): Observable<string> {
    return this.system().pipe(
      map((s) => s.network?.advertised_host || DEFAULT_ADVERTISED_HOST),
      catchError(() => of(DEFAULT_ADVERTISED_HOST)),
    );
  }

  status(): Observable<StatusInfo> {
    return this.http.get<StatusInfo>(`${this.base}/status`);
  }
}
