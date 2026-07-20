import { Injectable, inject, signal } from "@angular/core";

import {
  DiagnosticsReport,
  RunApiService,
} from "../run-monitor/run-api.service";

export interface DiagVerdict {
  at: Date;
  /** ok | degraded | failing */
  status: string;
  fails: number;
  warns: number;
}

/**
 * Root-held diagnostics state so the doctor's last report SURVIVES NAVIGATION
 * instead of evaporating on route change, and the page can auto-run on first
 * visit. Also keeps a short verdict history — "was it failing an hour ago
 * too?" is half of every triage.
 */
@Injectable({ providedIn: "root" })
export class DiagnosticsStateService {
  private readonly api = inject(RunApiService);

  readonly report = signal<DiagnosticsReport | null>(null);
  readonly ranAt = signal<Date | null>(null);
  readonly running = signal(false);
  readonly environment = signal("");
  /** Last dozen verdicts, oldest first. */
  readonly history = signal<DiagVerdict[]>([]);

  run(): void {
    if (this.running()) return;
    this.running.set(true);
    this.api
      .diagnostics({ environment: this.environment() || undefined })
      .subscribe({
        next: (rep) => {
          this.report.set(rep);
          this.ranAt.set(new Date());
          this.running.set(false);
          this.history.update((h) =>
            [
              ...h,
              {
                at: new Date(),
                status: rep.status,
                fails: rep.summary?.["fail"] ?? 0,
                warns: rep.summary?.["warn"] ?? 0,
              },
            ].slice(-12),
          );
        },
        error: () => this.running.set(false),
      });
  }
}
