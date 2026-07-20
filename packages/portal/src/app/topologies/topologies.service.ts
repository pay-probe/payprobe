import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, map, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { NetworkFlow } from "./network-flows.service";

export interface TopologyParticipant {
  flow_id: string;
  instances: number;
}

/** Read-model of a network flow for the map pages: name + participant list.
 *  (The legacy /topologies API was removed — ADR-0004; this is derived from
 *  /network-flows, whose `participant` nodes carry flow_id × instances.) */
export interface Topology {
  id: string;
  name: string;
  description?: string;
  participants?: TopologyParticipant[];
  updated_at?: string | null;
}

/** Readiness of a network-flow run (all started instances still bound). */
export interface TopologyRunHealth {
  total: number;
  live: number;
  ready: boolean;
}

/** One instance's bound listen endpoint within a run. */
export interface TopologyRunEndpoint {
  participant: string;
  flow_id: string;
  endpoint: string;
}

/** A live network-flow run (a set of started participant instances). */
export interface TopologyRun {
  id: string;
  topology_id: string;
  name: string;
  kind?: string;
  instances: number;
  health?: TopologyRunHealth;
  endpoints?: TopologyRunEndpoint[];
}

/** Legacy-shaped read/run facade over network flows, kept for the topology-map
 *  pages: `topologies()` lists network flows condensed to participant lists;
 *  runs go through the orchestrator's `/network-flows/{id}/start` +
 *  `/topology-runs`. Authoring lives in NetworkFlowsService (`/network-flows`). */
@Injectable({ providedIn: "root" })
export class TopologiesService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get defsBase(): string {
    return `${this.cfg.scenarioApiBase}/network-flows`;
  }
  private get runBase(): string {
    return `${this.cfg.runApiBase}`;
  }

  readonly topologies = signal<Topology[]>([]);
  readonly runs = signal<TopologyRun[]>([]);

  private condense(f: NetworkFlow): Topology {
    return {
      id: f.id,
      name: f.name,
      description: f.description,
      updated_at: f.updated_at,
      participants: (f.nodes ?? [])
        .filter((n) => n.kind === "participant")
        .map((n) => ({
          flow_id: n.config?.flow_id ?? "",
          instances: Number(n.config?.instances) || 1,
        })),
    };
  }

  reload(): void {
    this.http.get<NetworkFlow[]>(this.defsBase).subscribe({
      next: (l) => this.topologies.set(l.map((f) => this.condense(f))),
      error: () => this.topologies.set([]),
    });
    this.reloadRuns();
  }

  reloadRuns(): void {
    this.http.get<TopologyRun[]>(`${this.runBase}/topology-runs`).subscribe({
      next: (l) => this.runs.set(l),
      error: () => this.runs.set([]),
    });
  }

  get(id: string): Observable<Topology> {
    return this.http
      .get<NetworkFlow>(`${this.defsBase}/${encodeURIComponent(id)}`)
      .pipe(map((f) => this.condense(f)));
  }

  /** Which run (if any) is currently live for a network-flow id. */
  runFor(topologyId: string): TopologyRun | undefined {
    return this.runs().find((r) => r.topology_id === topologyId);
  }

  start(topologyId: string): Observable<unknown> {
    return this.http
      .post(
        `${this.runBase}/network-flows/${encodeURIComponent(topologyId)}/start`,
        {},
      )
      .pipe(tap(() => this.reloadRuns()));
  }

  stopRun(runId: string): Observable<void> {
    return this.http
      .delete<void>(
        `${this.runBase}/topology-runs/${encodeURIComponent(runId)}`,
      )
      .pipe(tap(() => this.reloadRuns()));
  }
}
