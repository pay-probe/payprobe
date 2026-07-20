import { HttpClient } from "@angular/common/http";
import { Injectable, computed, inject, signal } from "@angular/core";
import { Observable, switchMap, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import {
  Connection,
  fromAdapterConfig,
  toAdapterConfig,
} from "./connection.models";

/** A stored connection doc: the worker-shaped adapter config plus its name. */
type StoredConnection = Record<string, unknown> & { name: string };

/** Server-persisted adapter connections (scenario-service `/connections`).
 *
 * Stored in the scenario-service alongside message formats, tables and
 * scenarios — not the browser. `connections` is a local cache kept in sync by
 * `reload()`; mutations hit the API then refresh the cache.
 */
@Injectable({ providedIn: "root" })
export class ConnectionsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.scenarioApiBase;
  }

  readonly connections = signal<Connection[]>([]);
  readonly loading = signal(false);

  /** The worker-shaped `{ "adapters": { ... } }` block for all connections. */
  readonly adaptersJson = computed(() => {
    const map: Record<string, unknown> = {};
    for (const c of this.connections()) {
      if (c.name) map[c.name] = toAdapterConfig(c);
    }
    return JSON.stringify({ adapters: map }, null, 2);
  });

  reload(): void {
    this.loading.set(true);
    this.http.get<StoredConnection[]>(`${this.base}/connections`).subscribe({
      next: (list) => {
        // stored docs are worker-shaped; parse back into the editable form model
        this.connections.set(list.map((d) => fromAdapterConfig(d.name, d)));
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** Upsert a connection. A rename drops the old record, then PUTs the new.
   *  The body is the worker-shaped adapter config (the canonical stored form)
   *  plus ``environment_overrides`` metadata (per-environment parameter
   *  overrides, kept out of the adapter config itself so the base never carries
   *  them — the orchestrator merges the active environment's override at run
   *  time). */
  save(conn: Connection, originalName?: string): Observable<StoredConnection> {
    const body = {
      ...toAdapterConfig(conn),
      environment_overrides: conn.environmentOverrides ?? {},
      // One port for both directions: `port` (emitted by toAdapterConfig) is the
      // dial port when outbound and the bind port when inbound. `listen_port` is
      // no longer written (the backend folds any legacy value into `port`).
      mode: conn.mode ?? "outbound",
      disabled: conn.disabled === true,
      default: conn.default === true,
    };
    const put = () =>
      this.http.put<StoredConnection>(
        `${this.base}/connections/${encodeURIComponent(conn.name)}`,
        body,
      );
    const op =
      originalName && originalName !== conn.name
        ? this.http
            .delete<void>(
              `${this.base}/connections/${encodeURIComponent(originalName)}`,
            )
            .pipe(switchMap(() => put()))
        : put();
    return op.pipe(tap(() => this.reload()));
  }

  remove(name: string): Observable<void> {
    return this.http
      .delete<void>(`${this.base}/connections/${encodeURIComponent(name)}`)
      .pipe(tap(() => this.reload()));
  }

  /** Toggle a connection's disabled flag (out of / back into group rotation).
   *  Returns null if the connection isn't cached. */
  setDisabled(
    name: string,
    disabled: boolean,
  ): Observable<StoredConnection> | null {
    const conn = this.connections().find((c) => c.name === name);
    if (!conn) return null;
    return this.save({ ...conn, disabled });
  }

  /** Set (or clear) a connection's parameter overrides for one environment and
   *  persist. Lets the Environments page edit the same overrides the
   *  Connections page owns (one source of truth). An empty/blank override map
   *  removes the environment's entry. Returns null if the connection isn't
   *  cached. */
  setEnvironmentOverride(
    connName: string,
    envName: string,
    overrides: Record<string, unknown>,
  ): Observable<StoredConnection> | null {
    const conn = this.connections().find((c) => c.name === connName);
    if (!conn) return null;
    const next = { ...(conn.environmentOverrides ?? {}) };
    if (overrides && Object.keys(overrides).length) next[envName] = overrides;
    else delete next[envName];
    return this.save({ ...conn, environmentOverrides: next });
  }

  /** Probe a connection live (opens the socket + signs on). Hits the
   *  orchestrator, which owns the worker adapter. Never persists. */
  test(
    conn: Connection,
  ): Observable<{ ok: boolean; latency_ms: number; error: string | null }> {
    return this.http.post<{
      ok: boolean;
      latency_ms: number;
      error: string | null;
    }>(`${this.cfg.runApiBase}/connections/test`, {
      config: toAdapterConfig(conn),
    });
  }

  nameExists(name: string, exclude?: string): boolean {
    return this.connections().some(
      (c) => c.name === name && c.name !== exclude,
    );
  }
}
