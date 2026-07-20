import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { TopologyRun } from "./topologies.service";

/** Config of a `participant` node — references a shared participant flow. */
export interface ParticipantNodeConfig {
  flow_id?: string;
  instances?: number;
  /** Optional explicit base listen port (overrides the connection's). */
  port?: number | null;
}

/** Config of a `scenario` node — a traffic initiator. */
export interface ScenarioNodeConfig {
  scenario_id?: string;
  environment?: string | null;
  autostart?: boolean;
}

/** Config of a `simulator` node — a saved simulator brought up with the network. */
export interface SimulatorNodeConfig {
  simulator_id?: string;
}

/** Config of a `group` node — a participant group as a passive wiring target. */
export interface GroupNodeConfig {
  group_id?: string;
}

export type NetworkNodeKind =
  | "participant"
  | "scenario"
  | "simulator"
  | "group";

/** A node on the network canvas (reuses the scenario Step shape server-side). */
export interface NetworkNode {
  id: string;
  kind: NetworkNodeKind;
  config: ParticipantNodeConfig &
    ScenarioNodeConfig &
    SimulatorNodeConfig &
    GroupNodeConfig;
}

/** Wiring: source sends traffic to target ⇒ target starts first. */
export interface NetworkEdge {
  source: string;
  source_port?: string;
  target: string;
}

export interface NetworkFlow {
  id: string;
  name: string;
  description?: string;
  nodes: NetworkNode[];
  edges: NetworkEdge[];
  layout?: Record<string, { x: number; y: number }>;
  updated_at?: string | null;
}

export interface NetworkFlowIssue {
  severity: "error" | "warning";
  step_id?: string | null;
  message: string;
}

/** Resolved launch plan (start order) from the scenario-service. */
export interface NetworkFlowPlan {
  participants: {
    node_id: string;
    flow_id: string;
    instances: number;
    port?: number | null;
  }[];
  initiators: {
    node_id: string;
    scenario_id: string;
    environment?: string | null;
    autostart: boolean;
  }[];
  simulators: { node_id: string; simulator_id: string }[];
  warnings: string[];
}

/**
 * Network flows (ADR-0004) — canvas-authored composites of participant flows
 * and initiator scenarios, started/stopped as one simulated network.
 * Definitions live in the scenario-service (`/network-flows`); runs share the
 * orchestrator's topology-run machinery (kind `network_flow`).
 */
@Injectable({ providedIn: "root" })
export class NetworkFlowsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get defsBase(): string {
    return `${this.cfg.scenarioApiBase}/network-flows`;
  }
  private get runBase(): string {
    return `${this.cfg.runApiBase}`;
  }

  readonly flows = signal<NetworkFlow[]>([]);
  readonly runs = signal<TopologyRun[]>([]);

  reload(): void {
    this.http.get<NetworkFlow[]>(this.defsBase).subscribe({
      next: (l) => this.flows.set(l),
      error: () => this.flows.set([]),
    });
    this.reloadRuns();
  }

  reloadRuns(): void {
    this.http
      .get<(TopologyRun & { kind?: string })[]>(`${this.runBase}/topology-runs`)
      .subscribe({
        next: (l) => this.runs.set(l.filter((r) => r.kind === "network_flow")),
        error: () => this.runs.set([]),
      });
  }

  get(id: string): Observable<NetworkFlow> {
    return this.http.get<NetworkFlow>(
      `${this.defsBase}/${encodeURIComponent(id)}`,
    );
  }

  plan(id: string): Observable<NetworkFlowPlan> {
    return this.http.get<NetworkFlowPlan>(
      `${this.defsBase}/${encodeURIComponent(id)}/plan`,
    );
  }

  validate(
    draft: Partial<NetworkFlow>,
  ): Observable<{ valid: boolean; issues: NetworkFlowIssue[] }> {
    return this.http.post<{ valid: boolean; issues: NetworkFlowIssue[] }>(
      `${this.defsBase}/validate`,
      draft,
    );
  }

  /** Derive wiring from what the flows already declare (their outbound
   *  connections/groups resolved against the other nodes' listeners). */
  inferWiring(
    draft: Partial<NetworkFlow>,
  ): Observable<{ edges: NetworkEdge[]; notes: string[] }> {
    return this.http.post<{ edges: NetworkEdge[]; notes: string[] }>(
      `${this.defsBase}/infer-wiring`,
      draft,
    );
  }

  create(draft: Partial<NetworkFlow>): Observable<NetworkFlow> {
    return this.http
      .post<NetworkFlow>(this.defsBase, draft)
      .pipe(tap(() => this.reload()));
  }

  save(id: string, draft: Partial<NetworkFlow>): Observable<NetworkFlow> {
    return this.http
      .put<NetworkFlow>(`${this.defsBase}/${encodeURIComponent(id)}`, draft)
      .pipe(tap(() => this.reload()));
  }

  remove(id: string): Observable<void> {
    return this.http
      .delete<void>(`${this.defsBase}/${encodeURIComponent(id)}`)
      .pipe(tap(() => this.reload()));
  }

  /** Which run (if any) is currently live for a network-flow id. */
  runFor(id: string): TopologyRun | undefined {
    return this.runs().find((r) => r.topology_id === id);
  }

  start(id: string): Observable<unknown> {
    return this.http
      .post(`${this.runBase}/network-flows/${encodeURIComponent(id)}/start`, {})
      .pipe(tap(() => this.reloadRuns()));
  }

  /** One-click onboarding: build the bundled showcase network (idempotent),
   *  optionally starting it. Reloads the list afterwards. */
  installShowcase(
    start = false,
  ): Observable<{ network_flow_id: string; started: boolean }> {
    return this.http
      .post<{
        network_flow_id: string;
        started: boolean;
      }>(`${this.runBase}/showcase/install`, { start })
      .pipe(tap(() => this.reload()));
  }

  stopRun(runId: string): Observable<void> {
    return this.http
      .delete<void>(
        `${this.runBase}/topology-runs/${encodeURIComponent(runId)}`,
      )
      .pipe(tap(() => this.reloadRuns()));
  }
}
