import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from "@angular/core";
import { ActivatedRoute, RouterLink } from "@angular/router";
import { Subscription } from "rxjs";

import { RunMonitorService } from "../run-monitor/run-monitor.service";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import {
  Diagnosis,
  LoadApiService,
  LoadCompare,
  LoadDelta,
  LoadProfileType,
  LoadSnapshot,
} from "./load-api.service";
import { LoadRunCardComponent, Point } from "./load-run-card.component";

/**
 * Read-only detail page for a single (historical or still-live) load run.
 * Reopens a run by id: fetches its final/merged snapshot, replays the stored
 * time-series into the shared metrics card, and offers the run-vs-run
 * comparison + diagnosis. Reached from the History table on the Load page.
 */
@Component({
  selector: "pp-load-detail",
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    PageHeaderComponent,
    IconComponent,
    LoadRunCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="detail">
      <pp-page-header>
        <a routerLink="/load" class="back" subtitle>
          <pp-icon name="arrow-left" [size]="14" /> Back to load tests
        </a>
      </pp-page-header>

      <div class="head">
        <h2 class="mono">{{ runId }}</h2>
        @if (running()) {
          <span class="dot dot--live"></span><span>running</span>
        } @else if (snapshot()) {
          <span class="dot dot--done"></span><span>finished</span>
        }
        @if (profileType(); as t) {
          <span class="prof">{{ t }}</span>
        }
      </div>

      @if (error()) {
        <p class="error">{{ error() }}</p>
      }

      <div class="card">
        <pp-load-run-card
          [snapshot]="snapshot()"
          [points]="points()"
          [type]="profileType()"
          [running]="running()"
          [diagnosing]="diagnosing()"
          [diagnosis]="diagnosis()"
          (diagnose)="diagnose()"
        />

        <!-- run-vs-run comparison (regression detection) -->
        @if (!running()) {
          <div class="cmp">
            <div class="cmp__head">
              <h3>vs previous run</h3>
              @if (compare()?.base_found) {
                <span
                  class="cmp__verdict"
                  [class.cmp__verdict--bad]="
                    (compare()!.summary.regressions || 0) > 0
                  "
                  [class.cmp__verdict--good]="
                    (compare()!.summary.regressions || 0) === 0 &&
                    (compare()!.summary.improvements || 0) > 0
                  "
                >
                  {{ compare()!.summary.regressions }} regressed ·
                  {{ compare()!.summary.improvements }} improved
                </span>
              }
            </div>
            @if (comparing()) {
              <p class="muted small">Comparing…</p>
            } @else if (compare(); as c) {
              @if (!c.base_found) {
                <p class="muted small">
                  No prior run to compare against yet — run this profile again
                  to start tracking regressions.
                </p>
              } @else {
                <table class="cmp__table">
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>This run</th>
                      <th>Baseline</th>
                      <th>Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (row of compareRows; track row.key) {
                      <tr>
                        <td>{{ row.label }}</td>
                        <td>
                          {{ fmtVal(c.deltas[row.key].current, row.unit) }}
                        </td>
                        <td>
                          {{
                            c.deltas[row.key].base === null
                              ? "—"
                              : fmtVal(c.deltas[row.key].base!, row.unit)
                          }}
                        </td>
                        <td
                          class="cmp__delta"
                          [class.cmp__delta--bad]="c.deltas[row.key].regressed"
                          [class.cmp__delta--good]="c.deltas[row.key].improved"
                        >
                          {{ fmtDelta(c.deltas[row.key], row.unit) }}
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                @if (c.error_categories.length > 0) {
                  <h4 class="cmp__sub">Top error categories</h4>
                  <table class="cmp__table cmp__table--cats">
                    <tbody>
                      @for (cat of c.error_categories; track cat.category) {
                        <tr>
                          <td>
                            <code>{{ cat.category }}</code>
                          </td>
                          <td>{{ cat.current }}</td>
                          <td class="muted">was {{ cat.base }}</td>
                          <td
                            class="cmp__delta"
                            [class.cmp__delta--bad]="cat.delta > 0"
                            [class.cmp__delta--good]="cat.delta < 0"
                          >
                            {{ cat.delta > 0 ? "+" : "" }}{{ cat.delta }}
                          </td>
                        </tr>
                      }
                    </tbody>
                  </table>
                }
                <p class="muted small cmp__base">
                  Baseline: {{ c.base_run_id || "—" }}
                </p>
              }
            } @else {
              <button class="btn btn--sm" (click)="loadCompare()">
                Compare to previous run
              </button>
            }
          </div>
        }
      </div>
    </div>
  `,
  styles: [
    `
      .detail {
        padding: 1.25rem 1.5rem;
      }
      .back {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        color: var(--pp-text-muted, #8a93a6);
        text-decoration: none;
        font-size: 0.85rem;
      }
      .back:hover {
        color: var(--pp-primary, #2f6fed);
      }
      .head {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 0.5rem 0 0.8rem;
      }
      .head h2 {
        margin: 0 0.4rem 0 0;
        font-size: 1rem;
      }
      .mono {
        font-family: var(--pp-font-mono, monospace);
      }
      .prof {
        font-size: 0.66rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--pp-primary, #2f6fed);
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 12%,
          transparent
        );
        border-radius: 999px;
        padding: 0.05rem 0.45rem;
      }
      .dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        display: inline-block;
      }
      .dot--live {
        background: #30a46c;
        box-shadow: 0 0 0 3px rgba(48, 164, 108, 0.18);
      }
      .dot--done {
        background: #8a93a6;
      }
      .card {
        background: var(--pp-surface, #fff);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 12px;
        padding: 0.7rem 0.8rem;
        max-width: 720px;
      }
      .error {
        color: #e5484d;
        margin: 0.6rem 0;
        font-size: 0.85rem;
      }
      .muted {
        color: var(--pp-text-muted, #8a93a6);
      }
      .small {
        font-size: 0.78rem;
      }
      .btn--sm {
        background: var(--pp-surface-2, #fafbfc);
        border: 1px solid var(--pp-border, #e6e8ee);
        color: inherit;
        border-radius: 8px;
        padding: 0.4rem 0.8rem;
        font-size: 0.8rem;
        font-weight: 600;
        cursor: pointer;
      }
      .cmp {
        margin-top: 1rem;
        padding-top: 0.8rem;
        border-top: 1px solid var(--pp-border, #e6e8ee);
      }
      .cmp__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.5rem;
      }
      .cmp__head h3 {
        margin: 0;
        font-size: 0.85rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .cmp__verdict {
        font-size: 0.72rem;
        font-weight: 600;
        padding: 0.1rem 0.45rem;
        border-radius: 999px;
        background: var(--pp-surface-2, #fafbfc);
        border: 1px solid var(--pp-border, #e6e8ee);
        color: var(--pp-text-muted, #8a93a6);
      }
      .cmp__verdict--bad {
        color: #e5484d;
        border-color: #e5484d55;
        background: #e5484d11;
      }
      .cmp__verdict--good {
        color: #2f9e54;
        border-color: #2f9e5455;
        background: #2f9e5411;
      }
      .cmp__table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 0.5rem;
        font-size: 0.8rem;
      }
      .cmp__table th {
        text-align: left;
        font-weight: 500;
        color: var(--pp-text-muted, #8a93a6);
        padding: 0.2rem 0.4rem;
        border-bottom: 1px solid var(--pp-border, #e6e8ee);
      }
      .cmp__table td {
        padding: 0.25rem 0.4rem;
        border-bottom: 1px solid var(--pp-surface-2, #f1f3f7);
      }
      .cmp__table--cats code {
        font-size: 0.74rem;
      }
      .cmp__delta {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        color: var(--pp-text-muted, #8a93a6);
      }
      .cmp__delta--bad {
        color: #e5484d;
      }
      .cmp__delta--good {
        color: #2f9e54;
      }
      .cmp__sub {
        margin: 0.7rem 0 0;
        font-size: 0.78rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .cmp__base {
        margin-top: 0.4rem;
        font-family: var(--pp-font-mono, monospace);
        font-size: 0.7rem;
      }
    `,
  ],
})
export class LoadDetailComponent implements OnInit, OnDestroy {
  private readonly api = inject(LoadApiService);
  private readonly monitor = inject(RunMonitorService);
  private readonly route = inject(ActivatedRoute);

  runId = "";
  readonly snapshot = signal<LoadSnapshot | null>(null);
  readonly points = signal<Point[]>([]);
  readonly running = signal(false);
  readonly error = signal<string | null>(null);
  readonly diagnosing = signal(false);
  readonly diagnosis = signal<Diagnosis | null>(null);

  readonly compare = signal<LoadCompare | null>(null);
  readonly comparing = signal(false);

  /** Profile type drives the concurrency-vs-connections stat block. */
  readonly profileType = computed<LoadProfileType>(() => {
    const p = this.snapshot()?.profile;
    return p === "soak" || p === "ramp" || p === "spike" ? p : "steady";
  });

  readonly compareRows: {
    key: keyof LoadCompare["deltas"];
    label: string;
    unit: string;
  }[] = [
    { key: "latency_p50", label: "Latency p50", unit: "ms" },
    { key: "latency_p95", label: "Latency p95", unit: "ms" },
    { key: "latency_p99", label: "Latency p99", unit: "ms" },
    { key: "error_rate", label: "Error rate", unit: "%" },
    { key: "tps", label: "Throughput", unit: "tps" },
    { key: "tps_cv", label: "TPS stability (cv)", unit: "" },
  ];

  private sub?: Subscription;
  private routeSub?: Subscription;

  ngOnInit(): void {
    this.routeSub = this.route.paramMap.subscribe((pm) => {
      const id = pm.get("id");
      if (id) this.reopen(id);
    });
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
    this.routeSub?.unsubscribe();
  }

  private reopen(runId: string): void {
    this.runId = runId;
    this.error.set(null);
    this.diagnosis.set(null);
    this.compare.set(null);
    this.points.set([]);
    this.snapshot.set(null);
    this.api.get(runId).subscribe({
      next: (resp) => {
        const snap = ((resp as unknown as { summary?: LoadSnapshot }).summary ??
          resp) as LoadSnapshot;
        this.snapshot.set(snap);
        const live =
          (resp as unknown as { status?: string }).status === "running" ||
          (!!snap.status && snap.status === "running");
        this.running.set(live);
        if (!live) this.loadCompare(runId);
      },
      error: () => this.error.set("Could not load that run."),
    });
    this.watch(runId);
  }

  private watch(runId: string): void {
    this.sub?.unsubscribe();
    this.sub = this.monitor.stream(runId).subscribe({
      next: (ev) => {
        if (ev.type === "load.sample" || ev.type === "load.completed") {
          const s = ev.data as unknown as LoadSnapshot;
          this.snapshot.set(s);
          this.points.update((h) =>
            [
              ...h,
              {
                t: s.elapsed_s,
                tps: s.tps,
                p95: s.latency_ms.p95,
                p99: s.latency_ms.p99,
              },
            ].slice(-240),
          );
        }
        if (ev.type === "run.completed" || ev.type === "load.completed") {
          this.running.set(false);
          this.loadCompare(runId);
        }
      },
      error: () => this.running.set(false),
      complete: () => this.running.set(false),
    });
  }

  loadCompare(runId?: string): void {
    const id = runId ?? this.runId;
    if (!id) return;
    this.comparing.set(true);
    this.api.compare(id).subscribe({
      next: (c) => {
        this.compare.set(c);
        this.comparing.set(false);
      },
      error: () => {
        this.compare.set(null);
        this.comparing.set(false);
      },
    });
  }

  diagnose(): void {
    this.diagnosing.set(true);
    this.api.diagnose(this.runId).subscribe({
      next: (d) => {
        this.diagnosis.set(d);
        this.diagnosing.set(false);
      },
      error: () => {
        this.diagnosing.set(false);
        this.error.set("Diagnosis failed — is the orchestrator running?");
      },
    });
  }

  fmtDelta(d: LoadDelta, unit: string): string {
    if (d.delta === null) return "—";
    const sign = d.delta > 0 ? "+" : d.delta < 0 ? "−" : "";
    const mag = Math.abs(unit === "%" ? d.delta * 100 : d.delta);
    const val =
      unit === "%" ? mag.toFixed(2) : mag.toFixed(unit === "" ? 3 : 1);
    const suffix = unit ? (unit === "%" ? " pp" : ` ${unit}`) : "";
    const pct =
      d.pct !== null && unit !== "%" && unit !== ""
        ? ` (${d.pct > 0 ? "+" : d.pct < 0 ? "−" : ""}${Math.abs(d.pct)}%)`
        : "";
    return `${sign}${val}${suffix}${pct}`;
  }

  fmtVal(v: number, unit: string): string {
    if (unit === "%") return `${(v * 100).toFixed(2)}%`;
    if (unit === "") return v.toFixed(3);
    return `${v.toFixed(1)}${unit === "ms" ? " ms" : ` ${unit}`}`;
  }
}
