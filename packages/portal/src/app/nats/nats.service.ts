import { HttpClient, HttpParams } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** A registered NATS server / cluster (scenario-service registry). */
export interface NatsServer {
  key: string;
  name: string;
  description?: string;
  servers: string[];
  monitoring?: string[];
  auth?: Record<string, unknown>;
}

export interface NatsServerDraft {
  name: string;
  description?: string;
  servers: string[];
  monitoring?: string[];
  auth?: Record<string, unknown>;
}

/** One node's health, from its monitoring port (/healthz, /varz, /jsz). */
export interface NatsNode {
  monitor: string;
  healthy: boolean;
  server_name?: string;
  version?: string;
  connections?: number;
  uptime?: string;
  cluster_name?: string;
  jetstream_enabled?: boolean;
  jetstream?: {
    streams?: number;
    consumers?: number;
    messages?: number;
    bytes?: number;
  };
  error?: string;
}

export interface NatsClusterHealth {
  servers: string[];
  nodes: NatsNode[];
  total: number;
  live: number;
  healthy: boolean;
  jetstream: boolean;
}

export interface NatsStream {
  name: string;
  subjects: string[];
  messages?: number;
  bytes?: number;
  consumers?: number;
}

export interface NatsConsumer {
  name?: string;
  durable?: string;
}

/**
 * NATS admin client (ADR-0006). Cluster health + JetStream stream/consumer
 * management live on the **orchestrator** (it holds the nats-py client and the
 * monitoring probes); the **server registry** (named clusters) lives on the
 * scenario-service, like Environments/Connections.
 *
 * A target cluster is addressed either by a registry name (`?server=`) or an
 * explicit comma-separated `?servers=` URL list.
 */
@Injectable({ providedIn: "root" })
export class NatsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  private get orch(): string {
    return `${this.cfg.runApiBase}/nats`;
  }
  private get registry(): string {
    return `${this.cfg.scenarioApiBase}/nats-servers`;
  }

  private target(server?: string, servers?: string): HttpParams {
    let p = new HttpParams();
    if (server) p = p.set("server", server);
    else if (servers) p = p.set("servers", servers);
    return p;
  }

  // -- server registry (scenario-service) ---------------------------------

  listServers(): Observable<NatsServer[]> {
    return this.http.get<NatsServer[]>(this.registry);
  }

  saveServer(name: string, doc: NatsServerDraft): Observable<NatsServer> {
    return this.http.put<NatsServer>(
      `${this.registry}/${encodeURIComponent(name)}`,
      doc,
    );
  }

  deleteServer(key: string): Observable<void> {
    return this.http.delete<void>(
      `${this.registry}/${encodeURIComponent(key)}`,
    );
  }

  // -- cluster health + JetStream admin (orchestrator) --------------------

  cluster(server?: string, servers?: string): Observable<NatsClusterHealth> {
    return this.http.get<NatsClusterHealth>(`${this.orch}/cluster`, {
      params: this.target(server, servers),
    });
  }

  streams(
    server?: string,
    servers?: string,
  ): Observable<{ streams: NatsStream[] }> {
    return this.http.get<{ streams: NatsStream[] }>(`${this.orch}/streams`, {
      params: this.target(server, servers),
    });
  }

  createStream(body: {
    name: string;
    subjects: string[];
    server?: string;
    servers?: string;
  }): Observable<unknown> {
    return this.http.post(`${this.orch}/streams`, body);
  }

  deleteStream(
    name: string,
    server?: string,
    servers?: string,
  ): Observable<unknown> {
    return this.http.delete(
      `${this.orch}/streams/${encodeURIComponent(name)}`,
      {
        params: this.target(server, servers),
      },
    );
  }

  consumers(
    stream: string,
    server?: string,
    servers?: string,
  ): Observable<{ stream: string; consumers: NatsConsumer[] }> {
    return this.http.get<{ stream: string; consumers: NatsConsumer[] }>(
      `${this.orch}/streams/${encodeURIComponent(stream)}/consumers`,
      { params: this.target(server, servers) },
    );
  }

  deleteConsumer(
    stream: string,
    durable: string,
    server?: string,
    servers?: string,
  ): Observable<unknown> {
    return this.http.delete(
      `${this.orch}/streams/${encodeURIComponent(stream)}/consumers/${encodeURIComponent(durable)}`,
      { params: this.target(server, servers) },
    );
  }
}
