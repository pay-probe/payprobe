import { CommonModule, DatePipe } from "@angular/common";
import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";
import { Subscription } from "rxjs";

import { ThemeService } from "../shared/theme.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";
import { KpiCardComponent } from "../shared/ui/kpi-card.component";
import {
  RunApiService,
  Simulator,
  TrendPoint,
} from "../run-monitor/run-api.service";
import { RunMonitorService } from "../run-monitor/run-monitor.service";
import {
  PhaseUpdate,
  RunListItem,
  StepResult,
} from "../run-monitor/run.models";
import { SystemService } from "../settings/system.service";
import {
  EndpointHealthService,
  ProbeResult,
} from "../shared/endpoint-health.service";
import {
  EndpointConfig,
  RuntimeConfigService,
} from "../shared/runtime-config.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

export interface StatCard {
  label: string;
  value: string;
  hint: string;
  accent?: boolean;
  icon?: string;
  delta?: number;
  deltaSuffix?: string;
  invert?: boolean;
  spark?: number[];
}

interface HealthRow {
  name: string;
  status: "online" | "degraded" | "offline";
  detail: string;
}

interface EndpointRow {
  cfg: EndpointConfig;
  url: string;
  result: ProbeResult;
}

interface LiveRunView {
  id: string;
  label: string;
  environment: string;
  progress: number;
  step: string;
}

@Component({
  selector: "app-dashboard",
  standalone: true,
  imports: [
    PageHeaderComponent,
    CommonModule,
    DatePipe,
    RouterLink,
    KpiCardComponent,
    StaleChipComponent,
  ],
  templateUrl: "./dashboard.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./dashboard.component.scss",
})
export class DashboardComponent implements OnInit, OnDestroy {
  readonly theme = inject(ThemeService);
  private readonly api = inject(RunApiService);
  private readonly monitor = inject(RunMonitorService);
  private readonly system = inject(SystemService);
  private readonly cfg = inject(RuntimeConfigService);
  private readonly health = inject(EndpointHealthService);

  readonly pollHealth = new PollHealth();
  private readonly clockPoll = new PagePoll(1000, () =>
    this.now.set(new Date()),
  );
  private readonly tickerPoll = new PagePoll(2600, () =>
    this.tickerIndex.update((i) => i + 1),
  );
  private readonly poll = new PagePoll(30_000, () => {
    this.refresh();
    this.refreshEndpoints();
  });
  private liveSub?: Subscription;
  private livePhases: Record<number, PhaseUpdate> = {};

  readonly now = signal(new Date());

  /** Loading / error posture for the initial fetch. */
  readonly loading = signal(true);
  readonly loadError = signal<string | null>(null);

  // ---- raw data, straight from the backend ----
  readonly runs = signal<RunListItem[]>([]);
  readonly trend = signal<TrendPoint[]>([]);
  readonly simulators = signal<Simulator[]>([]);

  // ---- live status-bar ticker, fed by real step events ----
  private readonly tickerLines = signal<string[]>([]);
  readonly tickerIndex = signal(0);
  readonly tickerLine = computed(() => {
    const lines = this.tickerLines();
    if (lines.length === 0)
      return "Awaiting activity — start a run to see live events";
    return lines[this.tickerIndex() % lines.length];
  });

  // ---- KPI cards, derived from runs + trend ----
  readonly stats = computed<StatCard[]>(() => {
    const t = this.trend();
    const runs = this.runs();

    // Runs today vs yesterday (from the daily trend series).
    const runsSeries = t.map((p) => p.runs);
    const runsToday = runsSeries.at(-1) ?? 0;
    const runsYesterday = runsSeries.at(-2) ?? 0;

    // Pass rate over the trailing 7 days vs the prior 7 days.
    const last7 = t.slice(-7);
    const prev7 = t.slice(-14, -7);
    const rate = (pts: TrendPoint[]) => {
      const tot = pts.reduce((a, p) => a + p.scenarios_total, 0);
      const pass = pts.reduce((a, p) => a + p.scenarios_passed, 0);
      return tot > 0 ? (pass / tot) * 100 : null;
    };
    const passRate = rate(last7);
    const prevRate = rate(prev7);
    const passSpark = last7
      .map((p) => p.pass_rate)
      .filter((v): v is number => v != null)
      .map((v) => v * 100);

    // Average duration of recently completed runs (real timestamps).
    const durations = runs
      .map((r) => this.runDurationSec(r))
      .filter((s): s is number => s != null);
    const avgDur =
      durations.length > 0
        ? durations.reduce((a, b) => a + b, 0) / durations.length
        : null;

    return [
      {
        label: "Runs today",
        value: String(runsToday),
        hint: "vs yesterday",
        icon: "activity",
        delta: runsToday - runsYesterday,
        deltaSuffix: "",
        spark: runsSeries.slice(-7),
      },
      {
        label: "Pass rate (7d)",
        value: passRate == null ? "—" : `${passRate.toFixed(1)}%`,
        hint: "vs last week",
        accent: true,
        icon: "check",
        delta:
          passRate != null && prevRate != null
            ? +(passRate - prevRate).toFixed(1)
            : undefined,
        spark: passSpark,
      },
      {
        label: "Avg duration",
        value: avgDur == null ? "—" : this.fmtDuration(Math.round(avgDur)),
        hint: "recent runs",
        icon: "clock",
        invert: true,
        spark: durations.slice(0, 7).reverse(),
      },
      {
        label: "Simulators",
        value: String(this.simulators().length),
        hint: "active",
        icon: "terminal",
      },
    ];
  });

  /** Most recent runs for the table (newest first, capped). */
  readonly recentRuns = computed(() => this.runs().slice(0, 8));

  readonly liveRun = signal<LiveRunView | null>(null);

  // ---- system health, from the orchestrator's /status aggregator ----
  readonly services = signal<HealthRow[]>([]);
  readonly healthSummary = computed(() => {
    const svc = this.services();
    if (this.loading()) return "Loading platform status…";
    if (svc.length === 0) return "Status unavailable";
    const down = svc.filter((s) => s.status !== "online").length;
    return down === 0
      ? "All systems operational"
      : `${down} service${down === 1 ? "" : "s"} degraded`;
  });

  // ---- endpoint monitor: browser-side reachability + latency, with URLs ----
  readonly endpoints = signal<EndpointRow[]>([]);

  ngOnInit(): void {
    // All three fire immediately and skip ticks while the tab is hidden.
    this.clockPoll.start();
    this.tickerPoll.start();
    this.poll.start();
  }

  ngOnDestroy(): void {
    this.clockPoll.stop();
    this.tickerPoll.stop();
    this.poll.stop();
    this.pollHealth.destroy();
    this.liveSub?.unsubscribe();
  }

  /** Pull the whole overview; tolerate partial failures per-panel. */
  private refresh(): void {
    this.api.list().subscribe({
      next: (runs) => {
        this.loading.set(false);
        this.loadError.set(null);
        this.runs.set(runs);
        this.syncLiveRun(runs);
        this.pollHealth.markOk();
      },
      error: (err) => {
        this.loading.set(false);
        this.loadError.set(this.errMsg(err));
        this.pollHealth.markError();
      },
    });

    this.api.trend(14).subscribe({
      next: (t) => this.trend.set(t),
      error: () => {},
    });

    this.api.simulators().subscribe({
      next: (s) => this.simulators.set(s),
      error: () => this.simulators.set([]),
    });

    this.system.status().subscribe({
      next: (info) => {
        const rows: HealthRow[] = Object.entries(info.services).map(
          ([name, s]) => ({
            name: this.prettyService(name),
            status:
              s.status === "ok"
                ? "online"
                : s.status === "degraded"
                  ? "degraded"
                  : "offline",
            detail: s.error ? "unreachable" : s.status,
          }),
        );
        this.services.set(rows);
      },
      error: () => this.services.set([]),
    });
  }

  /** Find an in-flight run and stream it; tear down the stream when none. */
  private syncLiveRun(runs: RunListItem[]): void {
    const active = runs.find(
      (r) => r.status === "running" || r.status === "pending",
    );
    if (!active) {
      if (this.liveRun()) {
        this.liveSub?.unsubscribe();
        this.liveSub = undefined;
        this.liveRun.set(null);
      }
      return;
    }

    // Already streaming this run — nothing to do.
    if (this.liveRun()?.id === active.id) return;

    this.liveSub?.unsubscribe();
    this.livePhases = {};
    this.liveRun.set({
      id: active.id,
      label: active.label ?? active.id,
      environment: active.environment ?? "—",
      progress: 0,
      step: "Connecting to live stream…",
    });

    this.liveSub = this.monitor.stream(active.id).subscribe({
      next: (event) => {
        switch (event.type) {
          case "phase.update": {
            const p = event.data as unknown as PhaseUpdate;
            this.livePhases[p.phase] = p;
            this.updateLiveProgress();
            break;
          }
          case "step.result": {
            const s = event.data as unknown as StepResult;
            this.pushTicker(
              `${s.scenario} · ${s.target}.${s.action} → ${s.status}`,
            );
            const cur = this.liveRun();
            if (cur)
              this.liveRun.set({ ...cur, step: `${s.scenario} · ${s.step}` });
            break;
          }
          case "run.completed": {
            const cur = this.liveRun();
            if (cur) this.liveRun.set({ ...cur, progress: 100 });
            // Refresh the registry so the finished run lands in the table.
            this.api.list().subscribe((r) => {
              this.runs.set(r);
              this.syncLiveRun(r);
            });
            break;
          }
        }
      },
      error: () => {},
    });
  }

  private updateLiveProgress(): void {
    const phases = Object.values(this.livePhases);
    const total = phases.reduce((a, p) => a + (p.total ?? 0), 0);
    const done = phases.reduce(
      (a, p) => a + (p.passed ?? 0) + (p.failed ?? 0) + (p.blocked ?? 0),
      0,
    );
    const pct = total > 0 ? Math.min((done / total) * 100, 100) : 0;
    const cur = this.liveRun();
    if (cur) this.liveRun.set({ ...cur, progress: pct });
  }

  private pushTicker(line: string): void {
    this.tickerLines.update((lines) => [line, ...lines].slice(0, 8));
    this.tickerIndex.set(0);
  }

  // ---- helpers ----

  passRate(run: RunListItem): number {
    const { passed, total } = run.scenarios;
    return total > 0 ? Math.round((passed / total) * 100) : 0;
  }

  runDurationSec(run: RunListItem): number | null {
    if (!run.started_at || !run.completed_at) return null;
    const start = new Date(run.started_at).getTime();
    const end = new Date(run.completed_at).getTime();
    if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
    return Math.round((end - start) / 1000);
  }

  durationLabel(run: RunListItem): string {
    const s = this.runDurationSec(run);
    return s == null ? "—" : this.fmtDuration(s);
  }

  fmtDuration(sec: number): string {
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    return `${m}m ${sec % 60}s`;
  }

  timeAgo(iso: string | null): string {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "—";
    const mins = Math.round((Date.now() - t) / 60_000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const h = Math.floor(mins / 60);
    if (h < 24) return `${h}h ${mins % 60}m ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  statusKind(status: RunListItem["status"]): "pass" | "fail" | "run" | "other" {
    if (status === "passed") return "pass";
    if (status === "failed" || status === "error") return "fail";
    if (status === "running" || status === "pending") return "run";
    return "other";
  }

  /** Probe every configured endpoint from the browser and refresh the panel. */
  private refreshEndpoints(): void {
    const eps = this.cfg.endpoints();
    // Seed rows immediately (URLs + "checking") so the panel never looks empty.
    const seed: EndpointRow[] = eps.map((cfg) => ({
      cfg,
      url:
        cfg.kind === "redis"
          ? `${cfg.scheme}://${cfg.host}:${cfg.port}`
          : // MCP is reached at its /mcp path — show that, not the bare root
            // (which 404s), so the URL is the actual connect target.
            `${this.cfg.baseUrl(cfg)}${cfg.kind === "mcp" ? (cfg.healthPath ?? "") : ""}`,
      result:
        this.endpoints().find((r) => r.cfg.key === cfg.key)?.result ??
        ({ status: "unknown", detail: "checking…" } as ProbeResult),
    }));
    this.endpoints.set(seed);
    void this.health.probeAll().then((results) => {
      this.endpoints.set(
        seed.map((row) => ({
          ...row,
          result: results.get(row.cfg.key) ?? row.result,
        })),
      );
    });
  }

  private prettyService(name: string): string {
    return name
      .split(/[-_]/)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  }

  private errMsg(err: unknown): string {
    const e = err as { status?: number; message?: string };
    if (e?.status === 0) return "Cannot reach the orchestrator API";
    if (e?.status) return `Request failed (${e.status})`;
    return e?.message ?? "Failed to load dashboard";
  }
}
