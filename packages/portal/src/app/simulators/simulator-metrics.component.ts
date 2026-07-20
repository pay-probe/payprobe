import {
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { ActivatedRoute, RouterLink } from "@angular/router";

import {
  RunApiService,
  SavedSimulator,
  SimulatorMetrics,
} from "../run-monitor/run-api.service";
import { UiService } from "../shared/ui.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { KpiCardComponent } from "../shared/ui/kpi-card.component";
import { ChaosDialComponent } from "./chaos-dial.component";

interface Breakdown {
  key: string;
  count: number;
  pct: number;
}

/** Dedicated live metrics dashboard for one host/responder simulator — the
 *  simulator equivalent of the load-run page: throughput timeline, response-code
 *  and message-type mix, fault rate and recent traffic, refreshed every 2s. */
@Component({
  selector: "app-simulator-metrics",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    KpiCardComponent,
    ChaosDialComponent,
    RouterLink,
    StaleChipComponent,
  ],
  template: `
    <div class="pg">
      <pp-page-header
        [subtitle]="
          saved()
            ? saved()!.label + ' · ' + saved()!.protocol
            : 'Simulator metrics'
        "
      >
        <pp-stale-chip [health]="pollHealth" />
        <a class="back" routerLink="/simulators">
          <pp-icon name="chevron" [size]="14" /> All simulators
        </a>
        @if (running()) {
          <pp-button variant="ghost" size="sm" icon="undo" (click)="clear()">
            Clear stats
          </pp-button>
          <pp-button variant="secondary" size="sm" icon="x" (click)="stop()">
            Stop
          </pp-button>
        } @else if (saved()) {
          <pp-button variant="primary" size="sm" icon="play" (click)="start()">
            Start
          </pp-button>
        }
      </pp-page-header>

      <div class="pg__body">
        @if (!running()) {
          <div class="state">
            <span class="state__icon"><pp-icon name="cpu" [size]="28" /></span>
            <h3>
              {{ saved() ? "Simulator is stopped" : "Simulator not found" }}
            </h3>
            <p class="muted">
              @if (saved()) {
                Start “{{ saved()!.label }}” to begin collecting live metrics.
              } @else {
                It may have been deleted, or never existed.
              }
            </p>
            @if (saved()) {
              <pp-button variant="primary" icon="play" (click)="start()">
                Start simulator
              </pp-button>
            }
          </div>
        } @else if (metrics(); as m) {
          <section class="kpis">
            <pp-kpi-card
              label="Throughput"
              [value]="m.rps || 0"
              icon="activity"
              caption="requests/sec"
            />
            <pp-kpi-card
              label="Avg req/s"
              [value]="m.stats.avg_rps"
              icon="chart"
              [caption]="m.stats.elapsed_s + 's window'"
            />
            <pp-kpi-card
              label="Received"
              [value]="m.stats.received"
              icon="layers"
              caption="messages"
            />
            <pp-kpi-card
              label="Faults"
              [value]="m.stats.faults"
              icon="zap"
              [caption]="m.stats.faults ? 'chaos active' : 'none'"
            />
            <pp-kpi-card
              label="Port"
              [value]="m.port || 0"
              icon="cpu"
              caption="listening"
            />
          </section>

          <app-chaos-dial [simId]="id()" [running]="running()" />

          <section class="panel">
            <div class="panel__head">
              <h3>Throughput</h3>
              <span class="now">{{ m.rps || 0 }} req/s</span>
            </div>
            <div class="chart">
              <svg viewBox="0 0 800 220" preserveAspectRatio="none">
                <polyline class="area" [attr.points]="areaPath()" />
                <polyline class="line" [attr.points]="linePath()" fill="none" />
              </svg>
              <div class="chart__axis">
                <span class="muted">0</span>
                <span class="muted">{{ maxRps() }} req/s</span>
              </div>
            </div>
          </section>

          <section class="two">
            <div class="panel">
              <div class="panel__head"><h3>Response codes</h3></div>
              @if (!rcBreakdown().length) {
                <p class="muted empty">No replies yet.</p>
              }
              @for (b of rcBreakdown(); track b.key) {
                <div class="bar">
                  <span class="bar__k mono">{{ b.key }}</span>
                  <span class="bar__track">
                    <span class="bar__fill" [style.width.%]="b.pct"></span>
                  </span>
                  <span class="bar__v">{{ b.count }}</span>
                </div>
              }
            </div>

            <div class="panel">
              <div class="panel__head"><h3>Message types</h3></div>
              @if (!mtiBreakdown().length) {
                <p class="muted empty">No traffic yet.</p>
              }
              @for (b of mtiBreakdown(); track b.key) {
                <div class="bar">
                  <span class="bar__k mono">{{ b.key }}</span>
                  <span class="bar__track">
                    <span
                      class="bar__fill bar__fill--alt"
                      [style.width.%]="b.pct"
                    ></span>
                  </span>
                  <span class="bar__v">{{ b.count }}</span>
                </div>
              }
            </div>
          </section>

          <section class="panel">
            <div class="panel__head">
              <h3>Recent traffic</h3>
              @if (saved()?.chaos) {
                <pp-badge tone="warning">chaos</pp-badge>
              }
            </div>
            @if (!m.log?.length) {
              <p class="muted empty">
                No traffic yet — point a step's connection at port {{ m.port }}.
              </p>
            }
            <div class="log">
              @for (line of (m.log || []).slice().reverse(); track $index) {
                <code class="logline">
                  @if (line.mti) {
                    <span class="logline__mti">MTI {{ line.mti }}</span>
                    <span class="logline__de">{{ deSummary(line.de) }}</span>
                  } @else {
                    <span class="logline__mti">{{ line.command }}</span>
                    <span class="logline__de">hdr {{ line.header }}</span>
                  }
                  @if (line.chaos) {
                    <span class="fault">⚡ {{ line.chaos }}</span>
                  }
                </code>
              }
            </div>
          </section>
        } @else {
          <div class="state"><span class="muted">Loading…</span></div>
        }
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--bg-base);
        color: var(--text-primary);
      }
      .pg {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .pg__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: var(--space-6);
        display: flex;
        flex-direction: column;
        gap: var(--space-5);
      }
      .muted {
        color: var(--text-muted);
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
      }
      .back {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: var(--text-sm);
        color: var(--text-secondary);
        text-decoration: none;
      }
      .back:hover {
        color: var(--brand);
      }
      .back pp-icon {
        transform: rotate(90deg);
      }

      .kpis {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: var(--space-4);
      }

      .panel {
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
        box-shadow: var(--shadow-xs);
        padding: var(--space-5);
        display: flex;
        flex-direction: column;
        gap: var(--space-3);
      }
      .panel__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-3);
      }
      .panel__head h3 {
        margin: 0;
        font-size: var(--text-base);
      }
      .now {
        font-size: var(--text-sm);
        font-weight: var(--weight-semibold);
        color: var(--brand);
      }
      .two {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-4);
      }

      .chart {
        display: flex;
        flex-direction: column;
        gap: var(--space-1);
      }
      .chart svg {
        width: 100%;
        height: 220px;
        display: block;
      }
      .line {
        stroke: var(--brand);
        stroke-width: 2;
        vector-effect: non-scaling-stroke;
      }
      .area {
        fill: color-mix(in srgb, var(--brand) 14%, transparent);
        stroke: none;
      }
      .chart__axis {
        display: flex;
        justify-content: space-between;
        font-size: var(--text-xs);
      }

      .bar {
        display: grid;
        grid-template-columns: 56px 1fr auto;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
      }
      .bar__k {
        color: var(--text-secondary);
      }
      .bar__track {
        height: 10px;
        border-radius: var(--radius-full);
        background: var(--bg-subtle);
        overflow: hidden;
      }
      .bar__fill {
        display: block;
        height: 100%;
        border-radius: var(--radius-full);
        background: var(--brand);
      }
      .bar__fill--alt {
        background: color-mix(
          in srgb,
          var(--brand) 55%,
          var(--color-info, #38bdf8)
        );
      }
      .bar__v {
        font-weight: var(--weight-semibold);
      }
      .empty {
        margin: 0;
      }

      .log {
        display: flex;
        flex-direction: column;
        gap: 4px;
        max-height: 320px;
        overflow: auto;
      }
      .logline {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: var(--text-xs);
        white-space: nowrap;
      }
      .logline__mti {
        color: var(--brand);
        font-weight: var(--weight-semibold);
      }
      .logline__de {
        color: var(--text-secondary);
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .fault {
        margin-left: auto;
        flex: none;
        padding: 1px 7px;
        border-radius: var(--radius-full);
        font-size: 10px;
        background: color-mix(in srgb, var(--color-warning) 20%, transparent);
        color: var(--color-warning);
      }

      .state {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        gap: var(--space-3);
        padding: var(--space-8) var(--space-6);
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .state h3 {
        margin: 0;
        font-size: var(--text-lg);
      }
      .state__icon {
        display: grid;
        place-items: center;
        width: 56px;
        height: 56px;
        border-radius: var(--radius-full);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
      }

      @media (max-width: 860px) {
        .kpis {
          grid-template-columns: 1fr 1fr;
        }
        .two {
          grid-template-columns: 1fr;
        }
      }
    `,
  ],
})
export class SimulatorMetricsComponent implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly ui = inject(UiService);

  readonly id = signal<string>("");
  readonly saved = signal<SavedSimulator | null>(null);
  readonly metrics = signal<SimulatorMetrics | null>(null);
  readonly running = computed(() => !!this.metrics());

  readonly pollHealth = new PollHealth();
  private readonly poll = new PagePoll(2000, () => this.refresh());

  readonly maxRps = computed(() => {
    const pts = this.metrics()?.samples ?? [];
    return Math.max(1, ...pts.map((p) => p.rps));
  });

  readonly linePath = computed(() => {
    const pts = this.metrics()?.samples ?? [];
    if (!pts.length) return "";
    const max = this.maxRps();
    const n = Math.max(1, pts.length - 1);
    return pts
      .map((p, i) => {
        const x = (i / n) * 800;
        const y = 214 - (p.rps / max) * 206;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  });

  /** The line path closed down to the baseline, for the filled area. */
  readonly areaPath = computed(() => {
    const line = this.linePath();
    if (!line) return "";
    const pts = this.metrics()?.samples ?? [];
    const n = Math.max(1, pts.length - 1);
    const lastX = ((pts.length - 1) / n) * 800;
    return `0,214 ${line} ${lastX.toFixed(1)},214`;
  });

  readonly rcBreakdown = computed(() =>
    this.breakdown(this.metrics()?.stats.by_response_code),
  );
  readonly mtiBreakdown = computed(() =>
    this.breakdown(this.metrics()?.stats.by_mti),
  );

  ngOnInit() {
    const id = this.route.snapshot.paramMap.get("id") || "";
    this.id.set(id);
    this.loadSaved();
    this.poll.start(); // fires immediately, skips ticks while the tab is hidden
  }

  ngOnDestroy() {
    this.poll.stop();
    this.pollHealth.destroy();
  }

  private breakdown(map?: Record<string, number>): Breakdown[] {
    if (!map) return [];
    const entries = Object.entries(map);
    const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
    return entries
      .map(([key, count]) => ({ key, count, pct: (count / total) * 100 }))
      .sort((a, b) => b.count - a.count);
  }

  private loadSaved() {
    this.api.getSavedSimulator(this.id()).subscribe({
      next: (s) => this.saved.set(s),
      error: () => this.saved.set(null),
    });
  }

  private refresh() {
    this.api.simulatorMetrics(this.id()).subscribe({
      next: (m) => {
        this.metrics.set(m);
        this.pollHealth.markOk();
      },
      error: (e: { status?: number }) => {
        if (e?.status === 404) {
          // a stopped simulator is a real answer, not a connection problem
          this.metrics.set(null);
          this.pollHealth.markOk();
        } else {
          this.pollHealth.markError();
        }
      },
    });
  }

  start() {
    this.api.startSavedSimulator(this.id()).subscribe({
      next: () => {
        this.ui.toast("success", "Simulator started.");
        this.loadSaved();
        this.refresh();
      },
      error: () => this.ui.toast("error", "Could not start simulator."),
    });
  }

  async stop() {
    const ok = await this.ui.confirm("Stop this simulator?", {
      title: "Stop simulator",
      confirmLabel: "Stop",
      danger: true,
    });
    if (!ok) return;
    this.api.stopSavedSimulator(this.id()).subscribe({
      next: () => {
        this.ui.toast("success", "Stopped.");
        this.metrics.set(null);
        this.loadSaved();
      },
      error: () => this.ui.toast("error", "Stop failed."),
    });
  }

  clear() {
    this.api.clearSimulator(this.id()).subscribe({
      next: () => {
        this.ui.toast("success", "Stats cleared.");
        this.refresh();
      },
      error: () => this.ui.toast("error", "Could not clear stats."),
    });
  }

  deSummary(de?: Record<string, string>): string {
    if (!de) return "";
    return Object.entries(de)
      .slice(0, 6)
      .map(([k, v]) => `DE${k}=${v}`)
      .join("  ");
  }
}
