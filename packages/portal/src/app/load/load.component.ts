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
import { FormsModule } from "@angular/forms";
import { Router, RouterLink } from "@angular/router";
import { Subscription } from "rxjs";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import { ScenarioSummary } from "../constructor/scenario.models";
import { RunApiService } from "../run-monitor/run-api.service";
import { RunMonitorService } from "../run-monitor/run-monitor.service";
import { EnvironmentInfo } from "../run-monitor/run.models";
import { ActiveEnvironmentService } from "../shared/active-environment.service";
import { UiService } from "../shared/ui.service";
import { IconComponent } from "../shared/ui/icon.component";
import {
  Diagnosis,
  LoadApiService,
  LoadProfileType,
  LoadRunListItem,
  LoadRunRequest,
  LoadSnapshot,
  ProvisionerInfo,
} from "./load-api.service";
import { LoadRunCardComponent, Point } from "./load-run-card.component";

/** A one-click load-shape preset. Selecting one sets the profile type and a set
 *  of sensible parameters; everything stays editable afterwards. */
interface LoadPreset {
  id: string;
  label: string;
  desc: string;
  icon: string;
  type: LoadProfileType;
  params: Partial<{
    targetTps: number;
    durationS: number;
    workers: number;
    startTps: number;
    endTps: number;
    rampS: number;
    baseTps: number;
    spikeTps: number;
    spikeEveryS: number;
    spikeDurationS: number;
    connections: number;
    heartbeatS: number;
  }>;
}

/** Fallback accent palette for scenarios with no `color` of their own, so every
 *  scenario still gets a stable, distinct chip color (picked by a hash of its id). */
const SCEN_PALETTE = [
  "#a371f7",
  "#58a6ff",
  "#3fb950",
  "#f0883e",
  "#d29922",
  "#ff7b72",
  "#79c0ff",
  "#d2a8ff",
  "#7ee787",
  "#ffa657",
];

const PRESETS: LoadPreset[] = [
  {
    id: "smoke",
    label: "Smoke",
    icon: "activity",
    type: "steady",
    desc: "Quick low-rate sanity check.",
    params: { targetTps: 50, durationS: 30, workers: 1 },
  },
  {
    id: "sustained",
    label: "Sustained",
    icon: "flow",
    type: "steady",
    desc: "Hold a steady target rate.",
    params: { targetTps: 2000, durationS: 300, workers: 1 },
  },
  {
    id: "stress",
    label: "Stress",
    icon: "up",
    type: "ramp",
    desc: "Ramp up to find the knee.",
    params: { startTps: 100, endTps: 5000, rampS: 120, workers: 2 },
  },
  {
    id: "spike",
    label: "Spike",
    icon: "zap",
    type: "spike",
    desc: "Baseline with periodic bursts.",
    params: {
      baseTps: 1000,
      spikeTps: 4000,
      spikeEveryS: 30,
      spikeDurationS: 5,
      durationS: 300,
      workers: 2,
    },
  },
  {
    id: "soak",
    label: "Soak",
    icon: "clock",
    type: "soak",
    desc: "Long endurance, many connections.",
    params: { connections: 2000, heartbeatS: 30, durationS: 0, workers: 2 },
  },
  {
    id: "custom",
    label: "Custom",
    icon: "settings",
    type: "steady",
    desc: "Full control over every knob.",
    params: {},
  },
];

/** One concurrently-running load run: its own live snapshot, time-series and
 *  control state, plus the stream subscription feeding them. Multiple of these
 *  stack on the page between the config form and the history table. */
class ActiveRun {
  readonly snapshot = signal<LoadSnapshot | null>(null);
  readonly points = signal<Point[]>([]);
  readonly stopping = signal(false);
  readonly retuning = signal(false);
  readonly retuneNote = signal<string | null>(null);
  retuneTps: number;
  readonly scaling = signal(false);
  readonly scaleNote = signal<string | null>(null);
  scaleTo: number;
  readonly copied = signal(false);
  readonly diagnosing = signal(false);
  readonly diagnosis = signal<Diagnosis | null>(null);
  sub?: Subscription;

  constructor(
    readonly runId: string,
    readonly label: string,
    readonly type: LoadProfileType,
    readonly workers: number,
    retuneSeed: number,
  ) {
    this.retuneTps = retuneSeed;
    this.scaleTo = Math.max(1, workers);
  }

  /** Workers reporting in / wanted, for the per-run fleet-health readout. */
  readonly fleetUp = computed(() => this.snapshot()?.workers_reporting ?? 0);
  readonly fleetWanted = computed(
    () => this.snapshot()?.workers ?? this.workers,
  );
  readonly elapsed = computed(() => this.snapshot()?.elapsed_s ?? 0);
}

/**
 * Load Test console — configure a profile at the top, launch it, and watch it
 * live in a stack of concurrent run cards below. Each run streams independently
 * off `load.sample`; you can start another at any time without stopping the
 * first. Finished runs drop into the History table, each linking to a detail
 * page.
 */
@Component({
  selector: "pp-load",
  standalone: true,
  imports: [
    IconComponent,
    CommonModule,
    FormsModule,
    RouterLink,
    LoadRunCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `
    <div class="load">
      <!-- ── 1. configuration (always available) ───────────────────────── -->
      <form class="card config" (ngSubmit)="launch()">
        <div class="config__body">
          <!-- presets: one-click load shapes -->
          <div class="presets">
            @for (p of presets; track p.id) {
              <button
                type="button"
                class="preset"
                [class.preset--on]="presetId === p.id"
                (click)="applyPreset(p)"
                [attr.data-tip]="p.desc"
                [attr.aria-label]="p.label + ': ' + p.desc"
              >
                <pp-icon [name]="p.icon" [size]="15" /> {{ p.label }}
              </button>
            }
          </div>

          <div class="config__cols">
            <div class="config__col">
              <h2>Workload</h2>
              <div class="pfield">
                <span class="pfield__l">Scenario</span>
                <div class="picker" [class.picker--open]="pickerOpen()">
                  <button
                    type="button"
                    class="picker__trigger"
                    (click)="togglePicker()"
                    [attr.aria-expanded]="pickerOpen()"
                  >
                    @if (selectedScenario; as sc) {
                      <span class="schip" [style.background]="chipBg(sc)">
                        <pp-icon
                          [name]="sc.icon || 'box'"
                          [size]="14"
                          [style.color]="scenarioColor(sc)"
                        />
                      </span>
                      <span class="picker__name">{{ sc.name }}</span>
                      <span class="picker__meta"
                        >{{ sc.step_count }} steps</span
                      >
                    } @else {
                      <span class="schip"
                        ><pp-icon name="layers" [size]="14"
                      /></span>
                      <span class="picker__name muted">Examples (default)</span>
                    }
                    <pp-icon name="chevron" [size]="14" class="picker__chev" />
                  </button>

                  @if (pickerOpen()) {
                    <div
                      class="picker__backdrop"
                      (click)="pickerOpen.set(false)"
                    ></div>
                    <div class="picker__panel" role="listbox">
                      <div class="picker__search">
                        <pp-icon name="search" [size]="14" />
                        <input
                          type="text"
                          placeholder="Search scenarios…"
                          autocomplete="off"
                          [ngModel]="scenarioQuery()"
                          (ngModelChange)="scenarioQuery.set($event)"
                          name="scenQuery"
                        />
                      </div>
                      <div class="picker__list">
                        <button
                          type="button"
                          class="picker__opt"
                          [class.picker__opt--on]="!scenarioId"
                          (click)="pickScenario(undefined)"
                        >
                          <span class="schip"
                            ><pp-icon name="layers" [size]="16"
                          /></span>
                          <span class="picker__opt-main">
                            <span class="picker__opt-name"
                              >Examples (default)</span
                            >
                            <span class="picker__opt-desc"
                              >The bundled example scenarios</span
                            >
                          </span>
                        </button>
                        @for (s of filteredScenarios(); track s.id) {
                          <button
                            type="button"
                            class="picker__opt"
                            [class.picker__opt--on]="scenarioId === s.id"
                            (click)="pickScenario(s.id)"
                          >
                            <span class="schip" [style.background]="chipBg(s)">
                              <pp-icon
                                [name]="s.icon || 'box'"
                                [size]="16"
                                [style.color]="scenarioColor(s)"
                              />
                            </span>
                            <span class="picker__opt-main">
                              <span class="picker__opt-name"
                                >{{ s.name }}
                                @if (s.category) {
                                  <span
                                    class="picker__cat"
                                    [style.color]="scenarioColor(s)"
                                    [style.background]="chipBg(s)"
                                    >{{ s.category }}</span
                                  >
                                }
                              </span>
                              @if (s.description) {
                                <span class="picker__opt-desc">{{
                                  s.description
                                }}</span>
                              }
                            </span>
                            <span class="picker__opt-steps">{{
                              s.step_count
                            }}</span>
                          </button>
                        } @empty {
                          <p class="picker__empty">
                            No scenarios match “{{ scenarioQuery() }}”.
                          </p>
                        }
                      </div>
                    </div>
                  }
                </div>
              </div>
              <label
                >Environment
                <select [(ngModel)]="envName" name="env">
                  <option [ngValue]="undefined">
                    — active{{ activeEnv() ? " · " + activeEnv() : "" }} —
                  </option>
                  @for (e of envs(); track e.name) {
                    <option [ngValue]="e.name">{{ e.label || e.name }}</option>
                  }
                </select>
              </label>
              <p class="hint">
                Overrides the active environment for this run only.
              </p>
            </div>

            <div class="config__col">
              <h2 class="profile__h">
                Profile <span class="profile__type">{{ type }}</span>
              </h2>

              <div class="pgrid">
                <label
                  >Workers
                  <input
                    type="number"
                    min="1"
                    [(ngModel)]="workers"
                    name="workers"
                  />
                </label>
                @if (type !== "soak") {
                  <label
                    >Duration <span class="unit">s</span>
                    <input
                      type="number"
                      min="1"
                      [(ngModel)]="durationS"
                      name="dur"
                    />
                  </label>
                }

                @if (type === "steady") {
                  <label class="span2"
                    >Target TPS <span class="unit">fleet-wide</span>
                    <input
                      type="number"
                      min="1"
                      [(ngModel)]="targetTps"
                      name="tps"
                    />
                  </label>
                }
                @if (type === "ramp") {
                  <label
                    >Start TPS<input
                      type="number"
                      [(ngModel)]="startTps"
                      name="s"
                  /></label>
                  <label
                    >End TPS<input type="number" [(ngModel)]="endTps" name="e"
                  /></label>
                  <label class="span2"
                    >Ramp <span class="unit">s</span
                    ><input type="number" [(ngModel)]="rampS" name="r"
                  /></label>
                }
                @if (type === "spike") {
                  <label
                    >Base TPS<input
                      type="number"
                      [(ngModel)]="baseTps"
                      name="b"
                  /></label>
                  <label
                    >Spike TPS<input
                      type="number"
                      [(ngModel)]="spikeTps"
                      name="sp"
                  /></label>
                  <label
                    >Every <span class="unit">s</span
                    ><input type="number" [(ngModel)]="spikeEveryS" name="ev"
                  /></label>
                  <label
                    >For <span class="unit">s</span
                    ><input
                      type="number"
                      [(ngModel)]="spikeDurationS"
                      name="sd"
                  /></label>
                }
                @if (type === "soak") {
                  <label
                    >Connections <span class="unit">fleet</span>
                    <input
                      type="number"
                      min="1"
                      [(ngModel)]="connections"
                      name="conn"
                  /></label>
                  <label
                    >Heartbeat <span class="unit">s</span>
                    <input
                      type="number"
                      min="1"
                      [(ngModel)]="heartbeatS"
                      name="hb"
                  /></label>
                  <label class="span2"
                    >Hold <span class="unit">s · 0 = until stopped</span>
                    <input
                      type="number"
                      min="0"
                      [(ngModel)]="durationS"
                      name="hold"
                  /></label>
                }
              </div>
            </div>

            <div class="config__col">
              <h2>Run plan</h2>
              <!-- live preview: what this config will actually do -->
              <p class="plan">{{ planSummary }}</p>

              <details class="adv">
                <summary>Advanced · drain &amp; backlog</summary>
                <div class="pgrid">
                  <label class="span2"
                    >Drain
                    <span class="unit">s · -1 wait for all · 0 cut now</span>
                    <input
                      type="number"
                      min="-1"
                      [(ngModel)]="drainS"
                      name="drain"
                    />
                  </label>
                  @if (type !== "soak") {
                    <label class="span2"
                      >Max backlog
                      <span class="unit"
                        >0 = cap at concurrency · -1 = unbounded</span
                      >
                      <input
                        type="number"
                        min="-1"
                        [(ngModel)]="maxBacklog"
                        name="backlog"
                      />
                    </label>
                  }
                </div>
              </details>
            </div>
          </div>
        </div>

        <div class="config__foot">
          <button type="submit" class="primary" [disabled]="busy()">
            {{
              busy()
                ? "Starting…"
                : activeRuns().length
                  ? "Start another load test"
                  : "Start load test"
            }}
          </button>
          @if (activeRuns().length) {
            <span class="muted small"
              >{{ activeRuns().length }} run{{
                activeRuns().length === 1 ? "" : "s"
              }}
              live</span
            >
          }
          @if (error()) {
            <span class="error">{{ error() }}</span>
          }
        </div>
      </form>

      <!-- ── 2. active runs stack ───────────────────────────────────────── -->
      @if (activeRuns().length) {
        <section class="stack">
          @for (run of activeRuns(); track run.runId) {
            <div class="card runcard">
              <div class="runcard__head">
                <span class="dot dot--live"></span>
                <strong class="runcard__label">{{ run.label }}</strong>
                <span class="runcard__prof">{{ run.type }}</span>
                <span class="runcard__elapsed">{{
                  fmtElapsed(run.elapsed())
                }}</span>
                <a class="linkbtn" [routerLink]="['/load', run.runId]">Open</a>
                <span class="runcard__spacer"></span>
                <button
                  type="button"
                  class="danger btn--sm"
                  (click)="stop(run)"
                  [disabled]="run.stopping()"
                >
                  {{ run.stopping() ? "Stopping…" : "Stop" }}
                </button>
              </div>

              <pp-load-run-card
                [snapshot]="run.snapshot()"
                [points]="run.points()"
                [type]="run.type"
                [workers]="run.workers"
                [running]="true"
                [diagnosing]="run.diagnosing()"
                [diagnosis]="run.diagnosis()"
                (diagnose)="diagnose(run)"
              />

              <!-- hot re-tune (steady / spike) -->
              @if (run.type === "steady" || run.type === "spike") {
                <div class="retune">
                  <label
                    >Re-tune target TPS
                    <input
                      type="number"
                      min="1"
                      [(ngModel)]="run.retuneTps"
                      [name]="'rt-' + run.runId"
                    />
                  </label>
                  <button
                    type="button"
                    class="btn btn--sm"
                    (click)="retune(run)"
                    [disabled]="run.retuning() || !run.retuneTps"
                  >
                    {{ run.retuning() ? "Applying…" : "Apply" }}
                  </button>
                  @if (run.retuneNote()) {
                    <span class="retune__note">{{ run.retuneNote() }}</span>
                  }
                </div>
              }

              <!-- fleet scaling -->
              <div
                class="fleet"
                [class.fleet--alert]="!!run.snapshot()?.notice"
              >
                <div class="fleet__head">
                  <strong>Fleet</strong>
                  @if (provisioner(); as p) {
                    <span
                      class="fleet__badge fleet__badge--{{
                        p.available ? 'on' : 'off'
                      }}"
                      [title]="p.detail || ''"
                    >
                      {{ p.available ? "auto · " + p.kind : "manual" }}
                    </span>
                  }
                  <span class="fleet__spacer"></span>
                  <span
                    class="fleet__health"
                    [class.good]="
                      run.fleetUp() >= run.fleetWanted() && run.fleetUp() > 0
                    "
                    [class.warn]="run.fleetUp() < run.fleetWanted()"
                    >{{ run.fleetUp() }}/{{ run.fleetWanted() }} workers
                    up</span
                  >
                </div>

                @if (run.snapshot()?.notice) {
                  <p class="fleet__cap">
                    <strong>Capped.</strong> The requested rate needs external
                    workers — add capacity to reach the target.
                  </p>
                }

                @if (provisioner()?.available) {
                  <div class="fleet__row">
                    <label
                      >Workers
                      <input
                        type="number"
                        min="1"
                        [(ngModel)]="run.scaleTo"
                        [name]="'sc-' + run.runId"
                      />
                    </label>
                    <button
                      type="button"
                      class="btn btn--sm"
                      (click)="scale(run)"
                      [disabled]="run.scaling() || !run.scaleTo"
                    >
                      {{ run.scaling() ? "Scaling…" : "Scale fleet" }}
                    </button>
                  </div>
                  @if (run.scaleNote()) {
                    <span class="retune__note">{{ run.scaleNote() }}</span>
                  }
                } @else {
                  <p class="fleet__hint">
                    No auto-provisioner available — start workers yourself, one
                    per shard:
                  </p>
                  <div class="fleet__cmdrow">
                    <code class="fleet__cmd">{{ manualCommand(run) }}</code>
                    <button
                      type="button"
                      class="btn btn--sm"
                      (click)="copyManualCmd(run)"
                    >
                      {{ run.copied() ? "Copied" : "Copy" }}
                    </button>
                  </div>
                }
              </div>
            </div>
          }
        </section>
      }

      <!-- ── 3. history (finished runs) ─────────────────────────────────── -->
      <div class="card recent">
        <div class="recent__head">
          <h2>History</h2>
          <span class="recent__actions">
            @if (hasStuck()) {
              <button
                class="linkbtn linkbtn--warn"
                [disabled]="reconciling()"
                (click)="reconcileStuck()"
                title="Close runs stuck as running after a restart"
              >
                {{ reconciling() ? "Reconciling…" : "Fix stuck runs" }}
              </button>
            }
          </span>
        </div>
        @if (finished().length === 0) {
          <p class="muted small">No finished load runs yet.</p>
        } @else {
          <div class="rt-wrap">
            <div class="rt">
              <div class="rt-head">
                <span></span>
                <span>Run</span>
                <span class="rt-num">TPS</span>
                <span class="rt-num">p95</span>
                <span class="rt-num">Success</span>
                <span class="rt-num">Errors</span>
                <span class="rt-num">Duration</span>
                <span>Environment</span>
                <span>When</span>
                <span>Status</span>
              </div>
              @for (r of finished(); track r.run_id) {
                <div class="rt-row" (click)="open(r)">
                  <span class="rt-c"
                    ><span class="rlc__dot rl-status--{{ r.status }}"></span
                  ></span>
                  <span class="rt-c rt-name">
                    @if (scenarioFor(r.label); as sc) {
                      <span
                        class="schip schip--sm"
                        [style.background]="chipBg(sc)"
                      >
                        <pp-icon
                          [name]="sc.icon || 'box'"
                          [size]="13"
                          [style.color]="scenarioColor(sc)"
                        />
                      </span>
                    } @else {
                      <span
                        class="schip schip--sm"
                        [style.background]="chipBg({ name: r.label })"
                      >
                        <pp-icon
                          name="box"
                          [size]="13"
                          [style.color]="scenarioColor({ name: r.label })"
                        />
                      </span>
                    }
                    <span class="rt-label">{{ r.label }}</span>
                    @if (r.metrics?.profile) {
                      <span class="rlc__prof">{{ r.metrics?.profile }}</span>
                    }
                  </span>
                  <span class="rt-c rt-num"
                    >{{ r.metrics?.tps != null ? fmt(r.metrics!.tps!) : "—"
                    }}<i>tps</i></span
                  >
                  <span class="rt-c rt-num"
                    >{{ r.metrics?.p95 != null ? fmt(r.metrics!.p95!) : "—"
                    }}<i>ms</i></span
                  >
                  <span
                    class="rt-c rt-num"
                    [class.good]="(r.metrics?.success_rate ?? 100) >= 99.5"
                    [class.warn]="
                      (r.metrics?.success_rate ?? 100) < 99.5 &&
                      (r.metrics?.success_rate ?? 100) >= 95
                    "
                    [class.bad]="(r.metrics?.success_rate ?? 100) < 95"
                    >{{
                      r.metrics?.success_rate != null
                        ? fmt(r.metrics!.success_rate!, 1) + "%"
                        : "—"
                    }}</span
                  >
                  <span
                    class="rt-c rt-num"
                    [class.bad]="(r.metrics?.errors || 0) > 0"
                    >{{
                      r.metrics?.errors != null ? fmt(r.metrics!.errors!) : "—"
                    }}</span
                  >
                  <span class="rt-c rt-num rt-mut">{{
                    r.metrics?.duration_s
                      ? fmtElapsed(r.metrics!.duration_s!)
                      : "—"
                  }}</span>
                  <span class="rt-c rt-env">{{ r.environment || "—" }}</span>
                  <span class="rt-c rt-mut rt-when">{{
                    r.created_at ? (r.created_at | date: "short") : "—"
                  }}</span>
                  <span class="rt-c"
                    ><span class="rlc__status rl-status--{{ r.status }}">{{
                      r.status
                    }}</span></span
                  >
                </div>
              }
            </div>
          </div>
        }
      </div>
    </div>
  `,
  styles: [
    `
      .load {
        padding: 1.25rem 1.5rem;
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }
      .muted {
        color: var(--pp-text-muted, #8a93a6);
      }
      .small {
        font-size: 0.78rem;
      }
      .card {
        background: var(--pp-surface, #fff);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 12px;
        padding: 0.8rem 0.9rem;
      }
      /* config form */
      .config {
        margin-top: 0;
        padding: 0.7rem 0.8rem;
      }
      .config__cols {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(215px, 1fr));
        gap: 0.7rem 1.3rem;
        align-items: start;
      }
      .config h2 {
        margin: 0 0 0.15rem;
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--pp-text-muted, #8a93a6);
      }
      .profile__h {
        display: flex;
        align-items: center;
        gap: 0.4rem;
      }
      .profile__type {
        font-size: 0.62rem;
        font-weight: 600;
        color: var(--pp-primary, #2f6fed);
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 12%,
          transparent
        );
        border-radius: 999px;
        padding: 0.05rem 0.4rem;
        letter-spacing: 0.02em;
      }
      .presets {
        display: grid;
        grid-template-columns: repeat(6, 1fr);
        gap: 6px;
        margin-bottom: 0.7rem;
      }
      @media (max-width: 820px) {
        .presets {
          grid-template-columns: repeat(3, 1fr);
        }
      }
      .preset {
        position: relative;
        display: flex;
        flex-direction: row;
        align-items: center;
        justify-content: center;
        gap: 5px;
        text-align: center;
        padding: 0.36rem 0.5rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 9px;
        background: var(--pp-surface, #fff);
        cursor: pointer;
        font: inherit;
        font-weight: 500;
        font-size: 0.78rem;
      }
      .preset:hover {
        border-color: var(--pp-primary, #2f6fed);
      }
      .preset--on {
        border-color: var(--pp-primary, #2f6fed);
        border-width: 1.5px;
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 7%,
          transparent
        );
      }
      /* whole-chip tooltip (replaces the separate info icon) */
      .preset[data-tip]::after {
        content: attr(data-tip);
        position: absolute;
        bottom: calc(100% + 8px);
        left: 50%;
        transform: translateX(-50%) translateY(3px);
        z-index: 20;
        width: max-content;
        max-width: 200px;
        padding: 0.4rem 0.55rem;
        border-radius: 8px;
        background: var(--pp-text, #1c2333);
        color: var(--pp-surface, #fff);
        font-size: 0.72rem;
        font-weight: 400;
        line-height: 1.35;
        white-space: normal;
        text-align: left;
        box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
        opacity: 0;
        pointer-events: none;
        transition:
          opacity 0.12s ease,
          transform 0.12s ease;
      }
      .preset[data-tip]:hover::after {
        opacity: 1;
        transform: translateX(-50%) translateY(0);
      }
      .config label {
        display: block;
        font-size: 0.72rem;
        color: var(--pp-text-muted, #8a93a6);
        margin: 0.4rem 0 0;
      }
      .config input,
      .config select {
        width: 100%;
        margin-top: 0.15rem;
        padding: 0.34rem 0.45rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 7px;
        background: var(--pp-surface-2, #fafbfc);
        color: inherit;
        font: inherit;
        font-size: 0.82rem;
      }
      .row {
        display: flex;
        gap: 0.5rem;
      }
      .row label {
        flex: 1;
      }
      /* compact profile grid */
      .pgrid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.3rem 0.6rem;
        align-items: end;
      }
      .pgrid label {
        margin: 0;
      }
      .pgrid .span2 {
        grid-column: 1 / -1;
      }
      .config .unit {
        font-weight: 400;
        font-size: 0.66rem;
        color: var(--pp-text-muted, #8a93a6);
        text-transform: none;
        letter-spacing: 0;
      }
      /* colored scenario icon chip (shared: picker + history) */
      .schip {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 22px;
        height: 22px;
        border-radius: 6px;
        background: var(--pp-surface-2, #f0f2f6);
        color: var(--pp-text-muted, #8a93a6);
        flex: none;
      }
      .schip--sm {
        width: 18px;
        height: 18px;
        border-radius: 5px;
      }
      /* scenario picker — searchable dropdown */
      .pfield {
        display: block;
      }
      .pfield__l {
        display: block;
        font-size: 0.72rem;
        color: var(--pp-text-muted, #8a93a6);
        margin: 0.4rem 0 0.15rem;
      }
      .picker {
        position: relative;
      }
      .picker__trigger {
        width: 100%;
        display: flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.34rem 0.45rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 7px;
        background: var(--pp-surface-2, #fafbfc);
        color: inherit;
        font: inherit;
        font-size: 0.82rem;
        cursor: pointer;
        text-align: left;
      }
      .picker--open .picker__trigger,
      .picker__trigger:hover {
        border-color: var(--pp-primary, #2f6fed);
      }
      .picker__name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
        min-width: 0;
      }
      .picker__name.muted {
        color: var(--pp-text-muted, #8a93a6);
      }
      .picker__meta {
        font-size: 0.68rem;
        color: var(--pp-text-muted, #8a93a6);
        flex: none;
      }
      .picker__chev {
        color: var(--pp-text-muted, #8a93a6);
        flex: none;
        transition: transform 0.14s ease;
      }
      .picker--open .picker__chev {
        transform: rotate(180deg);
      }
      .picker__backdrop {
        position: fixed;
        inset: 0;
        z-index: 39;
      }
      .picker__panel {
        position: absolute;
        top: calc(100% + 4px);
        left: 0;
        right: 0;
        z-index: 40;
        background: var(--pp-surface, #fff);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 9px;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.14);
        overflow: hidden;
      }
      .picker__search {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.4rem 0.55rem;
        border-bottom: 1px solid var(--pp-border, #e6e8ee);
        color: var(--pp-text-muted, #8a93a6);
      }
      .picker__search input {
        flex: 1;
        border: none;
        background: transparent;
        padding: 0.1rem 0;
        margin: 0;
        font: inherit;
        font-size: 0.82rem;
        color: var(--pp-text, #1c2333);
      }
      .picker__search input:focus {
        outline: none;
      }
      .picker__list {
        max-height: 280px;
        overflow-y: auto;
        padding: 0.25rem;
      }
      .picker__opt {
        width: 100%;
        display: flex;
        align-items: flex-start;
        gap: 0.5rem;
        padding: 0.4rem 0.5rem;
        border: none;
        border-radius: 7px;
        background: transparent;
        color: inherit;
        font: inherit;
        text-align: left;
        cursor: pointer;
      }
      .picker__opt:hover {
        background: var(--pp-surface-2, #f4f6fa);
      }
      .picker__opt--on {
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 10%,
          transparent
        );
      }
      .picker__opt > .schip {
        margin-top: 0.1rem;
      }
      .picker__opt-main {
        min-width: 0;
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 0.05rem;
      }
      .picker__opt-name {
        font-size: 0.82rem;
        display: flex;
        align-items: center;
        gap: 0.35rem;
      }
      .picker__cat {
        font-size: 0.6rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        font-weight: 600;
        color: var(--pp-primary, #2f6fed);
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 12%,
          transparent
        );
        border-radius: 999px;
        padding: 0.02rem 0.35rem;
      }
      .picker__opt-desc {
        font-size: 0.7rem;
        color: var(--pp-text-muted, #8a93a6);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 100%;
      }
      .picker__opt-steps {
        flex: none;
        align-self: center;
        font-size: 0.68rem;
        color: var(--pp-text-muted, #8a93a6);
        font-variant-numeric: tabular-nums;
      }
      .picker__opt-steps::after {
        content: " steps";
      }
      .picker__empty {
        margin: 0;
        padding: 0.6rem 0.55rem;
        font-size: 0.75rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      /* enrich: live run-plan preview */
      .plan {
        margin: 0.15rem 0 0;
        padding: 0.4rem 0.55rem;
        border-radius: 8px;
        background: color-mix(
          in srgb,
          var(--pp-primary, #2f6fed) 7%,
          transparent
        );
        border: 1px solid
          color-mix(in srgb, var(--pp-primary, #2f6fed) 22%, transparent);
        color: var(--pp-text, #1c2333);
        font-size: 0.74rem;
        line-height: 1.35;
        font-variant-numeric: tabular-nums;
      }
      /* compact: collapsible advanced knobs */
      .adv {
        margin-top: 0.55rem;
      }
      .adv > summary {
        cursor: pointer;
        list-style: none;
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        font-size: 0.66rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--pp-text-muted, #8a93a6);
        user-select: none;
      }
      .adv > summary::-webkit-details-marker {
        display: none;
      }
      .adv > summary::before {
        content: "▸";
        font-size: 0.66rem;
        transition: transform 0.12s ease;
      }
      .adv[open] > summary::before {
        transform: rotate(90deg);
      }
      .adv .pgrid {
        margin-top: 0.4rem;
      }
      .config__foot {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        margin-top: 0.7rem;
        padding-top: 0.6rem;
        border-top: 1px solid var(--pp-border, #e6e8ee);
      }
      .config__foot .primary {
        padding: 0.45rem 0.9rem;
        font-size: 0.85rem;
      }
      .hint {
        margin: 0.3rem 0 0;
        font-size: 0.68rem;
        line-height: 1.35;
        color: var(--pp-text-muted, #8a93a6);
      }
      button {
        border: none;
        border-radius: 8px;
        padding: 0.55rem 1rem;
        font-weight: 600;
        cursor: pointer;
      }
      button.primary {
        background: var(--pp-primary, #2f6fed);
        color: #fff;
      }
      button.danger {
        background: #e5484d;
        color: #fff;
      }
      button:disabled {
        opacity: 0.6;
        cursor: default;
      }
      .error {
        color: #e5484d;
        font-size: 0.85rem;
      }
      /* active-run stack */
      .stack {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
        gap: 0.85rem;
      }
      .runcard {
        padding: 0.65rem 0.7rem;
      }
      .runcard__head {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        padding-bottom: 0.45rem;
        margin-bottom: 0.1rem;
        border-bottom: 1px solid var(--pp-border, #e6e8ee);
      }
      .runcard__label {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 38%;
        font-size: 0.88rem;
      }
      .runcard__prof {
        font-size: 0.56rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--pp-text-muted, #8a93a6);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 5px;
        padding: 0.02rem 0.3rem;
      }
      .runcard__elapsed {
        font-size: 0.7rem;
        color: var(--pp-text-muted, #8a93a6);
        font-variant-numeric: tabular-nums;
      }
      .runcard__head .linkbtn {
        font-size: 0.72rem;
      }
      .runcard__head .btn--sm {
        padding: 0.28rem 0.6rem;
        font-size: 0.74rem;
      }
      .runcard__spacer {
        flex: 1;
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        display: inline-block;
        flex: none;
      }
      .dot--live {
        background: #30a46c;
        box-shadow: 0 0 0 3px rgba(48, 164, 108, 0.18);
      }
      .retune {
        margin-top: 0.6rem;
        display: flex;
        align-items: flex-end;
        gap: 0.45rem;
        flex-wrap: wrap;
      }
      .retune label {
        display: block;
        font-size: 0.72rem;
        color: var(--pp-text-muted, #8a93a6);
        flex: 1;
      }
      .retune input {
        width: 100%;
        margin-top: 0.15rem;
        padding: 0.36rem 0.45rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 8px;
        background: var(--pp-surface-2, #fafbfc);
        color: inherit;
        font: inherit;
      }
      .retune__note {
        font-size: 0.72rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .fleet {
        margin-top: 0.6rem;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.4rem 0.7rem;
        font-size: 0.8rem;
      }
      .fleet--alert {
        flex-direction: column;
        align-items: stretch;
        padding: 0.55rem 0.65rem;
        border: 1px solid rgba(245, 165, 36, 0.4);
        background: rgba(245, 165, 36, 0.07);
        border-radius: 8px;
      }
      .fleet__head {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        flex-basis: 100%;
      }
      .fleet__spacer {
        flex: 1;
      }
      .fleet__health {
        font-size: 0.72rem;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        color: var(--pp-text-muted, #8a93a6);
      }
      .fleet__health.good {
        color: #2a9d63;
      }
      .fleet__health.warn {
        color: #d98a0b;
      }
      .fleet__cap {
        flex-basis: 100%;
        margin: 0.15rem 0;
        font-size: 0.8rem;
        color: #d98a0b;
      }
      .fleet__row {
        display: flex;
        align-items: flex-end;
        gap: 0.5rem;
      }
      .fleet__row label {
        font-size: 0.78rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .fleet__row input {
        width: 90px;
        margin-top: 0.2rem;
        padding: 0.42rem 0.5rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 8px;
        background: var(--pp-surface-2, #fafbfc);
        color: inherit;
        font: inherit;
      }
      .fleet__cmdrow {
        flex-basis: 100%;
        display: flex;
        align-items: stretch;
        gap: 0.4rem;
      }
      .fleet__cmdrow .fleet__cmd {
        flex: 1 1 auto;
      }
      .fleet__badge {
        font-size: 0.7rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 0.1rem 0.4rem;
        border-radius: 6px;
      }
      .fleet__badge--on {
        background: rgba(64, 160, 96, 0.16);
        border: 1px solid rgba(64, 160, 96, 0.4);
      }
      .fleet__badge--off {
        background: rgba(140, 140, 140, 0.16);
        border: 1px solid rgba(140, 140, 140, 0.4);
      }
      .fleet__hint {
        flex-basis: 100%;
        margin: 0;
        font-size: 0.8rem;
        opacity: 0.8;
      }
      .fleet__cmd {
        flex-basis: 100%;
        display: block;
        padding: 0.45rem 0.6rem;
        border-radius: 6px;
        background: rgba(120, 120, 120, 0.12);
        font-size: 0.78rem;
        word-break: break-all;
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
      button.danger.btn--sm {
        background: #e5484d;
        color: #fff;
        border-color: #e5484d;
      }
      /* history */
      .recent__head {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
      }
      .recent__actions {
        display: inline-flex;
        align-items: baseline;
        gap: var(--space-3);
      }
      .recent__head h2 {
        margin: 0;
      }
      .linkbtn {
        border: none;
        background: transparent;
        color: var(--pp-primary, #2f6fed);
        cursor: pointer;
        font-size: 0.8rem;
        font-weight: 600;
        padding: 0;
        text-decoration: none;
      }
      .linkbtn--warn {
        color: var(--color-warning, #d98a0b);
      }
      .linkbtn:hover:not(:disabled) {
        text-decoration: underline;
      }
      .linkbtn:disabled {
        opacity: 0.6;
        cursor: default;
      }
      .rt-wrap {
        margin-top: 0.6rem;
        overflow-x: auto;
      }
      .rt {
        min-width: 760px;
      }
      .rt-head,
      .rt-row {
        display: grid;
        grid-template-columns:
          16px minmax(150px, 1.7fr) 66px 60px 70px 60px 76px
          minmax(72px, 0.9fr) 100px 76px;
        align-items: center;
        gap: 0.5rem;
      }
      .rt-head {
        padding: 0 0.6rem 0.35rem;
        font-size: 0.64rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--pp-text-muted, #8a93a6);
        border-bottom: 1px solid var(--pp-border, #e6e8ee);
      }
      .rt-row {
        padding: 0.5rem 0.6rem;
        border-bottom: 1px solid var(--pp-surface-2, #f1f3f7);
        cursor: pointer;
        font-size: 0.8rem;
        border-radius: 8px;
        transition: background 0.1s ease;
      }
      .rt-row:hover {
        background: var(--pp-surface-2, #fafbfc);
      }
      .rt-c {
        min-width: 0;
        display: flex;
        align-items: center;
        gap: 0.35rem;
      }
      .rt-num {
        justify-content: flex-end;
        text-align: right;
        font-variant-numeric: tabular-nums;
        font-weight: 600;
      }
      .rt-num i {
        font-style: normal;
        font-weight: 400;
        font-size: 0.66rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .rt-num.good {
        color: #2a9d63;
      }
      .rt-num.warn {
        color: #d98a0b;
      }
      .rt-num.bad {
        color: #e5484d;
      }
      .rt-mut {
        color: var(--pp-text-muted, #8a93a6);
        font-weight: 400;
      }
      .rt-name {
        gap: 0.4rem;
      }
      .rt-label {
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .rt-env,
      .rt-when {
        color: var(--pp-text-muted, #8a93a6);
        font-size: 0.74rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .rlc__dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex: none;
        background: var(--pp-text-muted, #8a93a6);
      }
      .rlc__dot.rl-status--running {
        background: #30a46c;
        box-shadow: 0 0 0 3px rgba(48, 164, 108, 0.18);
      }
      .rlc__dot.rl-status--failed {
        background: #e5484d;
      }
      .rlc__prof {
        font-size: 0.6rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--pp-text-muted, #8a93a6);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 5px;
        padding: 0.02rem 0.35rem;
        flex: none;
      }
      .rlc__status {
        font-size: 0.6rem;
        text-transform: uppercase;
        font-weight: 600;
        padding: 0.1rem 0.4rem;
        border-radius: 5px;
        opacity: 0.9;
        white-space: nowrap;
      }
      .rl-status--running {
        color: #30a46c;
        background: rgba(48, 164, 108, 0.12);
      }
      .rl-status--finished {
        color: #8a93a6;
        background: rgba(138, 147, 166, 0.08);
      }
      .rl-status--failed {
        color: #e5484d;
        background: rgba(229, 72, 77, 0.12);
      }
    `,
  ],
})
export class LoadComponent implements OnInit, OnDestroy {
  private readonly api = inject(LoadApiService);
  private readonly monitor = inject(RunMonitorService);
  private readonly runApi = inject(RunApiService);
  private readonly scenarioApi = inject(ScenarioApiService);
  private readonly router = inject(Router);
  private readonly activeEnvSvc = inject(ActiveEnvironmentService);
  private readonly ui = inject(UiService);
  /** The platform's active environment (used as the default target). */
  readonly activeEnv = this.activeEnvSvc.active;

  // config form state
  type: LoadProfileType = "steady";
  scenarioId?: string;
  // scenario picker (searchable dropdown) state
  readonly pickerOpen = signal(false);
  readonly scenarioQuery = signal("");
  /** The currently-selected scenario summary (undefined ⇒ examples default). */
  get selectedScenario(): ScenarioSummary | undefined {
    return this.scenarios().find((s) => s.id === this.scenarioId);
  }
  /** Scenarios filtered by the picker's search box (name / description /
   *  category / tags), case-insensitive. */
  readonly filteredScenarios = computed(() => {
    const q = this.scenarioQuery().trim().toLowerCase();
    const all = this.scenarios();
    if (!q) return all;
    return all.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q) ||
        (s.category || "").toLowerCase().includes(q) ||
        (s.tags || []).some((t) => t.toLowerCase().includes(q)),
    );
  });
  readonly presets = PRESETS;
  presetId = "sustained";
  envName?: string;
  workers = 1;
  durationS = 60;
  targetTps = 2000;
  startTps = 0;
  endTps = 2000;
  rampS = 60;
  baseTps = 1000;
  spikeTps = 2000;
  spikeEveryS = 30;
  spikeDurationS = 5;
  connections = 2000;
  heartbeatS = 30;
  // grace period (s) at end-of-run to let in-flight transactions finish.
  // >0 waits up to that long then aborts the rest; <0 waits for all; 0 = cut now.
  drainS = 30;
  // cap on outstanding txns per worker: 0 = cap at concurrency (recommended),
  // >0 = explicit, -1 = unbounded (legacy open-loop, inflates sent/pending).
  maxBacklog = 0;

  // page state
  readonly provisioner = signal<ProvisionerInfo | null>(null);
  readonly scenarios = signal<ScenarioSummary[]>([]);
  readonly envs = signal<EnvironmentInfo[]>([]);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly recent = signal<LoadRunListItem[]>([]);
  readonly reconciling = signal(false);

  /** Concurrent live runs shown in the stack. */
  readonly activeRuns = signal<ActiveRun[]>([]);

  /** History = runs that are no longer live (the active stack owns live ones). */
  readonly finished = computed(() =>
    this.recent().filter((r) => r.status !== "running"),
  );

  /** Any run still marked "running" server-side that we're NOT actively
   *  streaming — a candidate for the reconcile action. */
  readonly hasStuck = computed(() => {
    const owned = new Set(this.activeRuns().map((r) => r.runId));
    return this.recent().some(
      (r) => r.status === "running" && !owned.has(r.run_id),
    );
  });

  constructor() {
    this.scenarioApi.list().subscribe({
      next: (s) => this.scenarios.set(s),
      error: () => {},
    });
    this.runApi.environments().subscribe({
      next: (e) => this.envs.set(e),
      error: () => {},
    });
  }

  ngOnInit(): void {
    this.api.provisioner().subscribe({
      next: (p) => this.provisioner.set(p),
      error: () => this.provisioner.set(null),
    });
    // adopt any runs already live (e.g. after a page refresh) so they reappear
    // in the stack rather than being lost.
    this.api.list().subscribe({
      next: (r) => {
        this.recent.set(r);
        for (const item of r) {
          if (item.status === "running") this.adopt(item);
        }
      },
      error: () => {},
    });
  }

  ngOnDestroy(): void {
    for (const run of this.activeRuns()) run.sub?.unsubscribe();
  }

  private refreshRecent(): void {
    this.api.list().subscribe({
      next: (r) => this.recent.set(r),
      error: () => {},
    });
  }

  reconcileStuck(): void {
    this.reconciling.set(true);
    this.api.reconcile().subscribe({
      next: (res) => {
        this.reconciling.set(false);
        this.refreshRecent();
        this.ui.toast(
          "success",
          res.count
            ? `Reconciled ${res.count} stuck run${res.count === 1 ? "" : "s"}.`
            : "No stuck runs found.",
        );
      },
      error: () => {
        this.reconciling.set(false);
        this.ui.toast("error", "Could not reconcile stuck runs.");
      },
    });
  }

  applyPreset(p: LoadPreset): void {
    this.presetId = p.id;
    this.type = p.type;
    const q = p.params;
    if (q.targetTps != null) this.targetTps = q.targetTps;
    if (q.durationS != null) this.durationS = q.durationS;
    if (q.workers != null) this.workers = q.workers;
    if (q.startTps != null) this.startTps = q.startTps;
    if (q.endTps != null) this.endTps = q.endTps;
    if (q.rampS != null) this.rampS = q.rampS;
    if (q.baseTps != null) this.baseTps = q.baseTps;
    if (q.spikeTps != null) this.spikeTps = q.spikeTps;
    if (q.spikeEveryS != null) this.spikeEveryS = q.spikeEveryS;
    if (q.spikeDurationS != null) this.spikeDurationS = q.spikeDurationS;
    if (q.connections != null) this.connections = q.connections;
    if (q.heartbeatS != null) this.heartbeatS = q.heartbeatS;
  }

  open(item: LoadRunListItem): void {
    this.router.navigate(["/load", item.run_id]);
  }

  launch(): void {
    this.error.set(null);
    this.busy.set(true);

    const req: LoadRunRequest = {
      type: this.type,
      duration_s: this.durationS,
      workers: this.workers,
      scenario_ids: this.scenarioId ? [this.scenarioId] : undefined,
      environment_name: this.envName ?? this.activeEnv() ?? undefined,
      target_tps: this.targetTps,
      start_tps: this.startTps,
      end_tps: this.endTps,
      ramp_s: this.rampS,
      base_tps: this.baseTps,
      spike_tps: this.spikeTps,
      spike_every_s: this.spikeEveryS,
      spike_duration_s: this.spikeDurationS,
      connections: this.connections,
      heartbeat_interval_s: this.heartbeatS,
      drain_s: this.drainS,
      max_backlog_per_worker: this.maxBacklog,
    };
    const retuneSeed = this.type === "spike" ? this.spikeTps : this.targetTps;
    const label = this.presetId ? this.presetLabel() : this.type;

    this.api.start(req).subscribe({
      next: (res) => {
        this.busy.set(false);
        const run = new ActiveRun(
          res.run_id,
          label,
          this.type,
          this.workers,
          retuneSeed,
        );
        this.activeRuns.update((rs) => [...rs, run]);
        this.watch(run);
        this.refreshRecent();
      },
      error: (e) => {
        this.busy.set(false);
        this.error.set(this.startError(e));
      },
    });
  }

  /** Re-attach to a run reported as live by the server (page refresh). */
  private adopt(item: LoadRunListItem): void {
    if (this.activeRuns().some((r) => r.runId === item.run_id)) return;
    const type = (item.metrics?.profile as LoadProfileType) || "steady";
    const seed = item.metrics?.target_tps ?? item.metrics?.tps ?? 2000;
    const run = new ActiveRun(item.run_id, item.label, type, 1, seed);
    this.activeRuns.update((rs) => [...rs, run]);
    this.watch(run);
  }

  /** Open/close the scenario picker (resets the search on open). */
  togglePicker(): void {
    const next = !this.pickerOpen();
    if (next) this.scenarioQuery.set("");
    this.pickerOpen.set(next);
  }

  /** Select a scenario (or undefined for the examples default) and close. */
  pickScenario(id: string | undefined): void {
    this.scenarioId = id;
    this.pickerOpen.set(false);
  }

  /** A scenario's accent color: its own `color` if set, otherwise a stable one
   *  picked from a palette by hashing its id/name — so chips are always colored,
   *  even for scenarios with no color metadata. */
  scenarioColor(s: {
    id?: string;
    name?: string;
    color?: string | null;
  }): string {
    if (s.color) return s.color;
    const key = s.id || s.name || "";
    let h = 0;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
    return SCEN_PALETTE[h % SCEN_PALETTE.length];
  }

  /** A soft tint of a scenario's accent color for the icon chip background. */
  chipBg(s: { id?: string; name?: string; color?: string | null }): string {
    return `color-mix(in srgb, ${this.scenarioColor(s)} 16%, transparent)`;
  }

  /** Best-effort scenario summary for a finished run, matched by name, so the
   *  history can show the same colored icon as the picker. */
  scenarioFor(label: string): ScenarioSummary | undefined {
    return this.scenarios().find((s) => s.name === label);
  }

  /** The human label for the currently-selected preset (falls back to type). */
  private presetLabel(): string {
    return this.presets.find((p) => p.id === this.presetId)?.label ?? this.type;
  }

  /** A one-line preview of what the current config will do — an estimated
   *  transaction/connection count, the shape, fleet size, and drain policy — so
   *  the operator sees the consequence of the knobs before starting. */
  get planSummary(): string {
    const n = (x: number) => Math.round(Math.max(0, x || 0)).toLocaleString();
    const w = Math.max(1, this.workers || 1);
    const workers = `${w} worker${w > 1 ? "s" : ""}`;
    const drain =
      this.drainS < 0
        ? "drain: wait for all"
        : this.drainS === 0
          ? "no drain"
          : `drain ≤ ${this.drainS}s`;

    if (this.type === "soak") {
      const hold = this.durationS > 0 ? `${this.durationS}s` : "until stopped";
      return `≈ ${n(this.connections)} connections · heartbeat every ${this.heartbeatS}s · ${hold} · ${workers}`;
    }

    const dur = Math.max(0, this.durationS || 0);
    let avgTps = 0;
    let shape = "";
    if (this.type === "steady") {
      avgTps = this.targetTps;
      shape = `${n(this.targetTps)} TPS`;
    } else if (this.type === "ramp") {
      avgTps = (this.startTps + this.endTps) / 2;
      shape = `${n(this.startTps)} → ${n(this.endTps)} TPS over ${this.rampS}s`;
    } else if (this.type === "spike") {
      avgTps = this.baseTps;
      shape = `${n(this.baseTps)} TPS, spiking to ${n(this.spikeTps)} every ${this.spikeEveryS}s`;
    }
    return `≈ ${n(avgTps * dur)} transactions · ${shape} · ${dur}s · ${workers} · ${drain}`;
  }

  private startError(e: unknown): string {
    const err = e as { status?: number; error?: unknown } | null;
    const body = err?.error;
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail?: unknown }).detail;
      if (typeof d === "string" && d) return d;
    }
    if (typeof body === "string" && body.trim()) return body.trim();
    if (err?.status === 0) {
      return "Failed to start load run — the orchestrator is unreachable (is it running?).";
    }
    if (err?.status) {
      return `Failed to start load run (HTTP ${err.status}).`;
    }
    return "Failed to start load run.";
  }

  private watch(run: ActiveRun): void {
    run.sub?.unsubscribe();
    run.sub = this.monitor.stream(run.runId).subscribe({
      next: (ev) => {
        if (ev.type === "load.sample" || ev.type === "load.completed") {
          const s = ev.data as unknown as LoadSnapshot;
          run.snapshot.set(s);
          run.points.update((h) =>
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
          this.finish(run);
        }
      },
      error: () => this.finish(run),
      complete: () => this.finish(run),
    });
  }

  /** A run reached a terminal state: drop it from the stack, refresh history. */
  private finish(run: ActiveRun): void {
    run.sub?.unsubscribe();
    this.activeRuns.update((rs) => rs.filter((r) => r.runId !== run.runId));
    this.refreshRecent();
  }

  stop(run: ActiveRun): void {
    run.stopping.set(true);
    this.api.stop(run.runId).subscribe({
      error: (e) => {
        // 404 ⇒ already finished server-side; just clear it
        if (e?.status && e.status !== 404) {
          run.stopping.set(false);
          this.error.set("Stop failed — is the orchestrator running?");
          return;
        }
        this.finish(run);
      },
    });
    // safety net: never leave a run wedged if no terminal event arrives
    setTimeout(() => {
      if (
        run.stopping() &&
        this.activeRuns().some((r) => r.runId === run.runId)
      ) {
        this.finish(run);
      }
    }, 12000);
  }

  retune(run: ActiveRun): void {
    if (!run.retuneTps) return;
    run.retuning.set(true);
    run.retuneNote.set(null);
    const params =
      run.type === "spike"
        ? { spike_tps: run.retuneTps }
        : { target_tps: run.retuneTps };
    this.api.retune(run.runId, params).subscribe({
      next: () => {
        run.retuning.set(false);
        run.retuneNote.set(`target → ${run.retuneTps} TPS`);
      },
      error: (e) => {
        run.retuning.set(false);
        run.retuneNote.set(
          e?.status === 404 ? "run is no longer live" : "re-tune failed",
        );
      },
    });
  }

  scale(run: ActiveRun): void {
    if (!run.scaleTo) return;
    run.scaling.set(true);
    run.scaleNote.set(null);
    this.api.scale(run.runId, run.scaleTo).subscribe({
      next: (r) => {
        run.scaling.set(false);
        run.scaleNote.set(
          r.notice ?? `fleet → ${r.running ?? run.scaleTo} workers`,
        );
      },
      error: (e) => {
        run.scaling.set(false);
        run.scaleNote.set(
          e?.status === 404 ? "run is no longer live" : "scale failed",
        );
      },
    });
  }

  manualCommand(run: ActiveRun): string {
    const tpl = this.provisioner()?.manual_command_template ?? "";
    return tpl.replace("<RUN_ID>", run.runId);
  }

  copyManualCmd(run: ActiveRun): void {
    navigator.clipboard
      ?.writeText(this.manualCommand(run))
      .then(() => {
        run.copied.set(true);
        setTimeout(() => run.copied.set(false), 1500);
      })
      .catch(() => {});
  }

  diagnose(run: ActiveRun): void {
    run.diagnosing.set(true);
    this.api.diagnose(run.runId).subscribe({
      next: (d) => {
        run.diagnosis.set(d);
        run.diagnosing.set(false);
      },
      error: () => {
        run.diagnosing.set(false);
        this.error.set("Diagnosis failed — is the orchestrator running?");
      },
    });
  }

  fmt(v: number, dp = 0): string {
    return v.toLocaleString(undefined, {
      minimumFractionDigits: dp,
      maximumFractionDigits: dp,
    });
  }

  fmtElapsed(sec: number): string {
    const total = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(total / 60);
    const r = total % 60;
    return m ? `${m}m ${r}s` : `${r}s`;
  }
}
