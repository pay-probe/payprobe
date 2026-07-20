import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** A saved participant flow (definition). Nodes reuse the scenario Step shape;
 *  entry is a `trigger` node, terminals are `reply` nodes. */
export interface ParticipantFlow {
  id: string;
  name: string;
  description?: string;
  trigger?: { connection?: string };
  nodes?: unknown[];
  edges?: unknown[];
  variables?: Record<string, unknown>;
  updated_at?: string | null;
}

/** A participant flow currently listening (running instance). */
export interface RunningParticipant {
  id: string;
  flow_id: string;
  protocol: string;
  host?: string;
  port: number | null;
  /** Bound listen endpoint (host:port), or null until the socket is up. */
  endpoint?: string | null;
  /** Owner of this listener: a topology run id, or "standalone". */
  owner?: string;
  topology_run?: string | null;
  received: number;
}

/** Adapter family of a hop / call — drives how it renders in the trace. */
export type TraceKind =
  | "tcp"
  | "http"
  | "hsm"
  | "grpc"
  | "db"
  | "group"
  | string;

/** One downstream call a flow made while handling a transaction. */
export interface TraceCall {
  target?: string;
  action?: string;
  kind?: TraceKind;
  ok?: boolean;
  error?: string | null;
  duration_ms?: number | null;
  request?: unknown;
  response?: unknown;
}

/** One hop of a transaction: a flow's handling of the message + its calls.
 *  ``request`` is adapter-shaped: ISO carries {mti, de}; HTTP carries
 *  {method, path, query, body}. */
export interface TraceHop {
  participant?: string;
  flow_id?: string;
  flow_name?: string | null;
  kind?: TraceKind;
  port?: number;
  ts?: number;
  mti?: string;
  request?: {
    mti?: string;
    de?: Record<string, string>;
    method?: string;
    path?: string;
    query?: Record<string, string>;
    body?: unknown;
  };
  response_code?: string | null;
  status?: string;
  error?: string | null;
  ok?: boolean;
  calls?: TraceCall[];
}

/** A recent transaction (one correlation id) for the Network Trace index. */
export interface TxnSummary {
  correlation_id: string;
  ts: number;
  mti?: string;
  kind?: TraceKind;
  flows: string[];
  hops: number;
  ok: boolean;
}

/** Trace-capture state (Network Trace pause/resume control). */
export interface CaptureState {
  capturing: boolean;
  participants: number;
  buffered?: number;
}

/** Participant flows: definitions live in the scenario-service
 *  (`/participant-flows`); running listeners are managed by the orchestrator
 *  (`/participants`). */
@Injectable({ providedIn: "root" })
export class ParticipantsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get flowsBase(): string {
    return `${this.cfg.scenarioApiBase}/participant-flows`;
  }
  private get runBase(): string {
    return `${this.cfg.runApiBase}/participants`;
  }

  readonly flows = signal<ParticipantFlow[]>([]);
  readonly running = signal<RunningParticipant[]>([]);
  readonly loading = signal(false);

  reloadFlows(): void {
    this.loading.set(true);
    this.http.get<ParticipantFlow[]>(this.flowsBase).subscribe({
      next: (l) => {
        this.flows.set(l);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  reloadRunning(): void {
    this.http.get<RunningParticipant[]>(this.runBase).subscribe({
      next: (l) => this.running.set(l),
      error: () => this.running.set([]),
    });
  }

  get(id: string): Observable<ParticipantFlow> {
    return this.http.get<ParticipantFlow>(
      `${this.flowsBase}/${encodeURIComponent(id)}`,
    );
  }

  create(draft: Partial<ParticipantFlow>): Observable<ParticipantFlow> {
    return this.http
      .post<ParticipantFlow>(this.flowsBase, draft)
      .pipe(tap(() => this.reloadFlows()));
  }

  save(
    id: string,
    draft: Partial<ParticipantFlow>,
  ): Observable<ParticipantFlow> {
    return this.http
      .put<ParticipantFlow>(
        `${this.flowsBase}/${encodeURIComponent(id)}`,
        draft,
      )
      .pipe(tap(() => this.reloadFlows()));
  }

  remove(id: string): Observable<void> {
    return this.http
      .delete<void>(`${this.flowsBase}/${encodeURIComponent(id)}`)
      .pipe(tap(() => this.reloadFlows()));
  }

  /** Save As / rename: clone a flow's graph into a NEW flow (fresh unique id)
   *  under ``name``. ``replace`` makes it a rename — repoints every network
   *  flow at the copy and deletes the source. Returns the new flow + what
   *  changed. */
  duplicate(
    id: string,
    name: string,
    replace = false,
  ): Observable<{
    flow: ParticipantFlow;
    repointed_network_flows: string[];
    deleted_source: boolean;
  }> {
    return this.http
      .post<{
        flow: ParticipantFlow;
        repointed_network_flows: string[];
        deleted_source: boolean;
      }>(`${this.flowsBase}/${encodeURIComponent(id)}/duplicate`, {
        name,
        replace,
      })
      .pipe(tap(() => this.reloadFlows()));
  }

  /** Which running instance (if any) is serving this flow id. */
  runningFor(flowId: string): RunningParticipant | undefined {
    return this.running().find((r) => r.flow_id === flowId);
  }

  start(flowId: string): Observable<RunningParticipant> {
    return this.http
      .post<RunningParticipant>(`${this.runBase}/start`, { flow_id: flowId })
      .pipe(tap(() => this.reloadRunning()));
  }

  stop(pid: string): Observable<void> {
    return this.http
      .delete<void>(`${this.runBase}/${encodeURIComponent(pid)}`)
      .pipe(tap(() => this.reloadRunning()));
  }

  // -- network trace -------------------------------------------------------

  /** Recent transactions across all listeners (Network Trace index). ``q``
   *  filters by correlation id / STAN substring; ``flow`` keeps only
   *  transactions that passed through that participant flow. */
  recentTraces(
    q?: string,
    flow?: string,
  ): Observable<{ transactions: TxnSummary[] }> {
    const params: string[] = [];
    if (q) params.push(`q=${encodeURIComponent(q)}`);
    if (flow) params.push(`flow=${encodeURIComponent(flow)}`);
    const url =
      `${this.runBase}/traces` + (params.length ? "?" + params.join("&") : "");
    return this.http.get<{ transactions: TxnSummary[] }>(url);
  }

  /** Full cross-hop waterfall for one transaction. */
  trace(cid: string): Observable<{ correlation_id: string; hops: TraceHop[] }> {
    return this.http.get<{ correlation_id: string; hops: TraceHop[] }>(
      `${this.runBase}/trace/${encodeURIComponent(cid)}`,
    );
  }

  /** Current capture state (capturing? + buffered hop count). */
  captureState(): Observable<CaptureState> {
    return this.http.get<CaptureState>(`${this.runBase}/capture`);
  }

  /** Pause/resume trace recording across all listeners (listeners keep serving). */
  setCapture(enabled: boolean): Observable<CaptureState> {
    return this.http.post<CaptureState>(`${this.runBase}/capture`, { enabled });
  }

  /** Empty every listener's trace buffer. */
  clearTraces(): Observable<void> {
    return this.http.delete<void>(`${this.runBase}/traces`);
  }
}
