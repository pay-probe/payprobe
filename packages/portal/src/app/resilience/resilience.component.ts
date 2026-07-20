import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import {
  ChaosPhase,
  ResilienceRun,
  ResilienceRunCard,
  RunApiService,
  Simulator,
} from "../run-monitor/run-api.service";
import { LoadApiService, LoadRunListItem } from "../load/load-api.service";
import { UiService } from "../shared/ui.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";

/** A named storm the launcher can pick (matches the chaos-dial storms). */
interface StormProfile {
  key: string;
  label: string;
  hint: string;
  phases: ChaosPhase[];
  repeat: number;
}

const STORMS: StormProfile[] = [
  {
    key: "outage",
    label: "30s outage",
    hint: "total drop for 30s",
    repeat: 1,
    phases: [{ duration_s: 30, chaos: { drop_pct: 100 }, label: "outage" }],
  },
  {
    key: "brownout",
    label: "Brownout ramp",
    hint: "20s mild → 20s heavy latency",
    repeat: 1,
    phases: [
      {
        duration_s: 20,
        chaos: { latency_ms: { min: 100, max: 300, pct: 60 } },
        label: "brownout·mild",
      },
      {
        duration_s: 20,
        chaos: { latency_ms: { min: 400, max: 1200, pct: 90 } },
        label: "brownout·heavy",
      },
    ],
  },
  {
    key: "flap",
    label: "Flapping ×5",
    hint: "10s down / 10s up, five cycles",
    repeat: 5,
    phases: [
      { duration_s: 10, chaos: { drop_pct: 100 }, label: "down" },
      { duration_s: 10, chaos: {}, label: "up" },
    ],
  },
  {
    key: "lossy",
    label: "20% loss (30s)",
    hint: "drop 1 in 5 replies for 30s",
    repeat: 1,
    phases: [{ duration_s: 30, chaos: { drop_pct: 20 }, label: "lossy" }],
  },
];

const STAGE_ORDER = ["baseline", "storm", "recovery"];

/**
 * Resilience — chaos as a pass/fail certificate. Drive a load run through the
 * topology, unleash a chaos storm on a target simulator, and score whether the
 * client survived: availability held, faults absorbed via retry, recovery to
 * baseline, latency under a ceiling. The resilience analog of certification.
 */
@Component({
  selector: "app-resilience",
  standalone: true,
  imports: [
    FormsModule,
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    StaleChipComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="pg">
      <pp-page-header
        subtitle="Chaos + observation → a pass/fail resilience certificate"
      >
        <pp-stale-chip [health]="pollHealth" />
        <span class="hdr">Resilience</span>
      </pp-page-header>

      <div class="pg__body">
        <!-- launcher -->
        <section class="panel launch">
          <div class="panel__head"><h3>New resilience run</h3></div>
          <p class="muted sm intro">
            Start a load run first so real traffic is flowing, then pick the
            simulator to attack and a storm to throw at it. We watch a calm
            baseline, inject the storm, then check the client recovers.
          </p>

          <div class="grid">
            <label class="fld">
              <span class="fld__k">Target simulator</span>
              <select [(ngModel)]="targetSim" class="inp">
                <option value="">— choose a running simulator —</option>
                @for (s of sims(); track s.id) {
                  <option [value]="s.id">{{ simOpt(s) }}</option>
                }
              </select>
              @if (!sims().length) {
                <span class="fld__hint warn"
                  >No running simulators. Start one on the Simulators
                  page.</span
                >
              }
            </label>

            <label class="fld">
              <span class="fld__k">Load run to observe</span>
              <select [(ngModel)]="loadRun" class="inp">
                <option value="">— choose a running load run —</option>
                @for (l of runningLoads(); track l.run_id) {
                  <option [value]="l.run_id">{{ loadOpt(l) }}</option>
                }
              </select>
              @if (!runningLoads().length) {
                <span class="fld__hint warn"
                  >No running load runs. Start one on the Load Testing page,
                  then it appears here.</span
                >
              } @else {
                <span class="fld__hint muted"
                  >Only running load runs are listed — the resilience test
                  observes its live traffic.</span
                >
              }
            </label>

            <label class="fld">
              <span class="fld__k">Storm</span>
              <select [(ngModel)]="stormKey" class="inp">
                @for (st of storms; track st.key) {
                  <option [value]="st.key">
                    {{ st.label }} — {{ st.hint }}
                  </option>
                }
              </select>
            </label>

            <label class="fld fld--sm">
              <span class="fld__k">Baseline (s)</span>
              <input
                type="number"
                min="1"
                [(ngModel)]="baselineS"
                class="inp"
              />
            </label>
            <label class="fld fld--sm">
              <span class="fld__k">Recovery (s)</span>
              <input
                type="number"
                min="1"
                [(ngModel)]="recoveryS"
                class="inp"
              />
            </label>
          </div>

          <div class="launch__foot">
            <span class="muted sm">
              @if (launchHint()) {
                <pp-icon name="info" [size]="13" /> {{ launchHint() }}
              } @else {
                Est. duration ~{{ estDuration() }}s
              }
            </span>
            <pp-button
              variant="primary"
              icon="zap"
              [disabled]="!canLaunch() || launching()"
              (click)="launch()"
            >
              Run resilience test
            </pp-button>
          </div>
        </section>

        <!-- active / most recent report -->
        @if (active(); as run) {
          <section class="panel report">
            <div class="report__hd">
              <div class="report__title">
                <h3>{{ run.label || "Resilience run" }}</h3>
                <span class="muted sm mono"
                  >{{ run.target_sim_id }} · {{ run.load_run_id }}</span
                >
              </div>
              @if (run.status === "running") {
                <span class="stagepill">
                  <span class="stagepill__dot"></span>
                  {{ stageLabel(run.stage) }}
                </span>
                <pp-button
                  variant="ghost"
                  size="sm"
                  icon="x"
                  (click)="cancel(run.id)"
                  >Stop</pp-button
                >
              }
            </div>

            @if (run.report; as rep) {
              <div class="scoreband" [attr.data-verdict]="rep.verdict">
                <div class="grade">
                  <span class="grade__g">{{ rep.grade }}</span>
                  <span class="grade__s"
                    >{{ rep.score }}<span class="grade__o">/100</span></span
                  >
                </div>
                <div class="verdict">
                  <pp-badge
                    [tone]="rep.verdict === 'PASS' ? 'success' : 'danger'"
                    >{{ rep.verdict }}</pp-badge
                  >
                  <span class="muted sm">
                    baseline success {{ pct(rep.baseline_success_rate) }} · pass
                    bar {{ rep.thresholds["pass_score"] }}
                  </span>
                </div>
                <div class="components">
                  @for (c of componentRows(rep); track c.key) {
                    <div class="cmp">
                      <span class="cmp__k">{{ c.label }}</span>
                      <span class="cmp__track"
                        ><i
                          [style.width.%]="c.value"
                          [attr.data-lo]="c.value < 60"
                        ></i
                      ></span>
                      <span class="cmp__v">{{ c.value }}</span>
                    </div>
                  }
                </div>
              </div>

              <!-- gates -->
              <div class="gates">
                @for (g of rep.gates; track g.id) {
                  <div class="gate" [attr.data-pass]="g.passed">
                    <pp-icon [name]="g.passed ? 'check' : 'x'" [size]="14" />
                    <span class="gate__l">{{ g.label }}</span>
                    <span class="gate__v mono"
                      >{{ fmt(g.value) }} / {{ fmt(g.threshold) }}</span
                    >
                  </div>
                }
              </div>

              <!-- stage table -->
              <div class="stages">
                <div class="strow strow--hd">
                  <span>Stage</span><span class="num">Client OK</span
                  ><span class="num">Client err</span>
                  <span class="num">Success</span
                  ><span class="num">Faults</span>
                  <span class="num">Fault rate</span
                  ><span class="num">p95</span>
                </div>
                @for (st of stageRows(rep); track st.label) {
                  <div class="strow" [attr.data-stage]="st.label">
                    <span class="strow__l">{{ stageLabel(st.label) }}</span>
                    <span class="num">{{ st.client_received }}</span>
                    <span class="num">{{ st.client_errors }}</span>
                    <span class="num">{{ pct(st.success_rate) }}</span>
                    <span class="num">{{ st.sim_faults }}</span>
                    <span class="num">{{ pct(st.fault_rate) }}</span>
                    <span class="num">{{ st.p95_ms }}ms</span>
                  </div>
                }
              </div>

              <!-- timeline -->
              @if (run.samples.length > 1) {
                <div class="tl">
                  <div class="tl__lg">
                    <span><i class="lg lg--ok"></i> client success/s</span>
                    <span><i class="lg lg--flt"></i> injected faults/s</span>
                    <span class="tl__stages"
                      >baseline · <b>storm</b> · recovery</span
                    >
                  </div>
                  <svg
                    viewBox="0 0 600 120"
                    preserveAspectRatio="none"
                    class="tl__svg"
                  >
                    @for (b of stormBands(run); track b.x) {
                      <rect
                        [attr.x]="b.x"
                        y="0"
                        [attr.width]="b.w"
                        height="120"
                        class="tl__storm"
                      />
                    }
                    <polyline [attr.points]="okPath(run)" class="tl__ok" />
                    <polyline [attr.points]="fltPath(run)" class="tl__flt" />
                  </svg>
                </div>
              }

              @if (rep.notes.length) {
                <ul class="notes">
                  @for (n of rep.notes; track n) {
                    <li class="muted sm">{{ n }}</li>
                  }
                </ul>
              }
            } @else if (run.status === "failed") {
              <p class="err">Run failed: {{ run.error }}</p>
            } @else {
              <p class="muted sm running">
                Sampling {{ stageLabel(run.stage) }}… the report appears when
                the run completes.
              </p>
            }
          </section>
        }

        <!-- history -->
        <section class="panel">
          <div class="panel__head"><h3>Recent runs</h3></div>
          @if (!history().length) {
            <p class="muted sm empty">No resilience runs yet.</p>
          } @else {
            <div class="hist">
              @for (h of history(); track h.id) {
                <button
                  class="hrow"
                  [class.hrow--on]="selectedId() === h.id"
                  (click)="open(h.id)"
                >
                  @if (h.grade) {
                    <span class="hrow__grade" [attr.data-verdict]="h.verdict">{{
                      h.grade
                    }}</span>
                  } @else {
                    <span class="hrow__grade hrow__grade--pending">…</span>
                  }
                  <span class="hrow__l">{{ h.label || h.id }}</span>
                  <span class="hrow__st muted">{{ h.status }}</span>
                  @if (h.verdict) {
                    <pp-badge
                      [tone]="h.verdict === 'PASS' ? 'success' : 'danger'"
                      >{{ h.verdict }}</pp-badge
                    >
                  }
                </button>
              }
            </div>
          }
        </section>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .pg__body {
        padding: var(--space-5) var(--space-6);
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono);
      }
      .err {
        color: var(--color-danger);
      }
      .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }

      .panel {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-4);
        box-shadow: var(--shadow-xs);
      }
      .panel__head {
        margin-bottom: var(--space-2);
      }
      .panel__head h3 {
        margin: 0;
        font-size: var(--text-base);
      }
      .intro {
        margin: 0 0 var(--space-3);
        max-width: 74ch;
      }
      .empty {
        margin: var(--space-1) 0;
      }

      .grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-3);
      }
      @media (max-width: 900px) {
        .grid {
          grid-template-columns: 1fr;
        }
      }
      .fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .fld--sm {
        max-width: 160px;
      }
      .fld__k {
        font-size: var(--text-sm);
        color: var(--text-secondary);
      }
      .fld__hint {
        font-size: var(--text-xs);
      }
      .fld__hint.warn {
        color: var(--color-warning);
      }
      .inp {
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        background: var(--bg-base);
        color: var(--text-primary);
        padding: 6px var(--space-2);
        font-size: var(--text-sm);
      }
      .launch__foot {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-top: var(--space-3);
      }

      .report__hd {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        margin-bottom: var(--space-3);
      }
      .report__title h3 {
        margin: 0;
        font-size: var(--text-lg);
      }
      .stagepill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin-left: auto;
        font-size: var(--text-sm);
        color: var(--color-warning);
      }
      .stagepill__dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: currentColor;
        animation: res-pulse 1s ease-in-out infinite;
      }
      @keyframes res-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }

      .scoreband {
        display: grid;
        grid-template-columns: auto auto 1fr;
        gap: var(--space-4);
        align-items: center;
        padding: var(--space-3) var(--space-4);
        border-radius: var(--radius-lg);
        margin-bottom: var(--space-3);
        border: 1px solid var(--border);
      }
      .scoreband[data-verdict="PASS"] {
        background: color-mix(in srgb, var(--color-success) 8%, transparent);
        border-color: color-mix(
          in srgb,
          var(--color-success) 35%,
          var(--border)
        );
      }
      .scoreband[data-verdict="FAIL"] {
        background: color-mix(in srgb, var(--color-danger) 8%, transparent);
        border-color: color-mix(
          in srgb,
          var(--color-danger) 35%,
          var(--border)
        );
      }
      .grade {
        display: flex;
        flex-direction: column;
        align-items: center;
      }
      .grade__g {
        font-size: 40px;
        font-weight: var(--weight-bold);
        line-height: 1;
      }
      .grade__s {
        font-size: var(--text-sm);
        color: var(--text-secondary);
        font-variant-numeric: tabular-nums;
      }
      .grade__o {
        color: var(--text-muted);
      }
      .verdict {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .components {
        display: flex;
        flex-direction: column;
        gap: 5px;
        min-width: 0;
      }
      .cmp {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-xs);
      }
      .cmp__k {
        flex: none;
        width: 96px;
        color: var(--text-secondary);
        text-transform: capitalize;
      }
      .cmp__track {
        flex: 1;
        height: 6px;
        background: var(--bg-subtle);
        border-radius: 3px;
        overflow: hidden;
      }
      .cmp__track i {
        display: block;
        height: 100%;
        background: var(--color-success);
        border-radius: 3px;
      }
      .cmp__track i[data-lo="true"] {
        background: var(--color-warning);
      }
      .cmp__v {
        flex: none;
        min-width: 30px;
        text-align: right;
        font-variant-numeric: tabular-nums;
      }

      .gates {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: var(--space-2);
        margin-bottom: var(--space-3);
      }
      .gate {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
        padding: var(--space-2) var(--space-3);
        border-radius: var(--radius-md);
        border: 1px solid var(--border);
      }
      .gate[data-pass="true"] {
        color: var(--color-success);
        border-color: color-mix(
          in srgb,
          var(--color-success) 30%,
          var(--border)
        );
      }
      .gate[data-pass="false"] {
        color: var(--color-danger);
        border-color: color-mix(
          in srgb,
          var(--color-danger) 30%,
          var(--border)
        );
      }
      .gate__l {
        flex: 1;
        color: var(--text-primary);
      }
      .gate__v {
        color: var(--text-muted);
        font-size: var(--text-xs);
      }

      .stages {
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        overflow: hidden;
        margin-bottom: var(--space-3);
      }
      .strow {
        display: grid;
        grid-template-columns: 1.2fr repeat(6, 1fr);
        gap: var(--space-2);
        padding: var(--space-1-5) var(--space-3);
        font-size: var(--text-sm);
        border-bottom: 1px solid var(--border);
      }
      .strow:last-child {
        border-bottom: none;
      }
      .strow--hd {
        background: var(--bg-subtle);
        color: var(--text-muted);
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .strow[data-stage="storm"] {
        background: color-mix(in srgb, var(--color-warning) 7%, transparent);
      }
      .strow__l {
        text-transform: capitalize;
        font-weight: var(--weight-medium);
      }

      .tl {
        margin-bottom: var(--space-2);
      }
      .tl__lg {
        display: flex;
        gap: var(--space-4);
        font-size: var(--text-xs);
        color: var(--text-muted);
        margin-bottom: 4px;
      }
      .tl__lg span {
        display: inline-flex;
        align-items: center;
        gap: 5px;
      }
      .tl__stages {
        margin-left: auto;
      }
      .lg {
        width: 12px;
        height: 2px;
        display: inline-block;
      }
      .lg--ok {
        background: var(--color-success);
      }
      .lg--flt {
        background: var(--color-danger);
      }
      .tl__svg {
        width: 100%;
        height: 120px;
        background: var(--bg-base);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
      }
      .tl__storm {
        fill: color-mix(in srgb, var(--color-warning) 14%, transparent);
      }
      .tl__ok {
        fill: none;
        stroke: var(--color-success);
        stroke-width: 1.6;
        vector-effect: non-scaling-stroke;
      }
      .tl__flt {
        fill: none;
        stroke: var(--color-danger);
        stroke-width: 1.6;
        vector-effect: non-scaling-stroke;
      }

      .notes {
        margin: var(--space-2) 0 0;
        padding-left: var(--space-4);
      }
      .running {
        margin: var(--space-2) 0;
      }

      .hist {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .hrow {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        width: 100%;
        text-align: left;
        cursor: pointer;
        border: 1px solid transparent;
        background: transparent;
        border-radius: var(--radius-md);
        padding: var(--space-2) var(--space-3);
      }
      .hrow:hover {
        background: var(--bg-subtle);
      }
      .hrow--on {
        border-color: var(--border);
        background: var(--bg-subtle);
      }
      .hrow__grade {
        flex: none;
        width: 34px;
        height: 34px;
        display: grid;
        place-items: center;
        border-radius: 50%;
        font-weight: var(--weight-bold);
        font-size: var(--text-sm);
        border: 2px solid var(--border);
      }
      .hrow__grade[data-verdict="PASS"] {
        color: var(--color-success);
        border-color: var(--color-success);
      }
      .hrow__grade[data-verdict="FAIL"] {
        color: var(--color-danger);
        border-color: var(--color-danger);
      }
      .hrow__grade--pending {
        color: var(--text-muted);
      }
      .hrow__l {
        flex: 1;
        font-weight: var(--weight-medium);
      }
      .hrow__st {
        font-size: var(--text-sm);
        text-transform: capitalize;
      }
    `,
  ],
})
export class ResilienceComponent implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly loadApi = inject(LoadApiService);
  private readonly ui = inject(UiService);

  readonly storms = STORMS;

  readonly sims = signal<Simulator[]>([]);
  readonly loads = signal<LoadRunListItem[]>([]);
  readonly history = signal<ResilienceRunCard[]>([]);
  readonly active = signal<ResilienceRun | null>(null);
  readonly selectedId = signal<string | null>(null);
  readonly launching = signal(false);

  // launcher form
  targetSim = "";
  loadRun = "";
  stormKey = "outage";
  baselineS = 10;
  recoveryS = 15;

  readonly runningLoads = computed(() =>
    this.loads().filter((l) => l.status === "running"),
  );
  /** Plain methods (not computed): the form fields are ngModel-bound plain
   *  properties, not signals, so a computed() would never see them change. */
  canLaunch(): boolean {
    return !!this.targetSim && !!this.loadRun;
  }
  /** Why the launch button is disabled (empty when it's ready to go). */
  launchHint(): string {
    if (this.launching()) return "Starting…";
    if (!this.targetSim && !this.loadRun)
      return "Pick a target simulator and a load run to begin";
    if (!this.targetSim) return "Pick a target simulator";
    if (!this.loadRun) return "Pick a running load run to observe";
    return "";
  }

  readonly pollHealth = new PollHealth();
  private readonly listPoll = new PagePoll(3000, () => {
    this.refreshLists();
    this.refreshHistory();
  });
  private runPoll: PagePoll | null = null;

  ngOnInit() {
    this.listPoll.start(); // fires immediately, skips ticks while tab hidden
  }
  ngOnDestroy() {
    this.listPoll.stop();
    this.runPoll?.stop();
    this.pollHealth.destroy();
  }

  private refreshLists() {
    this.api
      .simulators()
      .subscribe({ next: (s) => this.sims.set(s), error: () => {} });
    this.loadApi
      .list()
      .subscribe({ next: (l) => this.loads.set(l), error: () => {} });
  }
  private refreshHistory() {
    this.api.resilienceRuns().subscribe({
      next: (h) => {
        this.history.set(h);
        this.pollHealth.markOk();
      },
      error: () => this.pollHealth.markError(),
    });
  }

  /** A distinguishable label for a simulator option: label + protocol + port + short id. */
  simOpt(s: Simulator): string {
    const port = s.port ? " :" + s.port : "";
    return `${s.label} · ${s.protocol}${port} · #${s.id}`;
  }
  /** A distinguishable label for a load-run option — labels often repeat, so
   *  lead with the short run id and include env + live TPS. */
  loadOpt(l: LoadRunListItem): string {
    const short = "#" + l.run_id.slice(0, 8);
    const env = l.environment ? " · " + l.environment : "";
    const tps =
      l.metrics?.tps != null ? ` · ${Math.round(l.metrics.tps)} tps` : "";
    return `${short} · ${l.label}${env}${tps}`;
  }

  estDuration(): number {
    const st = STORMS.find((s) => s.key === this.stormKey);
    const storm = st
      ? st.phases.reduce((a, p) => a + p.duration_s, 0) * st.repeat
      : 0;
    return Math.round(this.baselineS + storm + this.recoveryS);
  }

  launch() {
    const st = STORMS.find((s) => s.key === this.stormKey);
    if (!st || !this.canLaunch()) return;
    this.launching.set(true);
    this.api
      .startResilienceRun({
        target_sim_id: this.targetSim,
        load_run_id: this.loadRun,
        storm: { phases: st.phases, repeat: st.repeat },
        baseline_s: this.baselineS,
        recovery_s: this.recoveryS,
        label: `resilience · ${st.label}`,
      })
      .subscribe({
        next: (run) => {
          this.launching.set(false);
          this.ui.toast("success", "Resilience run started.");
          this.open(run.id);
          this.refreshHistory();
        },
        error: (e) => {
          this.launching.set(false);
          this.ui.toast(
            "error",
            e?.error?.detail || "Could not start resilience run.",
          );
        },
      });
  }

  open(id: string) {
    this.selectedId.set(id);
    this.runPoll?.stop();
    const tick = () =>
      this.api.resilienceRun(id).subscribe({
        next: (run) => {
          this.active.set(run);
          this.pollHealth.markOk();
          if (run.status !== "running" && this.runPoll) {
            this.runPoll.stop();
            this.runPoll = null;
            this.refreshHistory();
          }
        },
        error: () => this.pollHealth.markError(),
      });
    this.runPoll = new PagePoll(1500, tick);
    this.runPoll.start();
  }

  cancel(id: string) {
    this.api.cancelResilienceRun(id).subscribe({
      next: () => {
        this.ui.toast("success", "Resilience run stopped.");
        this.open(id);
      },
      error: () => this.ui.toast("error", "Could not stop the run."),
    });
  }

  // -- report helpers --------------------------------------------------------
  stageLabel(s?: string): string {
    switch (s) {
      case "baseline":
        return "Baseline";
      case "storm":
        return "Storm";
      case "recovery":
        return "Recovery";
      case "starting":
        return "Starting";
      case "done":
        return "Done";
      default:
        return s || "—";
    }
  }
  pct(v: number): string {
    return Math.round((v || 0) * 100) + "%";
  }
  fmt(v: number): string {
    return Number.isInteger(v) ? "" + v : (v || 0).toFixed(2);
  }
  componentRows(
    rep: ResilienceRun["report"],
  ): { key: string; label: string; value: number }[] {
    if (!rep) return [];
    const labels: Record<string, string> = {
      availability: "Availability",
      absorption: "Fault absorption",
      recovery: "Recovery",
      latency: "Latency",
    };
    return Object.keys(labels).map((k) => ({
      key: k,
      label: labels[k],
      value: rep.components[k] ?? 0,
    }));
  }
  stageRows(rep: ResilienceRun["report"]) {
    if (!rep) return [];
    return STAGE_ORDER.filter((s) => rep.stages[s]).map((s) => rep.stages[s]);
  }

  // -- timeline (per-second deltas of client success + injected faults) ------
  private deltas(run: ResilienceRun) {
    const s = run.samples;
    const ok: number[] = [];
    const flt: number[] = [];
    for (let i = 1; i < s.length; i++) {
      ok.push(Math.max(0, s[i].client_received - s[i - 1].client_received));
      flt.push(Math.max(0, s[i].sim_faults - s[i - 1].sim_faults));
    }
    return { ok, flt };
  }
  okPath(run: ResilienceRun): string {
    const { ok, flt } = this.deltas(run);
    return this.poly(ok, Math.max(1, ...ok, ...flt));
  }
  fltPath(run: ResilienceRun): string {
    const { ok, flt } = this.deltas(run);
    return this.poly(flt, Math.max(1, ...ok, ...flt));
  }
  private poly(vals: number[], max: number): string {
    if (!vals.length) return "";
    const n = Math.max(1, vals.length - 1);
    return vals
      .map(
        (v, i) =>
          `${((i / n) * 600).toFixed(1)},${(116 - (v / max) * 112).toFixed(1)}`,
      )
      .join(" ");
  }
  /** Shaded x-bands covering the storm-stage samples. */
  stormBands(run: ResilienceRun): { x: number; w: number }[] {
    const s = run.samples;
    if (s.length < 2) return [];
    const n = s.length - 1;
    const bands: { x: number; w: number }[] = [];
    let start = -1;
    for (let i = 0; i < s.length; i++) {
      const storm = s[i].stage === "storm";
      if (storm && start < 0) start = i;
      if ((!storm || i === s.length - 1) && start >= 0) {
        const end = storm ? i : i - 1;
        bands.push({ x: (start / n) * 600, w: ((end - start) / n) * 600 });
        start = -1;
      }
    }
    return bands;
  }
}
