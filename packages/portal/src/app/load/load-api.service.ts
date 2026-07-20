import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** Fleet-wide rolled-up snapshot emitted as a `load.sample` event's data, and
 *  returned by GET /load-runs/{id}. Mirrors LoadStats.summary() on the server. */
export interface LoadSnapshot {
  generated?: number; // offered load produced by the pacer (= sent + throttled)
  sent: number; // transactions actually put on the wire
  received: number;
  errors: number;
  pending: number;
  live: number;
  dead: number;
  reconnects: number;
  elapsed_s: number;
  tps: number;
  target_tps: number;
  latency_ms: {
    min: number;
    mean: number;
    p50: number;
    p95: number;
    p99: number;
    max: number;
    samples: number;
  };
  status?: string;
  workers?: number;
  workers_reporting?: number;
  profile?: string;
  last_error?: string | null;
  notice?: string | null;
  // saturation / backpressure (summed across the fleet)
  in_flight?: number;
  max_in_flight?: number;
  peak_in_flight?: number;
  throttled?: number;
  saturation?: number; // 0..1
  // transactions launched but cancelled at end-of-run (drain window exceeded)
  aborted?: number;
  // drain phase: the send window is over and outstanding txns are finishing.
  draining?: boolean;
  drain_s?: number; // configured drain budget (s); <0 = wait for all, 0 = cut now
  // elapsed split into the send window and the drain (elapsed_s = their sum).
  send_elapsed_s?: number; // driving time (fleet-ready → send window closed)
  drain_elapsed_s?: number; // how long the drain ran / has been running (s)
}

export type LoadProfileType = "steady" | "ramp" | "spike" | "soak";

/** One entry of a weighted traffic mix (e.g. 80% payment / 15% refund). */
export interface MixEntry {
  scenario_id?: string;
  scenario?: Record<string, unknown>;
  weight?: number;
}

export interface LoadRunRequest {
  // transaction selectors
  scenario_ids?: string[];
  environment_name?: string;
  label?: string;
  heartbeat_action?: string;
  // weighted traffic mix (supersedes scenario_ids when set)
  mix?: MixEntry[];
  // provisioning pre-run (onboard terminals / register cards once before load)
  provision_scenario_id?: string;
  // profile
  type: LoadProfileType;
  duration_s: number;
  workers: number;
  target_tps?: number;
  start_tps?: number;
  end_tps?: number;
  ramp_s?: number;
  base_tps?: number;
  spike_tps?: number;
  spike_every_s?: number;
  spike_duration_s?: number;
  connections?: number;
  heartbeat_interval_s?: number;
  open_rate_per_s?: number;
  max_in_flight_per_worker?: number;
  // cap on outstanding (launched-but-unfinished) txns per worker.
  // 0 = cap at the concurrency limit (recommended); >0 = explicit; <0 = unbounded.
  max_backlog_per_worker?: number;
  // grace period (s) to let in-flight transactions finish at end-of-run.
  // >0 waits up to that long then aborts the rest; <0 waits for all; 0 = cut now.
  drain_s?: number;
}

/** Body for POST /load-runs/{id}/retune — adjust the live target rate. */
export interface RetuneRequest {
  target_tps?: number;
  base_tps?: number;
  spike_tps?: number;
  end_tps?: number;
}

export interface LoadRunResponse {
  run_id: string;
  status: string;
  profile: Record<string, unknown>;
}

/** Typed client for the orchestrator's load-test surface. */
@Injectable({ providedIn: "root" })
export class LoadApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.runApiBase;
  }

  /** Launch a load run; pair with RunMonitorService.stream(run_id) for live
   *  `load.sample` snapshots. */
  start(req: LoadRunRequest): Observable<LoadRunResponse> {
    return this.http.post<LoadRunResponse>(`${this.base}/load-runs`, req);
  }

  /** Latest merged snapshot (or persisted summary once finished). */
  get(runId: string): Observable<LoadSnapshot> {
    return this.http.get<LoadSnapshot>(`${this.base}/load-runs/${runId}`);
  }

  stop(runId: string): Observable<{ run_id: string; status: string }> {
    return this.http.post<{ run_id: string; status: string }>(
      `${this.base}/load-runs/${runId}/stop`,
      {},
    );
  }

  /** Hot re-tune a running run's target rate without restarting it. */
  retune(
    runId: string,
    params: RetuneRequest,
  ): Observable<{ run_id: string; status: string; params: RetuneRequest }> {
    return this.http.post<{
      run_id: string;
      status: string;
      params: RetuneRequest;
    }>(`${this.base}/load-runs/${runId}/retune`, params);
  }

  /** Diagnose a run (load or functional): taxonomy → likely cause + fix. */
  diagnose(runId: string): Observable<Diagnosis> {
    return this.http.get<Diagnosis>(`${this.base}/runs/${runId}/diagnose`);
  }

  /** Run-vs-run load comparison: latency percentiles, error-rate, TPS, TPS
   *  stability and a per-category error breakdown. Omit `to` to diff against
   *  the previous like-for-like load run. */
  compare(runId: string, to?: string): Observable<LoadCompare> {
    const url =
      `${this.base}/load-runs/${encodeURIComponent(runId)}/compare` +
      (to ? `?to=${encodeURIComponent(to)}` : "");
    return this.http.get<LoadCompare>(url);
  }

  /** Load-run history (newest first) for the reopen list. */
  list(): Observable<LoadRunListItem[]> {
    return this.http.get<LoadRunListItem[]>(`${this.base}/load-runs`);
  }

  /** Close out load runs stuck in "running" with no live owner (e.g. after a
   *  service restart). Returns the reconciled run ids. */
  reconcile(): Observable<{ reconciled: string[]; count: number }> {
    return this.http.post<{ reconciled: string[]; count: number }>(
      `${this.base}/load-runs/reconcile`,
      {},
    );
  }

  /** Live load-worker fleet (workers currently holding a shard). */
  workers(): Observable<LoadWorker[]> {
    return this.http.get<LoadWorker[]>(`${this.base}/load-workers`);
  }

  /** Drain one worker — it finishes its shard and exits (no process kill). */
  stopWorker(
    workerId: string,
  ): Observable<{ worker_id: string; status: string }> {
    return this.http.post<{ worker_id: string; status: string }>(
      `${this.base}/load-workers/${encodeURIComponent(workerId)}/stop`,
      {},
    );
  }

  /** Drain the whole fleet. */
  stopAllWorkers(): Observable<{ status: string; count: number }> {
    return this.http.post<{ status: string; count: number }>(
      `${this.base}/load-workers/stop-all`,
      {},
    );
  }

  /** Worker-provisioner capabilities — drives whether the portal shows a live
   *  scale control (dynamic path) or just the manual command (custom path). */
  provisioner(): Observable<ProvisionerInfo> {
    return this.http.get<ProvisionerInfo>(
      `${this.base}/load-workers/provisioner`,
    );
  }

  /** Scale a live run's worker fleet to `desired` (dynamic path). */
  scale(runId: string, desired: number): Observable<ScaleResult> {
    return this.http.post<ScaleResult>(
      `${this.base}/load-runs/${encodeURIComponent(runId)}/scale`,
      { desired },
    );
  }
}

/** GET /load-workers/provisioner — how the fleet can be scaled here. */
export interface ProvisionerInfo {
  kind: "local" | "compose" | "none" | string;
  available: boolean;
  detail?: string;
  manual_command_template?: string;
}

/** POST /load-runs/{id}/scale result. */
export interface ScaleResult {
  run_id?: string;
  kind?: string;
  available?: boolean;
  requested?: number;
  running?: number;
  notice?: string;
}

/** One live load-worker, as reported by GET /load-workers. */
export interface LoadWorker {
  worker_id: string;
  run_id?: string;
  host?: string;
  pid?: number;
  shard_index?: number;
  shard_count?: number;
  type?: string;
  status?: string;
  started_at?: number;
  tps?: number;
  sent?: number;
  received?: number;
  errors?: number;
  connections?: number;
  /** Resident memory (bytes) at the last heartbeat — soak/leak watch. */
  rss_bytes?: number;
  draining?: boolean;
}

/** Compact headline metrics shown on a recent-run card. */
export interface LoadRunMetrics {
  profile?: string | null;
  tps?: number | null;
  target_tps?: number | null;
  p95?: number | null;
  errors?: number | null;
  sent?: number | null;
  received?: number | null;
  /** Completed-transaction success rate (%), or null if nothing completed. */
  success_rate?: number | null;
  duration_s?: number | null;
}

export interface LoadRunListItem {
  run_id: string;
  label: string;
  status: string;
  environment?: string;
  created_at?: string;
  metrics?: LoadRunMetrics;
}

export interface DiagnosisFinding {
  code: string;
  severity: "error" | "warn" | "info";
  title: string;
  cause: string;
  suggestion: string;
  evidence?: string;
}

export interface Diagnosis {
  headline: string;
  ok: boolean;
  findings: DiagnosisFinding[];
}

/** One metric's run-vs-run movement (GET /load-runs/{id}/compare → deltas.*). */
export interface LoadDelta {
  metric: string;
  current: number;
  base: number | null;
  delta: number | null;
  pct: number | null;
  /** "up" | "down" | "flat" | "new" */
  direction: string;
  regressed: boolean;
  improved: boolean;
}

/** One error category's count in each run, with the change. */
export interface LoadErrorCategory {
  category: string;
  current: number;
  base: number;
  delta: number;
}

/** GET /load-runs/{id}/compare — full run-vs-run load comparison. */
export interface LoadCompare {
  run_id: string;
  base_run_id: string | null;
  base_found: boolean;
  current: Record<string, number>;
  base: Record<string, number> | null;
  deltas: {
    latency_p50: LoadDelta;
    latency_p95: LoadDelta;
    latency_p99: LoadDelta;
    error_rate: LoadDelta;
    tps: LoadDelta;
    tps_cv: LoadDelta;
  };
  error_categories: LoadErrorCategory[];
  summary: { regressions: number; improvements: number };
}
