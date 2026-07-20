import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

export interface GroupMember {
  connection: string;
  weight: number;
}

export interface ParticipantGroup {
  id: string;
  name: string;
  description?: string;
  adapter_type?: string;
  members?: GroupMember[];
  selection?: { policy: string; key?: string | null };
  updated_at?: string | null;
}

/** Participant groups (fleets): several member connections under one logical
 *  name with a selection policy. Persisted in the scenario-service. */
@Injectable({ providedIn: "root" })
export class GroupsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get base(): string {
    return `${this.cfg.scenarioApiBase}/participant-groups`;
  }

  readonly groups = signal<ParticipantGroup[]>([]);

  reload(): void {
    this.http.get<ParticipantGroup[]>(this.base).subscribe({
      next: (l) => this.groups.set(l),
      error: () => this.groups.set([]),
    });
  }

  get(id: string): Observable<ParticipantGroup> {
    return this.http.get<ParticipantGroup>(
      `${this.base}/${encodeURIComponent(id)}`,
    );
  }

  create(draft: Partial<ParticipantGroup>): Observable<ParticipantGroup> {
    return this.http
      .post<ParticipantGroup>(this.base, draft)
      .pipe(tap(() => this.reload()));
  }

  save(
    id: string,
    draft: Partial<ParticipantGroup>,
  ): Observable<ParticipantGroup> {
    return this.http
      .put<ParticipantGroup>(`${this.base}/${encodeURIComponent(id)}`, draft)
      .pipe(tap(() => this.reload()));
  }

  remove(id: string): Observable<void> {
    return this.http
      .delete<void>(`${this.base}/${encodeURIComponent(id)}`)
      .pipe(tap(() => this.reload()));
  }
}
