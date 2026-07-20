import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** A reference to something callable right now (see GET /playground/targets). */
export interface PlaygroundTargetRef {
  kind:
    | "connection"
    | "group"
    | "simulator"
    | "participant"
    | "raw"
    | "function"
    | "insight";
  id?: string;
  environment?: string | null;
  // raw targets only (parity with /hsm/command's hand-typed endpoint):
  host?: string;
  port?: number;
  adapter?: string;
  protocol?: string;
}

export interface PlaygroundConnection {
  name: string;
  adapter?: string;
  protocol?: string;
  mode?: string;
  host?: string;
  port?: number;
  default: boolean;
  disabled: boolean;
  environments: string[];
  /** samples.wire family this target draws samples from (server-resolved). */
  sample_family?: string | null;
}

export interface PlaygroundSimulator {
  id: string;
  label: string;
  protocol: string;
  port?: number;
  endpoint?: string;
  proxy: boolean;
  sample_family?: string | null;
}

export interface PlaygroundParticipant {
  id: string;
  flow_id: string;
  flow_name?: string;
  protocol: string;
  endpoint?: string;
  owner: string;
  fleet?: boolean;
  sample_family?: string | null;
}

export interface PlaygroundGroup {
  id?: string;
  name?: string;
  adapter?: string;
  members: number;
  sample_family?: string | null;
}

/** A ready-made example interaction — composer-shaped (action + payload). */
export interface PlaygroundSample {
  name: string;
  action: string;
  payload: Record<string, unknown>;
  notes?: string | null;
}

/** Samples per element family: `wire[protocol-or-adapter]` for wire targets,
 *  `functions.crypto` one runnable parameter set per operation. */
export interface PlaygroundSamples {
  wire: Record<string, PlaygroundSample[]>;
  functions: { crypto: PlaygroundSample[] };
}

export interface PlaygroundTargets {
  connections: PlaygroundConnection[];
  environments: string[];
  simulators: PlaygroundSimulator[];
  participants: PlaygroundParticipant[];
  groups: PlaygroundGroup[];
  functions: { crypto: string[] };
  /** The advisory insight service (ADR-0005) as a fixed target. */
  insight?: {
    actions: string[];
    base_url?: string;
    sample_family?: string | null;
  };
  raw: { adapters: string[] };
  samples?: PlaygroundSamples;
}

export interface PlaygroundExecuteRequest {
  target: PlaygroundTargetRef;
  action: string;
  payload: Record<string, unknown>;
  message_format_id?: string | null;
  label?: string | null;
}

/** Execute envelope — request/response echoes come back MASKED (invariant #8). */
export interface PlaygroundResult {
  ok: boolean;
  status: string;
  target: Record<string, unknown>;
  request: unknown;
  response: unknown;
  error: string | null;
  duration_ms: number;
  history_seq: number;
}

export interface PlaygroundHistoryRow {
  seq: number;
  ts: number;
  source: string;
  target: Record<string, unknown>;
  action: string;
  message_format_id?: string | null;
  label?: string | null;
  ok: boolean;
  status: string;
  error: string | null;
  duration_ms: number;
  request: unknown;
  response: unknown;
}

export interface PlaygroundSaveResult {
  scenario_id: string;
  name: string;
  steps: number;
}

/** Typed client for the orchestrator's Playground surface (ADR-0007). */
@Injectable({ providedIn: "root" })
export class PlaygroundApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.runApiBase;
  }

  /** Everything addressable right now (read-only registry merge). */
  targets(): Observable<PlaygroundTargets> {
    return this.http.get<PlaygroundTargets>(`${this.base}/playground/targets`);
  }

  /** Fire one ad-hoc interaction by reference; never a 5xx for an unreachable
   *  target — that is a normal `{ok:false, error}` result. */
  execute(req: PlaygroundExecuteRequest): Observable<PlaygroundResult> {
    return this.http.post<PlaygroundResult>(
      `${this.base}/playground/execute`,
      req,
    );
  }

  /** This caller's recent interactions (masked), newest first. */
  history(): Observable<{ count: number; rows: PlaygroundHistoryRow[] }> {
    return this.http.get<{ count: number; rows: PlaygroundHistoryRow[] }>(
      `${this.base}/playground/history`,
    );
  }

  clearHistory(): Observable<void> {
    return this.http.delete<void>(`${this.base}/playground/history`);
  }

  /** Re-fire one history entry verbatim (the stored raw request). */
  refire(seq: number): Observable<PlaygroundResult> {
    return this.http.post<PlaygroundResult>(
      `${this.base}/playground/history/${seq}/refire`,
      {},
    );
  }

  /** Promote history entries into a durable scenario (the on-ramp). */
  saveAsScenario(body: {
    name: string;
    seqs?: number[];
    project_id?: string | null;
  }): Observable<PlaygroundSaveResult> {
    return this.http.post<PlaygroundSaveResult>(
      `${this.base}/playground/save-as-scenario`,
      body,
    );
  }
}
