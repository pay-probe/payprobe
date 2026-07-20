import {
  Component,
  OnInit,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import {
  DiagnosticCheck,
  DiagnosticsReport,
  RunApiService,
} from "../run-monitor/run-api.service";
import { AssistantService } from "../assistant/assistant.service";
import { DiagnosticsStateService } from "./diagnostics-state.service";
import { EnvironmentInfo } from "../run-monitor/run.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { KpiCardComponent } from "../shared/ui/kpi-card.component";

/** Platform doctor — one layered pass over every hop that can break, with a
 *  concrete fix hint per failure. Answers "I have connection issues, where?"
 *  without visiting five pages: core service links, every saved connection
 *  (config → DNS → live probe), listener ports, and recent-run correlation. */
@Component({
  selector: "app-diagnostics",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    KpiCardComponent,
    RouterLink,
  ],
  template: `
    <div class="dg">
      <pp-page-header
        subtitle="Platform doctor — pinpoint the broken hop when something can't connect"
      >
        <label class="env">
          <span class="env__lbl">Overrides</span>
          <select
            class="env__sel"
            [value]="environment()"
            (change)="onEnv($event)"
          >
            <option value="">base config</option>
            @for (e of envs(); track e.name) {
              <option [value]="e.name">{{ e.label || e.name }}</option>
            }
          </select>
        </label>
        <pp-button
          variant="primary"
          size="sm"
          icon="activity"
          [disabled]="running()"
          (click)="run()"
        >
          {{
            running()
              ? "Running checks…"
              : report()
                ? "Re-run checks"
                : "Run checks"
          }}
        </pp-button>
      </pp-page-header>

      <div class="dg__body">
        @if (!report()) {
          @if (running()) {
            <div class="state">
              <span class="muted">Probing the platform…</span>
            </div>
          } @else {
            <div class="state state--empty">
              <span class="state__icon"
                ><pp-icon name="activity" [size]="26"
              /></span>
              <h4>Run a diagnosis</h4>
              <p class="muted">
                One pass checks core service links, every saved connection
                (config → DNS → live probe), simulator/participant listener
                ports, and correlates recent run failures — each problem comes
                with a concrete fix hint.
              </p>
            </div>
          }
        }
        @if (report(); as rep) {
          <!-- verdict -->
          <section class="verdict" [class]="'verdict--' + rep.status">
            <span class="verdict__icon">
              <pp-icon
                [name]="
                  rep.status === 'ok'
                    ? 'check'
                    : rep.status === 'failing'
                      ? 'x'
                      : 'alert-circle'
                "
                [size]="22"
              />
            </span>
            <div class="verdict__text">
              <h3>
                {{
                  rep.status === "ok"
                    ? "Everything reachable"
                    : rep.status === "failing"
                      ? "Broken links found"
                      : "Degraded — warnings found"
                }}
              </h3>
              <span class="muted">
                {{ rep.checks.length }} checks in {{ rep.duration_ms }} ms
                @if (ranAtLabel()) {
                  · as of {{ ranAtLabel() }}
                }
                @if (rep.environment) {
                  · overrides: <span class="mono">{{ rep.environment }}</span>
                }
              </span>
            </div>
            @if (state.history().length > 1) {
              <div class="hist" title="Recent verdicts, oldest first">
                @for (h of state.history(); track h.at) {
                  <span
                    class="hist__dot"
                    [attr.data-tone]="h.status"
                    [title]="histTitle(h)"
                  ></span>
                }
              </div>
            }
          </section>

          <section class="kpis">
            <pp-kpi-card
              label="Passed"
              [value]="count(rep, 'ok')"
              icon="check"
              caption="checks ok"
            />
            <pp-kpi-card
              label="Failed"
              [value]="count(rep, 'fail')"
              icon="x"
              caption="broken hops"
            />
            <pp-kpi-card
              label="Warnings"
              [value]="count(rep, 'warn')"
              icon="alert-circle"
              caption="needs a look"
            />
            <pp-kpi-card
              label="Skipped"
              [value]="count(rep, 'skip') + count(rep, 'disabled')"
              icon="info"
              caption="disabled / not applicable"
            />
          </section>

          <!-- problems first -->
          @if (problems().length) {
            <section class="block">
              <header class="block__head">
                <h3>What to fix</h3>
                <span class="muted">failures and warnings, worst first</span>
              </header>
              <div class="cards">
                @for (c of problems(); track c.id) {
                  <article
                    class="prob"
                    [class.prob--warn]="c.status === 'warn'"
                  >
                    <div class="prob__head">
                      <pp-badge [tone]="tone(c.status)">{{
                        c.status
                      }}</pp-badge>
                      <span class="prob__name">{{ c.name }}</span>
                      @if (c.target) {
                        <span class="mono muted">{{ c.target }}</span>
                      }
                      <pp-badge tone="neutral">{{ c.layer }}</pp-badge>
                    </div>
                    @if (c.error) {
                      <p class="prob__err mono">{{ c.error }}</p>
                    }
                    @if (c.hint) {
                      <p class="prob__hint">
                        <pp-icon name="sparkles" [size]="13" /> {{ c.hint }}
                      </p>
                    }
                    <button
                      class="link link--btn"
                      (click)="askAi(c)"
                      title="Open the assistant with this check's details as context"
                    >
                      <pp-icon name="sparkles" [size]="13" /> Ask AI
                    </button>
                    @if (c.layer === "connections") {
                      <a class="link" routerLink="/connections">
                        <pp-icon name="plug" [size]="13" /> Open Connections
                      </a>
                    } @else if (c.layer === "listeners") {
                      <a class="link" routerLink="/simulators">
                        <pp-icon name="server" [size]="13" /> Open Simulators
                      </a>
                    } @else if (c.layer === "runs" && c.runs?.length) {
                      <span class="runs">
                        @for (rid of c.runs; track rid) {
                          <a class="link" [routerLink]="['/reports', rid]">
                            <pp-icon name="clock" [size]="13" />
                            {{ rid.slice(0, 8) }}
                          </a>
                        }
                        <span class="muted"
                          >— open a run, then its Trace tab for the full
                          per-step error</span
                        >
                      </span>
                    }
                  </article>
                }
              </div>
            </section>
          }

          <!-- full check tree by layer -->
          @for (layer of layerOrder; track layer) {
            @if (byLayer()[layer]?.length) {
              <section class="block">
                <header class="block__head">
                  <h3>{{ layerTitles[layer] }}</h3>
                  <span class="muted">{{ layerCaptions[layer] }}</span>
                </header>
                <div class="tbl" role="table">
                  <div class="tr tr--head" role="row">
                    <span role="columnheader">Status</span>
                    <span role="columnheader">Check</span>
                    <span role="columnheader">Target</span>
                    <span role="columnheader">Latency</span>
                    <span role="columnheader">Result</span>
                  </div>
                  @for (c of byLayer()[layer]; track c.id) {
                    <div class="tr" role="row">
                      <span role="cell">
                        <pp-badge [tone]="tone(c.status)">{{
                          c.status
                        }}</pp-badge>
                      </span>
                      <span class="name" role="cell">{{ c.name }}</span>
                      <span class="mono muted" role="cell">{{
                        c.target || "—"
                      }}</span>
                      <span class="num" role="cell">
                        {{ c.latency_ms != null ? c.latency_ms + " ms" : "—" }}
                      </span>
                      <span class="res" role="cell">
                        @if (c.error) {
                          <span class="mono err">{{ c.error }}</span>
                        } @else {
                          <span class="muted">{{ c.detail || "ok" }}</span>
                        }
                      </span>
                    </div>
                  }
                </div>
              </section>
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
        background: var(--bg-base);
        color: var(--text-primary);
      }
      .dg {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .dg__body {
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
        font-size: var(--text-sm);
      }

      .env {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
      }
      .env__lbl {
        font-size: var(--text-xs);
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .env__sel {
        appearance: none;
        padding: var(--space-1-5) var(--space-3);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        background: var(--bg-surface);
        color: var(--text-primary);
        font-size: var(--text-sm);
      }

      /* verdict banner */
      .verdict {
        display: flex;
        align-items: center;
        gap: var(--space-4);
        padding: var(--space-4) var(--space-5);
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
        box-shadow: var(--shadow-xs);
      }
      .verdict h3 {
        margin: 0;
        font-size: var(--text-base);
      }
      .verdict__icon {
        display: grid;
        place-items: center;
        width: 44px;
        height: 44px;
        border-radius: var(--radius-full);
        flex: none;
      }
      .verdict--ok .verdict__icon {
        color: var(--color-success);
        background: color-mix(in srgb, var(--color-success) 14%, transparent);
      }
      .verdict--degraded .verdict__icon {
        color: var(--color-warning);
        background: color-mix(in srgb, var(--color-warning) 14%, transparent);
      }
      .verdict--failing .verdict__icon {
        color: var(--color-danger);
        background: color-mix(in srgb, var(--color-danger) 14%, transparent);
      }
      .hist {
        margin-left: auto;
        display: inline-flex;
        align-items: center;
        gap: 5px;
        flex: none;
      }
      .hist__dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: var(--border-strong);
      }
      .hist__dot[data-tone="ok"] {
        background: var(--color-success);
      }
      .hist__dot[data-tone="degraded"] {
        background: var(--color-warning);
      }
      .hist__dot[data-tone="failing"] {
        background: var(--color-danger);
      }

      .kpis {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: var(--space-4);
      }

      .block {
        display: flex;
        flex-direction: column;
        gap: var(--space-3);
      }
      .block__head {
        display: flex;
        align-items: baseline;
        gap: var(--space-3);
      }
      .block__head h3 {
        margin: 0;
        font-size: var(--text-base);
      }

      /* problem cards */
      .cards {
        display: flex;
        flex-direction: column;
        gap: var(--space-3);
      }
      .prob {
        display: flex;
        flex-direction: column;
        gap: var(--space-2);
        padding: var(--space-4);
        border: 1px solid
          color-mix(in srgb, var(--color-danger) 35%, var(--border));
        border-left: 3px solid var(--color-danger);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .prob--warn {
        border-color: color-mix(
          in srgb,
          var(--color-warning) 35%,
          var(--border)
        );
        border-left-color: var(--color-warning);
      }
      .prob__head {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        flex-wrap: wrap;
      }
      .prob__name {
        font-weight: var(--weight-semibold);
      }
      .prob__err {
        margin: 0;
        color: var(--color-danger);
        word-break: break-word;
      }
      .prob__hint {
        display: flex;
        align-items: flex-start;
        gap: var(--space-1-5);
        margin: 0;
        font-size: var(--text-sm);
        color: var(--text-primary);
      }
      .prob__hint pp-icon {
        color: var(--brand);
        margin-top: 2px;
        flex: none;
      }

      /* table */
      .tbl {
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        overflow: hidden;
        background: var(--bg-surface);
        box-shadow: var(--shadow-xs);
      }
      .tr {
        display: grid;
        grid-template-columns: 0.8fr 1.4fr 1.4fr 0.7fr 3fr;
        align-items: center;
        gap: var(--space-3);
        padding: var(--space-3) var(--space-4);
        border-top: 1px solid var(--border);
        font-size: var(--text-sm);
      }
      .tr--head {
        border-top: none;
        background: var(--bg-subtle);
        color: var(--text-muted);
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .tr:not(.tr--head):hover {
        background: var(--bg-subtle);
      }
      .name {
        font-weight: var(--weight-medium);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .num {
        font-variant-numeric: tabular-nums;
      }
      .res {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .err {
        color: var(--color-danger);
      }
      .runs {
        display: inline-flex;
        align-items: center;
        gap: var(--space-3);
        flex-wrap: wrap;
      }
      .link {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: var(--text-sm);
        color: var(--brand);
        text-decoration: none;
      }
      .link:hover {
        text-decoration: underline;
      }
      .link--btn {
        border: 0;
        background: transparent;
        padding: 0;
        font: inherit;
        font-size: var(--text-sm);
        cursor: pointer;
      }

      /* states */
      .state {
        padding: var(--space-6);
      }
      .state--empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        gap: var(--space-2);
        padding: var(--space-8) var(--space-6);
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .state--empty h4 {
        margin: 0;
        font-size: var(--text-base);
      }
      .state--empty p {
        max-width: 52ch;
        margin: 0;
      }
      .state__icon {
        display: grid;
        place-items: center;
        width: 52px;
        height: 52px;
        border-radius: var(--radius-full);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        margin-bottom: var(--space-1);
      }

      @media (max-width: 860px) {
        .kpis {
          grid-template-columns: 1fr 1fr;
        }
        .tr {
          grid-template-columns: 0.9fr 1.4fr 2fr;
        }
        .tr span:nth-child(n + 4) {
          display: none;
        }
      }
    `,
  ],
})
export class DiagnosticsComponent implements OnInit {
  private readonly api = inject(RunApiService);
  private readonly assistant = inject(AssistantService);
  /** Cached across navigation — the report is the service's, not the page's. */
  readonly state = inject(DiagnosticsStateService);

  readonly report = this.state.report;
  readonly running = this.state.running;
  readonly environment = this.state.environment;
  readonly envs = signal<EnvironmentInfo[]>([]);

  readonly layerOrder = ["services", "connections", "listeners", "runs"];
  readonly layerTitles: Record<string, string> = {
    services: "Core services",
    connections: "Connections",
    listeners: "Listeners",
    runs: "Recent runs",
  };
  readonly layerCaptions: Record<string, string> = {
    services: "orchestrator → scenario-service / auth / Redis / MCP",
    connections: "every saved connection: config → DNS → live probe",
    listeners: "simulator & participant ports actually accepting",
    runs: "connectivity-class failures in recent runs, correlated",
  };

  /** Open the assistant pre-seeded with one failing check (hidden context —
   *  the transcript just shows the question). */
  askAi(c: DiagnosticCheck): void {
    const context =
      `the user is on the Diagnostics page looking at a "${c.status}" check. ` +
      `Check: ${c.name} (layer: ${c.layer}` +
      `${c.target ? `, target: ${c.target}` : ""}). ` +
      `${c.detail ? `Detail: ${c.detail}. ` : ""}` +
      `${c.error ? `Error: ${c.error}. ` : ""}` +
      `${c.hint ? `Doctor's hint: ${c.hint}` : ""}`;
    this.assistant.askAbout(`Help me fix this: ${c.name}`, context);
  }

  /** Failures then warnings, in report order — the "what to fix" list. */
  readonly problems = computed(() => {
    const checks = this.report()?.checks ?? [];
    return [
      ...checks.filter((c) => c.status === "fail"),
      ...checks.filter((c) => c.status === "warn"),
    ];
  });

  readonly byLayer = computed(() => {
    const out: Record<string, DiagnosticCheck[]> = {};
    for (const c of this.report()?.checks ?? []) {
      (out[c.layer] ??= []).push(c);
    }
    return out;
  });

  ngOnInit() {
    this.api.environments().subscribe({
      next: (envs) => this.envs.set(envs ?? []),
      error: () => this.envs.set([]),
    });
    // first visit runs the doctor unprompted; revisits show the cached report
    if (!this.report() && !this.running()) this.state.run();
  }

  onEnv(ev: Event) {
    this.environment.set((ev.target as HTMLSelectElement).value);
    this.state.run(); // picking an override re-diagnoses with it
  }

  run() {
    this.state.run();
  }

  ranAtLabel(): string {
    return this.state.ranAt()?.toLocaleTimeString() ?? "";
  }

  histTitle(h: {
    at: Date;
    status: string;
    fails: number;
    warns: number;
  }): string {
    const bits = [h.at.toLocaleTimeString(), h.status];
    if (h.fails) bits.push(h.fails + " fail");
    if (h.warns) bits.push(h.warns + " warn");
    return bits.join(" · ");
  }

  count(rep: DiagnosticsReport, status: string): number {
    return rep.summary?.[status] ?? 0;
  }

  tone(status: string): "success" | "warning" | "danger" | "neutral" {
    if (status === "ok") return "success";
    if (status === "warn") return "warning";
    if (status === "fail") return "danger";
    return "neutral";
  }
}
