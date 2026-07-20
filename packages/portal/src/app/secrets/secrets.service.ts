import { HttpClient } from "@angular/common/http";
import { Injectable, computed, inject, signal } from "@angular/core";

import { RuntimeConfigService } from "../shared/runtime-config.service";

export type SecretSource = "connection" | "variable" | "key";

export interface SecretEntry {
  source: SecretSource;
  owner: string;
  field: string;
  fingerprint: string;
  encrypted: boolean;
}

export interface SecretsStatus {
  encrypted_at_rest: boolean;
  algorithm: string | null;
}

interface SecretsResponse {
  status: SecretsStatus;
  entries: SecretEntry[];
}

/** Read-only Secrets Vault client (scenario-service `/secrets`).
 *
 * The API returns a masked inventory only — fingerprints + metadata, never
 * plaintext — so this service holds no secret material.
 */
@Injectable({ providedIn: "root" })
export class SecretsService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  private get base(): string {
    return this.cfg.scenarioApiBase;
  }

  readonly entries = signal<SecretEntry[]>([]);
  readonly status = signal<SecretsStatus | null>(null);
  readonly loading = signal(false);

  readonly bySource = computed(() => {
    const groups: Record<SecretSource, SecretEntry[]> = {
      connection: [],
      variable: [],
      key: [],
    };
    for (const e of this.entries()) groups[e.source].push(e);
    return groups;
  });

  reload(): void {
    this.loading.set(true);
    this.http.get<SecretsResponse>(`${this.base}/secrets`).subscribe({
      next: (r) => {
        this.entries.set(r.entries ?? []);
        this.status.set(r.status ?? null);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** The portal manager that owns (and can rotate) a given secret source. */
  rotateRoute(source: SecretSource): string {
    return source === "connection"
      ? "/connections"
      : source === "variable"
        ? "/variables"
        : "/test-data";
  }
}
