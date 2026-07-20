import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { FormsModule } from "@angular/forms";

import {
  FlakyScenario,
  RunApiService,
  TrendPoint,
} from "../run-monitor/run-api.service";
import {
  InsightApiService,
  InsightPrediction,
} from "../shared/insight-api.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface PlotPoint {
  x: number;
  y: number;
  p: TrendPoint;
}

/** Regression-health trend — scenario pass-rate and run pass/fail over time,
 *  from the orchestrator's GET /runs/trend. Inline SVG (no chart dependency). */
@Component({
  selector: "app-trends",
  standalone: true,
  imports: [PageHeaderComponent, CommonModule, FormsModule],
  template: `
    <div class="tr">
      <pp-page-header>
        <div subtitle>
          <span class="muted">Regression health over time</span>
        </div>
        <label class="days"
          >Days
          <select
            [value]="days()"
            (change)="setDays($any($event.target).value)"
          >
            <option [value]="14">14</option>
            <option [value]="30">30</option>
            <option [value]="90">90</option>
          </select>
        </label>
      </pp-page-header>

      <div class="tr__body">
        @if (loading()) {
          <p class="muted">Loading…</p>
        } @else if (!data().length) {
          <p class="muted">
            No completed runs yet. Run a scenario, then come back.
          </p>
        } @else {
          @if (summaryStats()) {
            <div class="summary-stats">
              <div class="stat-item">
                <span class="stat-label">Avg pass rate</span>
                <span
                  class="stat-value"
                  [class.critical]="summaryStats()!.avgPassRate < 70"
                >
                  {{ summaryStats()!.avgPassRate }}%
                </span>
              </div>
              <div class="stat-item">
                <span class="stat-label">Total runs</span>
                <span class="stat-value">{{ summaryStats()!.totalRuns }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">Scenarios passed</span>
                <span class="stat-value ok">
                  {{ summaryStats()!.scenariosPassed }}/{{
                    summaryStats()!.scenariosTotal
                  }}
                </span>
              </div>
              <div
                class="stat-item"
                [class.warning]="summaryStats()!.criticalFlaky > 0"
              >
                <span class="stat-label">Critical flaky</span>
                <span class="stat-value bad">
                  {{ summaryStats()!.criticalFlaky }}
                </span>
              </div>
            </div>
          }
          <div class="card">
            <div class="card__h">Scenario pass-rate</div>
            <svg
              [attr.viewBox]="'0 0 ' + W + ' ' + H"
              preserveAspectRatio="none"
              class="chart"
            >
              <!-- gridlines -->
              @for (g of [0, 25, 50, 75, 100]; track g) {
                <line
                  [attr.x1]="PADL"
                  [attr.x2]="W - PADR"
                  [attr.y1]="yFor(g / 100)"
                  [attr.y2]="yFor(g / 100)"
                  class="grid"
                />
                <text
                  [attr.x]="PADL - 6"
                  [attr.y]="yFor(g / 100) + 3"
                  class="ylabel"
                >
                  {{ g }}
                </text>
              }
              <polyline [attr.points]="linePoints()" class="line" />
              @for (pt of points(); track pt.p.date) {
                <circle [attr.cx]="pt.x" [attr.cy]="pt.y" r="3" class="dot">
                  <title>
                    {{ pt.p.date }} — {{ pct(pt.p.pass_rate) }} ({{
                      pt.p.scenarios_passed
                    }}/{{ pt.p.scenarios_total }})
                  </title>
                </circle>
              }
            </svg>
          </div>

          <div class="card">
            <div class="card__h">Runs per day</div>
            <div class="bars">
              @for (p of data(); track p.date) {
                <div
                  class="bar"
                  [title]="
                    p.date +
                    ': ' +
                    p.passed +
                    ' passed, ' +
                    p.failed +
                    ' failed'
                  "
                >
                  <div class="bar__stack" [style.height.px]="80">
                    <div
                      class="bar__fail"
                      [style.height.%]="p.runs ? (p.failed / p.runs) * 100 : 0"
                    ></div>
                    <div
                      class="bar__pass"
                      [style.height.%]="p.runs ? (p.passed / p.runs) * 100 : 0"
                    ></div>
                  </div>
                  <span class="bar__x">{{ p.date.slice(5) }}</span>
                </div>
              }
            </div>
            <div class="legend">
              <span class="k pass"></span> passed
              <span class="k fail"></span> failed/error
            </div>
          </div>

          <div class="card">
            <div class="card__h">
              Flaky scenarios
              <span class="muted hint"
                >— intermittently pass &amp; fail (last {{ days() }} days)</span
              >
            </div>

            @if (flaky().length > 0) {
              <div class="flaky-controls">
                <input
                  type="text"
                  class="search-input"
                  placeholder="Search scenarios…"
                  (input)="onSearchFlaky($any($event.target).value)"
                />
                <div class="filter-tabs">
                  <button
                    class="tab"
                    [class.active]="flakyFilter() === 'all'"
                    (click)="setFlakyFilter('all')"
                  >
                    All ({{ flaky().length }})
                  </button>
                  <button
                    class="tab high"
                    [class.active]="flakyFilter() === 'high'"
                    (click)="setFlakyFilter('high')"
                  >
                    High
                  </button>
                  <button
                    class="tab med"
                    [class.active]="flakyFilter() === 'med'"
                    (click)="setFlakyFilter('med')"
                  >
                    Medium
                  </button>
                  <button
                    class="tab low"
                    [class.active]="flakyFilter() === 'low'"
                    (click)="setFlakyFilter('low')"
                  >
                    Low
                  </button>
                </div>
              </div>
            }

            @if (!flaky().length) {
              <p class="muted ok">✓ No flaky scenarios detected.</p>
            } @else if (filteredFlaky().length === 0) {
              <p class="muted">No scenarios match the filter.</p>
            } @else {
              <table class="flaky">
                <thead>
                  <tr>
                    <th>Scenario</th>
                    <th class="num">Flakiness</th>
                    <th class="num">Runs</th>
                    <th class="num">Pass / Fail</th>
                    <th class="num">Flips</th>
                    <th>Last</th>
                  </tr>
                </thead>
                <tbody>
                  @for (f of filteredFlaky(); track f.scenario_id || f.name) {
                    <tr>
                      <td>{{ f.name }}</td>
                      <td class="num">
                        <span class="sev" [class]="sev(f.score)">
                          {{ (f.score * 100).toFixed(0) }}%
                        </span>
                      </td>
                      <td class="num">{{ f.runs }}</td>
                      <td class="num">{{ f.passed }} / {{ f.failed }}</td>
                      <td class="num">{{ f.flips }}</td>
                      <td>
                        <span
                          class="dot"
                          [class.fail]="f.last_status === 'failed'"
                        ></span>
                        {{ f.last_status }}
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
            }
          </div>

          <!-- advisory model predictions (ADR-0005): a model OPINION over run
               history, never a gate. Card renders only when the insight
               service is deployed and has history. -->
          @if (predictions(); as preds) {
            @if (preds.length > 0) {
              <div class="card card--ai">
                <div class="card__h">
                  Likely to fail next
                  <span class="ai-badge">model — advisory</span>
                  <span class="muted hint"
                    >— predicted from run history; re-run risky ones first</span
                  >
                </div>
                <table class="flaky">
                  <thead>
                    <tr>
                      <th>Scenario</th>
                      <th class="num">P(fail next)</th>
                      <th class="num">P(flaky)</th>
                      <th class="num">History</th>
                      <th>Why</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (p of topPredictions(); track p.scenario_id) {
                      <tr>
                        <td>{{ p.name || p.scenario_id }}</td>
                        <td class="num">
                          <span class="sev" [class]="sevP(p.p_fail_next)">
                            {{ (p.p_fail_next * 100).toFixed(0) }}%
                          </span>
                        </td>
                        <td class="num">{{ (p.p_flaky * 100).toFixed(0) }}%</td>
                        <td class="num">{{ p.n_history }} runs</td>
                        <td class="muted why">
                          {{ p.top_factors[0] || "history-based estimate" }}
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                <p class="muted ai-note">
                  Basis: {{ preds[0].basis }} ({{ preds[0].model_version }}).
                  Advisory only — gates and sign-off stay deterministic.
                </p>
              </div>
            }
          }
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
        background: var(--color-bg);
        color: var(--color-text);
      }
      .tr {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .tr__bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .tr__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .sp {
        flex: 1 1 auto;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .days {
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .days select {
        margin-left: 6px;
        padding: 4px 8px;
        border: 1px solid var(--color-border);
        border-radius: 6px;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .tr__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 18px;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px;
        background: var(--color-node, var(--color-bg));
      }
      .card__h {
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 10px;
      }
      .chart {
        width: 100%;
        height: 220px;
      }
      .grid {
        stroke: var(--color-border);
        stroke-width: 1;
      }
      .ylabel {
        fill: var(--color-text-muted, #888);
        font-size: 9px;
        text-anchor: end;
      }
      .line {
        fill: none;
        stroke: var(--color-primary, #58a6ff);
        stroke-width: 2;
      }
      .dot {
        fill: var(--color-primary, #58a6ff);
      }
      .bars {
        display: flex;
        gap: 6px;
        align-items: flex-end;
        overflow-x: auto;
        padding-bottom: 4px;
      }
      .bar {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        min-width: 22px;
      }
      .bar__stack {
        width: 18px;
        display: flex;
        flex-direction: column;
        justify-content: flex-end;
        border-radius: 3px;
        overflow: hidden;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
      }
      .bar__pass {
        background: var(--color-success, #2ea043);
      }
      .bar__fail {
        background: var(--color-danger, #f85149);
      }
      .bar__x {
        font-size: 9px;
        color: var(--color-text-muted, var(--color-text));
      }
      .legend {
        margin-top: 8px;
        font-size: 11px;
        color: var(--color-text-muted, var(--color-text));
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .k {
        width: 10px;
        height: 10px;
        border-radius: 2px;
        display: inline-block;
      }
      .k.pass {
        background: var(--color-success, #2ea043);
      }
      .k.fail {
        background: var(--color-danger, #f85149);
      }
      .hint {
        font-weight: 400;
        font-size: 12px;
      }
      .ok {
        color: var(--color-success, #2ea043);
      }
      table.flaky {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
      }
      table.flaky th,
      table.flaky td {
        text-align: left;
        padding: 6px 8px;
        border-bottom: 1px solid var(--color-border);
      }
      table.flaky th {
        color: var(--color-text-muted, var(--color-text));
        font-weight: 500;
      }
      table.flaky .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .sev {
        display: inline-block;
        padding: 0 0.5em;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
      }
      .sev.high {
        background: color-mix(
          in srgb,
          var(--color-danger, #f85149) 18%,
          transparent
        );
        color: var(--color-danger, #f85149);
      }
      .sev.med {
        background: color-mix(
          in srgb,
          var(--color-warning, #c98a00) 20%,
          transparent
        );
        color: var(--color-warning, #c98a00);
      }
      .sev.low {
        background: var(--color-border);
        color: var(--color-text);
      }
      .dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 4px;
        background: var(--color-success, #2ea043);
      }
      .dot.fail {
        background: var(--color-danger, #f85149);
      }
      .summary-stats {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 1rem;
        margin-bottom: 1.5rem;
      }
      .stat-item {
        border: 1px solid var(--color-border);
        border-radius: 8px;
        padding: 1rem;
        background: var(--color-bg);
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
      }
      .stat-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        color: var(--color-text-muted, var(--color-text));
        font-weight: 600;
      }
      .stat-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: var(--color-text);
      }
      .stat-value.ok {
        color: var(--color-success, #2ea043);
      }
      .stat-value.bad {
        color: var(--color-danger, #f85149);
      }
      .stat-value.critical {
        color: var(--color-danger, #f85149);
      }
      .stat-item.warning {
        border-color: var(--color-warning, #c98a00);
      }
      /* advisory prediction card — styled as an opinion (dashed accent) */
      .card--ai {
        border-style: dashed;
        border-color: color-mix(in srgb, var(--color-primary) 45%, transparent);
      }
      .ai-badge {
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--color-primary);
        border: 1px solid
          color-mix(in srgb, var(--color-primary) 50%, transparent);
        border-radius: 999px;
        padding: 0.05rem 0.5rem;
        margin-left: 0.5rem;
      }
      .ai-note {
        margin: 0.6rem 0 0;
        font-size: 0.75rem;
      }
      .why {
        max-width: 260px;
        font-size: 0.78rem;
      }
      .flaky-controls {
        display: flex;
        gap: 1rem;
        margin-bottom: 1rem;
        flex-wrap: wrap;
        align-items: center;
      }
      .search-input {
        flex: 1;
        min-width: 200px;
        padding: 7px 10px;
        border: 1px solid var(--color-border);
        border-radius: 6px;
        background: var(--color-bg);
        color: var(--color-text);
        font-size: 13px;
      }
      .search-input::placeholder {
        color: var(--color-text-muted, var(--color-text));
      }
      .filter-tabs {
        display: flex;
        gap: 0.5rem;
      }
      .tab {
        padding: 6px 12px;
        border: 1px solid var(--color-border);
        border-radius: 6px;
        background: transparent;
        color: var(--color-text-muted, var(--color-text));
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        transition: all 0.2s;
      }
      .tab:hover {
        background: var(--color-border);
      }
      .tab.active {
        background: var(--color-primary);
        color: #fff;
        border-color: var(--color-primary);
      }
      .tab.high.active {
        background: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
      }
      .tab.med.active {
        background: var(--color-warning, #c98a00);
        border-color: var(--color-warning, #c98a00);
      }
      .tab.low.active {
        background: var(--color-border);
        color: var(--color-text);
      }
    `,
  ],
})
export class TrendsComponent implements OnInit {
  private readonly api = inject(RunApiService);
  private readonly insightApi = inject(InsightApiService);

  readonly W = 720;
  readonly H = 220;
  readonly PADL = 28;
  readonly PADR = 10;
  readonly PADT = 10;
  readonly PADB = 20;

  readonly data = signal<TrendPoint[]>([]);
  readonly flaky = signal<FlakyScenario[]>([]);
  /** Advisory outcome predictions from the insight service (ADR-0005).
   *  null = service not deployed/reachable — the card doesn't render. */
  readonly predictions = signal<InsightPrediction[] | null>(null);
  readonly loading = signal(true);
  readonly days = signal(30);
  readonly flakyFilter = signal<"all" | "high" | "med" | "low">("all");
  readonly searchFlaky = signal("");

  readonly filteredFlaky = computed(() => {
    const filter = this.flakyFilter();
    const search = this.searchFlaky().toLowerCase();
    let items = this.flaky();

    if (filter !== "all") {
      items = items.filter((f) => this.sev(f.score) === filter);
    }

    if (search) {
      items = items.filter((f) =>
        (f.name || f.scenario_id).toLowerCase().includes(search),
      );
    }

    return items;
  });

  /** Riskiest scenarios first (the API pre-sorts; cap the card at 10). */
  readonly topPredictions = computed(() =>
    (this.predictions() ?? []).slice(0, 10),
  );

  /** Severity bucket for a failure probability (advisory coloring). */
  sevP(p: number): "high" | "med" | "low" {
    return p >= 0.6 ? "high" : p >= 0.3 ? "med" : "low";
  }

  readonly summaryStats = computed(() => {
    const data = this.data();
    const flaky = this.flaky();
    if (data.length === 0) return null;

    const passed = data.reduce((sum, p) => sum + (p.scenarios_passed ?? 0), 0);
    const total = data.reduce((sum, p) => sum + (p.scenarios_total ?? 0), 0);
    const avgPassRate =
      data.length > 0
        ? Math.round(
            (data.reduce((sum, p) => sum + (p.pass_rate ?? 0), 0) /
              data.length) *
              100,
          )
        : 0;
    const criticalFlaky = flaky.filter((f) => f.score >= 0.6).length;

    return {
      avgPassRate,
      totalRuns: data.length,
      scenariosPassed: passed,
      scenariosTotal: total,
      criticalFlaky,
    };
  });

  ngOnInit() {
    this.load();
  }

  setDays(v: string) {
    this.days.set(+v);
    this.load();
  }

  setFlakyFilter(f: "all" | "high" | "med" | "low") {
    this.flakyFilter.set(f);
  }

  onSearchFlaky(v: string) {
    this.searchFlaky.set(v);
  }

  /** Flakiness severity bucket from the 0..1 score, for colour-coding. */
  sev(score: number): "high" | "med" | "low" {
    return score >= 0.6 ? "high" : score >= 0.3 ? "med" : "low";
  }

  private load() {
    this.loading.set(true);
    this.api.trend(this.days()).subscribe({
      next: (d) => {
        this.data.set(d);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
    this.api.flakiness(this.days()).subscribe({
      next: (f) => this.flaky.set(f),
      error: () => this.flaky.set([]),
    });
    // Advisory predictions — the insight service is an optional deployment,
    // so any error just hides the card (never a page failure).
    this.insightApi.predictions().subscribe({
      next: (r) => this.predictions.set(r.predictions),
      error: () => this.predictions.set(null),
    });
  }

  yFor(rate: number): number {
    return this.PADT + (1 - rate) * (this.H - this.PADT - this.PADB);
  }

  readonly points = computed<PlotPoint[]>(() => {
    const d = this.data();
    if (!d.length) return [];
    const span = this.W - this.PADL - this.PADR;
    const step = d.length > 1 ? span / (d.length - 1) : 0;
    return d.map((p, i) => ({
      x: this.PADL + i * step,
      y: this.yFor(p.pass_rate ?? 0),
      p,
    }));
  });

  linePoints = computed(() =>
    this.points()
      .map((pt) => `${pt.x},${pt.y}`)
      .join(" "),
  );

  pct(r: number | null): string {
    return r === null ? "—" : `${Math.round(r * 100)}%`;
  }
}
