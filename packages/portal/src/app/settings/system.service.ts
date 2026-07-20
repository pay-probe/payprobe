import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

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
  observability: Record<string, string>;
}

export interface StatusInfo {
  status: string;
  services: Record<
    string,
    { status: string; error?: string; detail?: unknown }
  >;
}

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

  status(): Observable<StatusInfo> {
    return this.http.get<StatusInfo>(`${this.base}/status`);
  }
}
