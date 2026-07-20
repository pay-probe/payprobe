import { CommonModule, DatePipe } from "@angular/common";
import { FormsModule } from "@angular/forms";
import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { Router, RouterLink } from "@angular/router";

import { RunApiService } from "./run-api.service";
import { RunListItem, RunStatus } from "./run.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";
import { ScenarioApiService } from "../constructor/scenario-api.service";
import { Project, categoryPreset } from "../constructor/scenario.models";

@Component({
  selector: "app-run-list",
  standalone: true,
  imports: [
    PageHeaderComponent,
    CommonModule,
    DatePipe,
    RouterLink,
    FormsModule,
    IconComponent,
  ],
  template: `
    <div class="rl">
      <pp-page-header>
        <div subtitle>
          <p class="rl__sub">
            History of every executed run — open one to review per-case results.
          </p>
        </div>
        <div class="rl__actions">
          <input
            class="search-input"
            type="text"
            placeholder="Search runs…"
            [(ngModel)]="searchQuery"
            (input)="onSearch()"
          />
          <button class="btn" (click)="reload()" [disabled]="loading()">
            ↻ Refresh
          </button>
          <a routerLink="/run-monitor" class="btn btn--primary">+ New run</a>
        </div>
      </pp-page-header>

      @if (stats().total > 0) {
        <section class="stats-bar">
          <div class="stat-card">
            <span class="stat-label">Total runs</span>
            <span class="stat-value">{{ stats().total }}</span>
          </div>
          <div class="stat-card success">
            <span class="stat-label">Passed</span>
            <span class="stat-value">{{ stats().passed }}</span>
          </div>
          <div class="stat-card danger">
            <span class="stat-label">Failed</span>
            <span class="stat-value">{{ stats().failed }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Success rate</span>
            <span
              class="stat-value"
              [class.critical]="stats().successRate < 70"
            >
              {{ stats().successRate }}%
            </span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Last 24h</span>
            <span class="stat-value">{{ stats().last24h }}</span>
          </div>
        </section>
      }

      @if (error()) {
        <p class="rl__error">
          Could not reach the orchestrator. Is it running?
        </p>
      }

      @if (trends().length) {
        <section class="trends">
          <h2 class="trends__h">
            Suite trends
            <span class="muted">· recent pass/fail, newest right</span>
          </h2>
          @for (t of trends(); track t.label) {
            <div class="trend">
              <span class="trend__label" [title]="t.label">{{ t.label }}</span>
              <span class="trend__dots">
                @for (r of t.recent; track r.id) {
                  <a
                    class="dot"
                    [class]="'dot dot--' + cls(r.status)"
                    [routerLink]="['/reports', r.id]"
                    [title]="
                      r.status +
                      ' · ' +
                      (r.started_at
                        ? (r.started_at | date: 'short')
                        : r.id.slice(0, 8))
                    "
                  ></a>
                }
              </span>
              <span class="trend__rate" [class.bad]="t.passRate < 100"
                >{{ t.passRate }}%</span
              >
            </div>
          }
        </section>
      }

      <div class="tbl">
        <div class="tbl__head tbl__row tbl__row--runs">
          <span>Status</span><span>Run</span><span>Suite</span>
          <span>Project</span><span>Cases</span><span>Started</span>
          <span class="act">Actions</span>
        </div>
        @for (r of filteredRuns(); track r.id) {
          <div
            class="tbl__row tbl__row--runs"
            [style.border-left]="
              '3px solid ' + (projectBadge(r)?.color || 'transparent')
            "
          >
            <span
              ><span class="badge" [class]="'badge--' + cls(r.status)">{{
                r.status
              }}</span></span
            >
            <span class="mono">{{ r.id.slice(0, 8) }}</span>
            <span>{{ r.label || r.environment || "—" }}</span>
            <span>
              @if (projectBadge(r); as b) {
                <span class="proj" [style.--proj-color]="b.color">
                  <pp-icon [name]="b.icon" [size]="13"></pp-icon>
                  <span class="proj__name" [title]="b.name">{{ b.name }}</span>
                </span>
              } @else {
                <span class="muted">—</span>
              }
            </span>
            <span class="counts">
              <span class="ok">{{ r.scenarios.passed }}✓</span>
              <span class="bad">{{ r.scenarios.failed }}✗</span>
              <span class="muted">{{ r.scenarios.blocked }}⊘</span>
              <span class="muted">/ {{ r.scenarios.total }}</span>
            </span>
            <span class="muted">{{
              r.started_at ? (r.started_at | date: "short") : "—"
            }}</span>
            <span class="links act">
              <a [routerLink]="['/reports', r.id]">Report</a>
              <a [routerLink]="['/run-monitor', r.id]">Live</a>
            </span>
          </div>
        } @empty {
          <div class="tbl__empty">
            {{
              loading()
                ? "Loading…"
                : "No runs yet. Start one to populate the registry."
            }}
          </div>
        }
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .rl {
        padding: 1.5rem;
        margin: 0 auto;
        color: var(--color-text);
      }
      .rl__bar {
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        margin-bottom: 1rem;
        gap: 1rem;
      }
      .rl__back {
        color: var(--color-muted);
        text-decoration: none;
        font-size: 0.85rem;
      }
      .rl__title {
        margin: 0.25rem 0 0;
        font-size: 1.4rem;
      }
      .rl__sub {
        margin: 0.25rem 0 0;
        color: var(--color-muted);
        font-size: 0.88rem;
      }
      .rl__actions {
        display: flex;
        gap: 0.5rem;
        align-items: center;
      }
      .search-input {
        padding: 0.6rem 0.9rem;
        border-radius: 8px;
        border: 1px solid var(--color-border);
        background: var(--color-bg-soft);
        color: var(--color-text);
        font-size: 0.88rem;
        min-width: 200px;
      }
      .search-input::placeholder {
        color: var(--color-muted);
      }
      .stats-bar {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 1rem;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
      }
      .stat-card {
        padding: 1rem;
        border-radius: 10px;
        border: 1px solid var(--color-border);
        background: var(--color-card);
        display: flex;
        flex-direction: column;
        gap: 0.4rem;
      }
      .stat-card.success {
        border-color: var(--color-accent);
      }
      .stat-card.danger {
        border-color: var(--color-fail);
      }
      .stat-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        color: var(--color-muted);
        font-weight: 600;
      }
      .stat-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: var(--color-text);
      }
      .stat-card.success .stat-value {
        color: var(--color-accent);
      }
      .stat-card.danger .stat-value {
        color: var(--color-fail);
      }
      .stat-value.critical {
        color: var(--color-fail);
      }
      .btn {
        padding: 0.5rem 0.9rem;
        border-radius: 8px;
        border: 1px solid var(--color-border);
        background: var(--color-card);
        color: var(--color-text);
        cursor: pointer;
        font-weight: 600;
      }
      .btn--primary {
        background: var(--color-primary);
        color: var(--color-primary-contrast);
        border-color: transparent;
      }
      .btn:disabled {
        opacity: 0.6;
        cursor: default;
      }
      .rl__error {
        color: var(--color-fail);
      }
      .rl__table {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        overflow: hidden;
      }
      .row {
        display: grid;
        grid-template-columns: 90px 90px 1fr 160px 150px 120px;
        gap: 0.75rem;
        align-items: center;
        padding: 0.6rem 0.9rem;
        border-bottom: 1px solid var(--color-border);
        font-size: 0.88rem;
      }
      .row--head {
        background: var(--color-bg-soft);
        color: var(--color-muted);
        font-weight: 700;
        text-transform: uppercase;
        font-size: 0.72rem;
      }
      .row:last-child {
        border-bottom: none;
      }
      .mono {
        font-family: ui-monospace, monospace;
        color: var(--color-info);
      }
      .counts {
        display: flex;
        gap: 0.4rem;
      }
      .counts .ok {
        color: var(--color-accent);
      }
      .counts .bad {
        color: var(--color-fail);
      }
      .counts .muted,
      .muted {
        color: var(--color-muted);
      }
      .links {
        display: flex;
        gap: 0.7rem;
      }
      .links a {
        color: var(--color-primary);
        text-decoration: none;
      }
      .badge {
        padding: 0.12rem 0.55rem;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
      }
      .badge--run {
        background: color-mix(in srgb, var(--color-info) 18%, transparent);
        color: var(--color-info);
      }
      .badge--ok {
        background: color-mix(in srgb, var(--color-success) 18%, transparent);
        color: var(--color-success);
      }
      .badge--bad {
        background: color-mix(in srgb, var(--color-danger) 18%, transparent);
        color: var(--color-danger);
      }
      .badge--idle {
        background: var(--bg-subtle);
        color: var(--text-muted);
      }
      .proj {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        max-width: 100%;
        padding: 0.12rem 0.5rem 0.12rem 0.4rem;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        color: var(--proj-color, var(--color-muted));
        background: color-mix(
          in srgb,
          var(--proj-color, gray) 14%,
          transparent
        );
        border: 1px solid
          color-mix(in srgb, var(--proj-color, gray) 32%, transparent);
      }
      .proj pp-icon {
        flex: 0 0 auto;
        line-height: 0;
      }
      .proj__name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .rl__idle {
        padding: 1.2rem;
        color: var(--color-muted);
      }
      .trends {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 0.8rem 1rem;
        margin-bottom: 1.2rem;
        background: var(--color-card);
      }
      .trends__h {
        font-size: 1rem;
        margin: 0 0 0.7rem;
      }
      .trend {
        display: grid;
        grid-template-columns: 200px 1fr 52px;
        gap: 0.85rem;
        align-items: center;
        padding: 0.28rem 0;
      }
      .trend__label {
        font-size: 0.85rem;
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .trend__dots {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
      }
      .dot {
        width: 14px;
        height: 14px;
        border-radius: 3px;
        display: inline-block;
        background: var(--color-muted);
      }
      .dot--ok {
        background: var(--color-accent);
      }
      .dot--bad {
        background: var(--color-fail);
      }
      .dot--run {
        background: var(--color-info);
      }
      .trend__rate {
        font-size: 0.8rem;
        text-align: right;
        color: var(--color-accent);
        font-weight: 600;
      }
      .trend__rate.bad {
        color: var(--color-fail);
      }
    `,
  ],
})
export class RunListComponent implements OnInit {
  private readonly api = inject(RunApiService);
  private readonly scenarioApi = inject(ScenarioApiService);
  private readonly router = inject(Router);

  readonly runs = signal<RunListItem[]>([]);
  readonly projects = signal<Project[]>([]);
  readonly loading = signal(false);
  readonly starting = signal(false);
  readonly error = signal(false);
  readonly searchQuery = signal("");

  /** id → project, for O(1) badge lookup per run row. */
  readonly projectMap = computed(
    () => new Map(this.projects().map((p) => [p.id, p])),
  );

  readonly filteredRuns = computed(() => {
    const query = this.searchQuery().toLowerCase();
    if (!query) return this.runs();
    return this.runs().filter((r) => {
      const project = r.project_id
        ? this.projectMap().get(r.project_id)
        : undefined;
      return (
        r.id.toLowerCase().includes(query) ||
        (r.label && r.label.toLowerCase().includes(query)) ||
        (r.environment && r.environment.toLowerCase().includes(query)) ||
        (project?.name?.toLowerCase().includes(query) ?? false)
      );
    });
  });

  readonly stats = computed(() => {
    const runs = this.runs();
    const passed = runs.filter((r) => r.status === "passed").length;
    const failed = runs.filter(
      (r) => r.status === "failed" || r.status === "error",
    ).length;
    const now = new Date();
    const day = 24 * 60 * 60 * 1000;
    const last24h = runs.filter((r) => {
      if (!r.started_at) return false;
      const date = new Date(r.started_at);
      return now.getTime() - date.getTime() < day;
    }).length;
    return {
      total: runs.length,
      passed,
      failed,
      successRate:
        runs.length > 0 ? Math.round((passed / runs.length) * 100) : 0,
      last24h,
    };
  });

  /** Per-suite (label) trend: the last runs of each suite that has ≥2 runs,
   * oldest→newest, with a pass-rate. Built from the existing run list. */
  readonly trends = computed(() => {
    const byLabel = new Map<string, RunListItem[]>();
    for (const r of this.runs()) {
      const key = r.label || r.environment || "—";
      const arr = byLabel.get(key) ?? [];
      if (arr.length < 15) arr.push(r);
      byLabel.set(key, arr);
    }
    return [...byLabel.entries()]
      .filter(([, recent]) => recent.length >= 2)
      .map(([label, recent]) => {
        const passed = recent.filter((r) => r.status === "passed").length;
        return {
          label,
          recent: [...recent].reverse(),
          passRate: Math.round((passed / recent.length) * 100),
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  });

  ngOnInit(): void {
    this.reload();
    this.scenarioApi.projects().subscribe({
      next: (list) => this.projects.set(list),
      error: () => this.projects.set([]),
    });
  }

  /** The project chip (name + icon + color) for a run, or null when the run
   *  isn't tied to a project or the project is gone. Falls back to the
   *  category preset when the project has no stored color/icon. */
  projectBadge(
    run: RunListItem,
  ): { name: string; icon: string; color: string } | null {
    if (!run.project_id) return null;
    const p = this.projectMap().get(run.project_id);
    if (!p) return null;
    const preset = categoryPreset(p.category);
    return {
      name: p.name,
      icon: p.icon || preset?.icon || "layers",
      color: p.color || preset?.color || "#8b949e",
    };
  }

  onSearch(): void {
    // search is real-time via computed filteredRuns
  }

  reload(): void {
    this.loading.set(true);
    this.error.set(false);
    this.api.list().subscribe({
      next: (runs) => {
        this.runs.set(runs);
        this.loading.set(false);
      },
      error: () => {
        this.error.set(true);
        this.loading.set(false);
      },
    });
  }

  start(): void {
    this.starting.set(true);
    this.api.start({}).subscribe({
      next: (res) => {
        this.starting.set(false);
        this.router.navigate(["/run-monitor", res.run_id]);
      },
      error: () => this.starting.set(false),
    });
  }

  cls(status: RunStatus): string {
    if (status === "running" || status === "pending") return "run";
    if (status === "passed") return "ok";
    if (status === "failed" || status === "error" || status === "cancelled")
      return "bad";
    return "idle";
  }
}
