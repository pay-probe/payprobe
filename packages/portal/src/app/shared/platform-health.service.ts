import { Injectable, inject, signal } from "@angular/core";

import { SystemService } from "../settings/system.service";

/**
 * Background pulse for the topbar health dot — a light /status poll every
 * 60s so platform problems find the user instead of waiting to be found.
 * Replaces the old hardcoded always-green dot, which claimed "All systems
 * operational" without ever asking. Deliberately /status (a cheap services
 * map), not the full diagnostics pass.
 */
@Injectable({ providedIn: "root" })
export class PlatformHealthService {
  private readonly system = inject(SystemService);

  readonly tone = signal<"ok" | "warn" | "bad" | "unknown">("unknown");
  readonly summary = signal("Checking platform health…");
  private timer: ReturnType<typeof setInterval> | null = null;

  /** Idempotent — the topbar calls it once; the poll runs app-wide. */
  start(): void {
    if (this.timer) return;
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 60000);
  }

  refresh(): void {
    this.system.status().subscribe({
      next: (info) => {
        const entries = Object.entries(info.services ?? {});
        if (!entries.length) {
          this.tone.set("unknown");
          this.summary.set("Status unavailable");
          return;
        }
        const bad = entries.filter(([, s]) => s.status !== "ok");
        if (!bad.length) {
          this.tone.set("ok");
          this.summary.set("All services healthy");
          return;
        }
        const names = bad.map(([n]) => n).join(", ");
        const onlyDegraded = bad.every(([, s]) => s.status === "degraded");
        this.tone.set(onlyDegraded ? "warn" : "bad");
        this.summary.set(
          (onlyDegraded ? "Degraded: " : "Down: ") +
            names +
            " — click to diagnose",
        );
      },
      error: () => {
        this.tone.set("bad");
        this.summary.set("Orchestrator unreachable — click to diagnose");
      },
    });
  }
}
