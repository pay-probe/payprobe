import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, switchMap, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** A row in the environments list (merged: bundled files + editable registry). */
export interface EnvironmentSummary {
  name: string;
  label: string;
  description?: string;
  /** "bundled" = shipped JSON file (read-only default); "registry" = editable. */
  source: "bundled" | "registry";
}

/** A full environment config doc (worker-shaped) for view/clone/edit. */
export interface EnvironmentConfig {
  name: string;
  description?: string;
  adapters?: Record<string, unknown>;
  [k: string]: unknown;
}

/** Run environments (adapter-target profiles: mock / staging / prod).
 *
 * The **list** and **full config** come from the orchestrator, which merges the
 * editable registry (scenario-service) with the bundled JSON files — so the
 * shipped environments always show up. **Writes** go to the scenario-service
 * registry; editing a bundled environment creates an editable copy under the
 * same name.
 */
@Injectable({ providedIn: "root" })
export class EnvironmentsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  private get orch(): string {
    return `${this.cfg.runApiBase}/environments`;
  }
  private get registry(): string {
    return `${this.cfg.scenarioApiBase}/environments`;
  }

  readonly environments = signal<EnvironmentSummary[]>([]);
  readonly loading = signal(false);
  /** Names of global variables, for `${vars.*}` autocomplete in the editor. */
  readonly varKeys = signal<string[]>([]);

  reload(): void {
    this.loading.set(true);
    this.http.get<EnvironmentSummary[]>(this.orch).subscribe({
      next: (l) => {
        this.environments.set(l);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** Load global-variable names so the editor can suggest `${vars.NAME}`. */
  loadVarKeys(): void {
    this.http
      .get<{
        variables?: Record<string, unknown>;
      }>(`${this.cfg.scenarioApiBase}/variables/global`)
      .subscribe({
        next: (r) => this.varKeys.set(Object.keys(r?.variables ?? {})),
        error: () => this.varKeys.set([]),
      });
  }

  /** Full config of an environment (registry first, else bundled). */
  getConfig(name: string): Observable<EnvironmentConfig> {
    return this.http.get<EnvironmentConfig>(
      `${this.orch}/${encodeURIComponent(name)}`,
    );
  }

  /** Upsert into the editable registry. A rename deletes the old key first. */
  save(
    name: string,
    doc: Record<string, unknown>,
    originalName?: string,
  ): Observable<unknown> {
    const put = () =>
      this.http.put(`${this.registry}/${encodeURIComponent(name)}`, doc);
    const isRegistryRename =
      originalName &&
      originalName !== name &&
      this.environments().some(
        (e) => e.name === originalName && e.source === "registry",
      );
    const op = isRegistryRename
      ? this.http
          .delete<void>(`${this.registry}/${encodeURIComponent(originalName!)}`)
          .pipe(switchMap(() => put()))
      : put();
    return op.pipe(tap(() => this.reload()));
  }

  /** Delete from the registry (bundled files can't be deleted here). */
  remove(name: string): Observable<void> {
    return this.http
      .delete<void>(`${this.registry}/${encodeURIComponent(name)}`)
      .pipe(tap(() => this.reload()));
  }
}
