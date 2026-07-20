import { CommonModule } from "@angular/common";
import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { RunApiService, Schedule } from "../run-monitor/run-api.service";
import { UiService } from "../shared/ui.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface Draft {
  label: string;
  cadence: "interval" | "daily";
  interval_sec: number;
  daily_at: string;
  environment_name: string;
  project_id: string;
}

function blank(): Draft {
  return {
    label: "",
    cadence: "daily",
    interval_sec: 3600,
    daily_at: "06:00",
    environment_name: "mock",
    project_id: "",
  };
}

/** Scheduled regression runs — create/enable/run/delete over /schedules. */
@Component({
  selector: "app-schedules",
  standalone: true,
  imports: [PageHeaderComponent, CommonModule, FormsModule],
  template: `
    <div class="sc">
      <pp-page-header>
        <div subtitle>
          <span class="muted">Run regression suites unattended</span>
        </div>
        <button class="btn btn--primary" (click)="showNew.set(!showNew())">
          {{ showNew() ? "Close" : "+ New schedule" }}
        </button>
      </pp-page-header>

      <div class="sc__body">
        @if (showNew()) {
          <div class="form">
            <div class="grid">
              <label class="f"
                ><span>Label</span
                ><input [(ngModel)]="d.label" placeholder="nightly smoke"
              /></label>
              <label class="f"
                ><span>Environment</span
                ><input [(ngModel)]="d.environment_name" placeholder="mock"
              /></label>
              <label class="f"
                ><span>Project id (optional)</span
                ><input
                  [(ngModel)]="d.project_id"
                  placeholder="run all scenarios if blank"
              /></label>
              <label class="f"
                ><span>Cadence</span>
                <select [(ngModel)]="d.cadence">
                  <option value="daily">Daily at…</option>
                  <option value="interval">Every N seconds</option>
                </select>
              </label>
              @if (d.cadence === "daily") {
                <label class="f"
                  ><span>Time (UTC, HH:MM)</span
                  ><input [(ngModel)]="d.daily_at" placeholder="06:00"
                /></label>
              } @else {
                <label class="f"
                  ><span>Interval (seconds)</span
                  ><input type="number" [(ngModel)]="d.interval_sec"
                /></label>
              }
            </div>
            <button class="btn btn--primary" (click)="create()">
              Create schedule
            </button>
          </div>
        }

        @if (loading()) {
          <p class="muted">Loading…</p>
        } @else if (!schedules().length) {
          <p class="muted">No schedules yet.</p>
        } @else {
          <div class="schedules-grid">
            @for (s of schedules(); track s.id) {
              <div class="schedule-card" [class.paused]="!s.enabled">
                <div class="card-header">
                  <div class="card-title">
                    <h3>{{ s.label }}</h3>
                    <span class="card-id mono">{{ s.id | slice: 0 : 8 }}…</span>
                  </div>
                  <span class="status-pill" [class.on]="s.enabled">
                    {{ s.enabled ? "Enabled" : "Paused" }}
                  </span>
                </div>

                <div class="card-body">
                  <div class="info-row">
                    <span class="label">Cadence</span>
                    <span class="value">
                      {{
                        s.interval_sec
                          ? "Every " + s.interval_sec + "s"
                          : "Daily at " + s.daily_at + " UTC"
                      }}
                    </span>
                  </div>

                  <div class="info-row">
                    <span class="label">Next run</span>
                    <span class="value mono">
                      {{ s.next_run_at | slice: 0 : 16 }}
                    </span>
                  </div>

                  <div class="info-row">
                    <span class="label">Last run</span>
                    <span class="value mono">
                      {{
                        s.last_run_at
                          ? (s.last_run_at | slice: 0 : 16)
                          : "Never"
                      }}
                    </span>
                  </div>
                </div>

                <div class="card-actions">
                  <button class="btn btn--sm btn--primary" (click)="runNow(s)">
                    ▶ Run now
                  </button>
                  <button class="btn btn--sm" (click)="toggle(s)">
                    {{ s.enabled ? "⏸ Pause" : "▶ Resume" }}
                  </button>
                  <button class="btn btn--sm btn--danger" (click)="remove(s)">
                    🗑 Delete
                  </button>
                </div>
              </div>
            }
          </div>
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
      .sc {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .sc__bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .sc__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .sp {
        flex: 1 1 auto;
      }
      .sc__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 11px;
      }
      .form {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px;
        background: var(--color-node, var(--color-bg));
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px 14px;
      }
      .f {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
      }
      .f span {
        color: var(--color-text-muted, var(--color-text));
      }
      .f input,
      .f select {
        padding: 7px 9px;
        border: 1px solid var(--color-border);
        border-radius: 7px;
        background: var(--color-bg);
        color: var(--color-text);
        font: inherit;
        font-size: 13px;
      }
      .schedules-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 1rem;
      }
      .schedule-card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 1.25rem;
        background: var(--color-node, var(--color-bg));
        display: flex;
        flex-direction: column;
        gap: 1rem;
        transition: all 0.2s;
      }
      .schedule-card:hover {
        border-color: var(--color-primary);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
      }
      .schedule-card.paused {
        opacity: 0.75;
      }
      .card-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 0.75rem;
      }
      .card-title {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
      }
      .card-title h3 {
        margin: 0;
        font-size: 1.1rem;
        font-weight: 600;
      }
      .card-id {
        font-size: 0.75rem;
        color: var(--color-muted);
      }
      .status-pill {
        font-size: 11px;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid var(--color-border);
        color: var(--color-muted);
        white-space: nowrap;
      }
      .status-pill.on {
        color: var(--color-success, #2ea043);
        border-color: var(--color-success, #2ea043);
        background: color-mix(
          in srgb,
          var(--color-success, #2ea043) 10%,
          transparent
        );
      }
      .card-body {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }
      .info-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 0.5rem;
      }
      .info-row .label {
        font-size: 0.75rem;
        text-transform: uppercase;
        color: var(--color-muted);
        font-weight: 600;
      }
      .info-row .value {
        font-size: 0.88rem;
        color: var(--color-text);
      }
      .card-actions {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        padding-top: 0.5rem;
        border-top: 1px solid var(--color-border);
      }
      .btn {
        flex: 1;
        min-width: 80px;
      }
      .btn {
        padding: 7px 12px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
        background: var(--color-bg);
        color: var(--color-text);
        cursor: pointer;
        font: inherit;
        font-size: 13px;
      }
      .btn:hover {
        background: var(--color-node);
      }
      .btn--sm {
        padding: 4px 9px;
        font-size: 12px;
      }
      .btn--primary {
        background: var(--color-primary, #58a6ff);
        border-color: var(--color-primary, #58a6ff);
        color: #fff;
      }
      .btn--danger {
        color: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
        background: transparent;
      }
    `,
  ],
})
export class SchedulesComponent implements OnInit {
  private readonly api = inject(RunApiService);
  private readonly ui = inject(UiService);

  readonly schedules = signal<Schedule[]>([]);
  readonly loading = signal(true);
  readonly showNew = signal(false);
  d: Draft = blank();

  ngOnInit() {
    this.reload();
  }

  reload() {
    this.loading.set(true);
    this.api.schedules().subscribe({
      next: (s) => {
        this.schedules.set(s);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  create() {
    if (!this.d.label.trim()) {
      this.ui.toast("error", "Give the schedule a label.");
      return;
    }
    const request: Record<string, unknown> = {
      environment_name: this.d.environment_name,
    };
    if (this.d.project_id.trim())
      request["project_id"] = this.d.project_id.trim();
    const draft = {
      label: this.d.label.trim(),
      request,
      interval_sec: this.d.cadence === "interval" ? this.d.interval_sec : null,
      daily_at: this.d.cadence === "daily" ? this.d.daily_at : null,
    };
    this.api.createSchedule(draft).subscribe({
      next: () => {
        this.ui.toast("success", "Schedule created.");
        this.showNew.set(false);
        this.d = blank();
        this.reload();
      },
      error: () =>
        this.ui.toast("error", "Create failed — is the orchestrator running?"),
    });
  }

  runNow(s: Schedule) {
    this.api.runSchedule(s.id).subscribe({
      next: (r) => {
        this.ui.toast("success", `Started run ${r.run_id.slice(0, 8)}…`);
        this.reload();
      },
      error: () => this.ui.toast("error", "Could not start run."),
    });
  }

  toggle(s: Schedule) {
    this.api.setScheduleEnabled(s.id, !s.enabled).subscribe({
      next: () => this.reload(),
      error: () => this.ui.toast("error", "Update failed."),
    });
  }

  async remove(s: Schedule) {
    const ok = await this.ui.confirm(`Delete schedule “${s.label}”?`, {
      title: "Delete schedule",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    this.api.deleteSchedule(s.id).subscribe({
      next: () => {
        this.ui.toast("success", "Deleted.");
        this.reload();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }
}
