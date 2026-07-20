import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { ActivatedRoute, Router, RouterLink } from "@angular/router";
import { Subscription } from "rxjs";

import { RunApiService } from "./run-api.service";
import { RunMonitorService } from "./run-monitor.service";
import {
  CreateRunRequest,
  PhaseUpdate,
  RunStatus,
  StepResult,
} from "./run.models";
import { ScenarioApiService } from "../constructor/scenario-api.service";
import { UiService } from "../shared/ui.service";
import { NetworkFlowsService } from "../topologies/network-flows.service";
import {
  Project,
  ScenarioSummary,
  categoryPreset,
} from "../constructor/scenario.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";

interface PhaseCard {
  n: number;
  label: string;
  status: RunStatus | "pending";
  counts: PhaseUpdate | null;
}

/** A resolved project chip (name + icon + color) for badge rendering. */
interface Badge {
  name: string;
  icon: string;
  color: string;
}

interface MetricsSnapshot {
  totalSteps: number;
  passedSteps: number;
  failedSteps: number;
  blockedSteps: number;
  avgDuration: number;
  passRate: number;
  duration: number;
}

const PHASE_LABELS: Record<number, string> = {
  1: "Components",
  2: "Integration",
  3: "End-to-End",
};

@Component({
  selector: "app-run-monitor",
  standalone: true,
  imports: [PageHeaderComponent, RouterLink, IconComponent],
  template: `
    <div class="rm">
      <pp-page-header>
        <div subtitle>
          <p class="rm__sub">
            Launch a run and watch every step stream in live.
          </p>
        </div>
        @if (runId()) {
          <a routerLink="/run-monitor" class="btn">＋ New run</a>
          <a [routerLink]="['/reports', runId()]" class="btn btn--primary"
            >Full report →</a
          >
        }
      </pp-page-header>

      @if (runId()) {
        <!-- ── live run header ─────────────────────────────────────────── -->
        <section class="hero" [class]="'hero--' + statusClass()">
          <div class="hero__main">
            <span
              class="hero__status"
              [class]="'hero__status--' + statusClass()"
            >
              <span class="hero__pulse"></span>{{ status() }}
            </span>
            @if (runBadge(); as b) {
              <span class="proj" [style.--proj-color]="b.color">
                <pp-icon [name]="b.icon" [size]="14"></pp-icon>
                <span class="proj__name">{{ b.name }}</span>
              </span>
            } @else if (runLabel()) {
              <span class="hero__label">{{ runLabel() }}</span>
            }
            <code class="hero__id" [title]="runId()">{{
              runId()!.slice(0, 8)
            }}</code>
          </div>
          <div class="hero__side">
            <span class="hero__clock">
              <pp-icon name="clock" [size]="14"></pp-icon>
              {{ elapsedLabel() }}
            </span>
            <span class="hero__conn" [class.hero__conn--on]="connected()">
              {{ connected() ? "● live" : "○ offline" }}
            </span>
            @if (isRunning()) {
              <button
                class="btn btn--danger"
                (click)="cancel()"
                [disabled]="cancelling()"
              >
                {{ cancelling() ? "Cancelling…" : "■ Cancel" }}
              </button>
            }
          </div>
        </section>

        <!-- ── live progress ──────────────────────────────────────────── -->
        <section class="prog">
          <div class="prog__track">
            <div
              class="prog__fill"
              [class.prog__fill--indet]="isRunning() && progress() === 0"
              [class.prog__fill--run]="isRunning()"
              [style.width.%]="progress() || (isRunning() ? 8 : 0)"
            ></div>
          </div>
          <span class="prog__label">{{ progressLabel() }}</span>
        </section>

        <!-- ── phase stepper ──────────────────────────────────────────── -->
        <section class="phases">
          @for (p of phaseCards(); track p.n) {
            <div class="phase" [class]="'phase--' + p.status">
              <div class="phase__ico">
                <pp-icon [name]="phaseIcon(p.status)" [size]="16"></pp-icon>
              </div>
              <div class="phase__body">
                <div class="phase__label">{{ p.label }}</div>
                <div class="phase__meta">
                  @if (p.counts?.components !== undefined) {
                    <span class="muted"
                      >{{ p.counts!.components }} components</span
                    >
                  }
                  @if (p.counts?.total !== undefined) {
                    <span class="ok">{{ p.counts!.passed }}✓</span>
                    <span class="bad">{{ p.counts!.failed }}✗</span>
                    <span class="muted">{{ p.counts!.blocked }}⊘</span>
                  } @else {
                    <span class="phase__state">{{ p.status }}</span>
                  }
                </div>
              </div>
            </div>
          }
        </section>

        <!-- ── metric tiles ───────────────────────────────────────────── -->
        <section class="tiles">
          <div class="tile">
            <span class="tile__k">Pass rate</span>
            <span class="tile__v" [class.bad]="metrics().passRate < 70">
              {{ metrics().passRate }}%
            </span>
          </div>
          <div class="tile">
            <span class="tile__k">Steps</span>
            <span class="tile__v">{{ metrics().totalSteps }}</span>
          </div>
          <div class="tile">
            <span class="tile__k">Passed</span>
            <span class="tile__v ok">{{ metrics().passedSteps }}</span>
          </div>
          <div class="tile">
            <span class="tile__k">Failed</span>
            <span class="tile__v bad">{{ metrics().failedSteps }}</span>
          </div>
          <div class="tile">
            <span class="tile__k">Avg time</span>
            <span class="tile__v">{{ metrics().avgDuration }}<em>ms</em></span>
          </div>
        </section>

        <!-- ── step feed ──────────────────────────────────────────────── -->
        <section class="feedwrap">
          <div class="feedwrap__top">
            <h2 class="feedwrap__h">
              Step feed
              <span class="muted"
                >{{ filteredSteps().length }} of {{ steps().length }}</span
              >
            </h2>
            <div class="segs">
              <button
                class="seg"
                [class.seg--on]="filterStatus() === 'all'"
                (click)="filterStatus.set('all')"
              >
                All
              </button>
              <button
                class="seg seg--ok"
                [class.seg--on]="filterStatus() === 'passed'"
                (click)="filterStatus.set('passed')"
              >
                ✓ {{ metrics().passedSteps }}
              </button>
              <button
                class="seg seg--bad"
                [class.seg--on]="filterStatus() === 'failed'"
                (click)="filterStatus.set('failed')"
              >
                ✗ {{ metrics().failedSteps }}
              </button>
              <button
                class="seg seg--warn"
                [class.seg--on]="filterStatus() === 'blocked'"
                (click)="filterStatus.set('blocked')"
              >
                ⊘ {{ metrics().blockedSteps }}
              </button>
            </div>
          </div>
          <div class="feed">
            @for (s of filteredSteps(); track $index) {
              <div class="feed__row" [class]="'feed__row--' + s.status">
                <span class="feed__dot"></span>
                <span class="feed__tgt">{{ s.target }}</span>
                <span class="feed__act">{{ s.action }}</span>
                <span class="feed__step">{{ s.scenario }} · {{ s.step }}</span>
                <span class="feed__ms">{{ s.duration_ms }}ms</span>
                <span class="feed__st">{{ s.status }}</span>
              </div>
            } @empty {
              <p class="feed__idle">
                {{
                  steps().length > 0
                    ? "No steps match this filter."
                    : "Waiting for step results…"
                }}
              </p>
            }
          </div>
        </section>
      } @else {
        <!-- ── launcher ───────────────────────────────────────────────── -->
        <section class="launch">
          <div class="launch__hero">
            <div class="launch__heroico">
              <pp-icon name="play" [size]="22"></pp-icon>
            </div>
            <div>
              <h2 class="launch__h">Start a run</h2>
              <p class="launch__lede">
                Pick a project to run its whole suite, or fire a single case.
              </p>
            </div>
          </div>

          <div class="launch__pick">
            @if (selectedBadge(); as b) {
              <span class="proj proj--lg" [style.--proj-color]="b.color">
                <pp-icon [name]="b.icon" [size]="16"></pp-icon>
              </span>
            }
            <select
              class="launch__select"
              (change)="onProjectChange($any($event.target).value)"
            >
              @for (p of projects(); track p.id) {
                <option
                  [value]="p.id"
                  [selected]="p.id === selectedProjectId()"
                >
                  {{ p.name }} ({{ p.scenario_count }})
                </option>
              }
            </select>
            <button
              class="btn btn--primary"
              (click)="runProject()"
              [disabled]="!selectedProjectId() || starting()"
            >
              {{ starting() ? "Starting…" : "▶ Run project" }}
            </button>
          </div>

          <div class="launch__list">
            @for (s of projectScenarios(); track s.id) {
              <div class="case">
                @if (scenarioBadge(s); as b) {
                  <span class="case__ico" [style.color]="b.color">
                    <pp-icon [name]="b.icon" [size]="16"></pp-icon>
                  </span>
                } @else {
                  <span class="case__ico muted"
                    ><pp-icon name="beaker" [size]="16"></pp-icon
                  ></span>
                }
                <span class="case__name">{{ s.name }}</span>
                <span class="chip">{{ s.test_class }}</span>
                <span class="muted case__steps">{{ s.step_count }} steps</span>
                <button
                  class="btn btn--sm"
                  (click)="runScenario(s)"
                  [disabled]="starting()"
                >
                  ▶ Run
                </button>
              </div>
            } @empty {
              <p class="muted launch__none">
                {{
                  loadingList()
                    ? "Loading…"
                    : "No test cases in this project yet."
                }}
              </p>
            }
          </div>

          <button
            class="launch__mock"
            (click)="start()"
            [disabled]="starting()"
          >
            ▶ Run bundled mock examples
          </button>
        </section>
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .rm {
        padding: var(--space-6);
        color: var(--text-primary);
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
      }
      .rm__sub {
        margin: 0;
        color: var(--text-muted);
        font-size: var(--text-sm);
      }
      .muted {
        color: var(--text-muted);
      }
      .ok {
        color: var(--color-success);
      }
      .bad {
        color: var(--color-danger);
      }

      /* -- buttons -- */
      .btn {
        padding: 0.5rem 0.9rem;
        border-radius: var(--radius-sm);
        border: 1px solid var(--border);
        background: var(--bg-surface);
        color: var(--text-primary);
        cursor: pointer;
        font-weight: var(--weight-semibold);
        font-size: var(--text-base);
        text-decoration: none;
        transition:
          background var(--duration-fast, 0.15s) ease,
          border-color var(--duration-fast, 0.15s) ease;
      }
      .btn:hover:not(:disabled) {
        border-color: var(--border-strong);
        background: var(--bg-subtle);
      }
      .btn--primary {
        background: var(--brand);
        color: var(--on-brand);
        border-color: transparent;
      }
      .btn--primary:hover:not(:disabled) {
        background: var(--brand-hover);
        border-color: transparent;
      }
      .btn--danger {
        background: color-mix(in srgb, var(--color-danger) 14%, transparent);
        color: var(--color-danger);
        border-color: color-mix(in srgb, var(--color-danger) 40%, transparent);
      }
      .btn--danger:hover:not(:disabled) {
        background: color-mix(in srgb, var(--color-danger) 22%, transparent);
      }
      .btn--sm {
        padding: 0.32rem 0.7rem;
        font-size: var(--text-sm);
      }
      .btn:disabled {
        opacity: 0.55;
        cursor: default;
      }

      /* -- project chip -- */
      .proj {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        max-width: 220px;
        padding: 0.14rem 0.55rem 0.14rem 0.42rem;
        border-radius: var(--radius-pill);
        font-size: var(--text-sm);
        font-weight: var(--weight-semibold);
        color: var(--proj-color, var(--text-muted));
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
      .proj--lg {
        padding: 0.4rem;
        max-width: none;
      }

      /* -- live hero header -- */
      .hero {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--space-4);
        flex-wrap: wrap;
        padding: var(--space-4) var(--space-5);
        border: 1px solid var(--border);
        border-left: 4px solid var(--border-strong);
        border-radius: var(--radius-lg);
        background: var(--bg-surface);
        box-shadow: var(--shadow-sm);
      }
      .hero--run {
        border-left-color: var(--color-info);
      }
      .hero--ok {
        border-left-color: var(--color-success);
      }
      .hero--bad {
        border-left-color: var(--color-danger);
      }
      .hero__main,
      .hero__side {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        flex-wrap: wrap;
      }
      .hero__status {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.2rem 0.7rem;
        border-radius: var(--radius-pill);
        font-size: var(--text-sm);
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        background: var(--bg-subtle);
        color: var(--text-secondary);
      }
      .hero__status--run {
        background: color-mix(in srgb, var(--color-info) 16%, transparent);
        color: var(--color-info);
      }
      .hero__status--ok {
        background: color-mix(in srgb, var(--color-success) 16%, transparent);
        color: var(--color-success);
      }
      .hero__status--bad {
        background: color-mix(in srgb, var(--color-danger) 16%, transparent);
        color: var(--color-danger);
      }
      .hero__pulse {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: currentColor;
      }
      .hero__status--run .hero__pulse {
        animation: pulse 1.2s ease-in-out infinite;
      }
      @keyframes pulse {
        0%,
        100% {
          opacity: 1;
          transform: scale(1);
        }
        50% {
          opacity: 0.35;
          transform: scale(0.7);
        }
      }
      .hero__label {
        font-weight: var(--weight-medium);
      }
      .hero__id {
        font-family: var(--font-mono);
        font-size: var(--text-sm);
        color: var(--text-muted);
        background: var(--bg-subtle);
        padding: 0.1rem 0.45rem;
        border-radius: var(--radius-sm);
      }
      .hero__clock {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        font-family: var(--font-mono);
        font-size: var(--text-base);
        color: var(--text-secondary);
      }
      .hero__conn {
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .hero__conn--on {
        color: var(--color-success);
      }

      /* -- progress -- */
      .prog {
        display: flex;
        align-items: center;
        gap: var(--space-3);
      }
      .prog__track {
        flex: 1;
        height: 8px;
        border-radius: var(--radius-pill);
        background: var(--bg-subtle);
        overflow: hidden;
      }
      .prog__fill {
        height: 100%;
        border-radius: var(--radius-pill);
        background: var(--color-success);
        transition: width 0.4s ease;
      }
      .prog__fill--run {
        background: var(--color-info);
      }
      .prog__fill--indet {
        width: 30% !important;
        animation: indet 1.3s ease-in-out infinite;
      }
      @keyframes indet {
        0% {
          margin-left: -30%;
        }
        100% {
          margin-left: 100%;
        }
      }
      .prog__label {
        font-size: var(--text-sm);
        color: var(--text-muted);
        min-width: 120px;
        text-align: right;
      }

      /* -- phase stepper -- */
      .phases {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: var(--space-3);
      }
      .phase {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        padding: var(--space-3) var(--space-4);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-surface);
      }
      .phase__ico {
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        border-radius: 50%;
        background: var(--bg-subtle);
        color: var(--text-muted);
        flex: 0 0 auto;
      }
      .phase--passed {
        border-color: color-mix(
          in srgb,
          var(--color-success) 45%,
          var(--border)
        );
      }
      .phase--passed .phase__ico {
        background: color-mix(in srgb, var(--color-success) 18%, transparent);
        color: var(--color-success);
      }
      .phase--failed {
        border-color: color-mix(
          in srgb,
          var(--color-danger) 45%,
          var(--border)
        );
      }
      .phase--failed .phase__ico {
        background: color-mix(in srgb, var(--color-danger) 18%, transparent);
        color: var(--color-danger);
      }
      .phase--partial,
      .phase--running {
        border-color: color-mix(in srgb, var(--color-info) 45%, var(--border));
      }
      .phase--running .phase__ico {
        background: color-mix(in srgb, var(--color-info) 18%, transparent);
        color: var(--color-info);
      }
      .phase__label {
        font-size: var(--text-md);
        font-weight: var(--weight-semibold);
      }
      .phase__meta {
        display: flex;
        gap: 0.5rem;
        font-size: var(--text-sm);
        margin-top: 2px;
      }
      .phase__state {
        text-transform: capitalize;
        color: var(--text-muted);
      }

      /* -- metric tiles -- */
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: var(--space-3);
      }
      .tile {
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
        padding: var(--space-4);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-surface);
      }
      .tile__k {
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--text-muted);
        font-weight: var(--weight-semibold);
      }
      .tile__v {
        font-size: var(--text-2xl);
        font-weight: 700;
        color: var(--text-primary);
      }
      .tile__v em {
        font-size: var(--text-base);
        font-style: normal;
        color: var(--text-muted);
        font-weight: var(--weight-medium);
      }
      .tile__v.ok {
        color: var(--color-success);
      }
      .tile__v.bad {
        color: var(--color-danger);
      }

      /* -- step feed -- */
      .feedwrap__top {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--space-3);
        flex-wrap: wrap;
        gap: var(--space-3);
      }
      .feedwrap__h {
        font-size: var(--text-lg);
        margin: 0;
        display: flex;
        align-items: baseline;
        gap: 0.5rem;
      }
      .feedwrap__h .muted {
        font-size: var(--text-sm);
        font-weight: var(--weight-regular);
      }
      .segs {
        display: inline-flex;
        gap: 2px;
        padding: 3px;
        background: var(--bg-subtle);
        border-radius: var(--radius-md);
      }
      .seg {
        padding: 0.3rem 0.7rem;
        border-radius: var(--radius-sm);
        border: none;
        background: transparent;
        color: var(--text-secondary);
        cursor: pointer;
        font-size: var(--text-sm);
        font-weight: var(--weight-semibold);
        transition: background 0.15s ease;
      }
      .seg:hover {
        color: var(--text-primary);
      }
      .seg--on {
        background: var(--bg-surface);
        color: var(--text-primary);
        box-shadow: var(--shadow-sm);
      }
      .seg--ok.seg--on {
        color: var(--color-success);
      }
      .seg--bad.seg--on {
        color: var(--color-danger);
      }
      .seg--warn.seg--on {
        color: var(--color-warning);
      }
      .feed {
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        overflow: hidden;
        background: var(--surface-code);
      }
      .feed__row {
        display: grid;
        grid-template-columns: 12px 120px 150px 1fr 72px 74px;
        gap: 0.6rem;
        align-items: center;
        padding: 0.5rem 0.8rem;
        border-bottom: 1px solid
          color-mix(in srgb, var(--code-text) 10%, transparent);
        font-family: var(--font-mono);
        font-size: var(--text-sm);
        color: var(--code-text);
      }
      .feed__row:last-child {
        border-bottom: none;
      }
      .feed__row--failed {
        background: color-mix(in srgb, var(--color-danger) 8%, transparent);
      }
      .feed__dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--text-muted);
      }
      .feed__row--passed .feed__dot {
        background: var(--color-success);
      }
      .feed__row--failed .feed__dot {
        background: var(--color-danger);
      }
      .feed__row--blocked .feed__dot {
        background: var(--color-warning);
      }
      .feed__tgt {
        color: #8ab4ff;
      }
      .feed__act {
        color: var(--code-text);
      }
      .feed__step {
        color: color-mix(in srgb, var(--code-text) 70%, transparent);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .feed__ms {
        color: color-mix(in srgb, var(--code-text) 60%, transparent);
        text-align: right;
      }
      .feed__st {
        text-transform: uppercase;
        font-weight: 700;
        font-size: var(--text-xs);
        text-align: right;
      }
      .feed__row--passed .feed__st {
        color: var(--color-success);
      }
      .feed__row--failed .feed__st {
        color: var(--color-danger);
      }
      .feed__row--blocked .feed__st {
        color: var(--color-warning);
      }
      .feed__idle {
        color: color-mix(in srgb, var(--code-text) 60%, transparent);
        padding: var(--space-5);
        text-align: center;
        font-family: var(--font-mono);
        font-size: var(--text-sm);
      }

      /* -- launcher -- */
      .launch {
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-6);
        background: var(--bg-surface);
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
        box-shadow: var(--shadow-sm);
      }
      .launch__hero {
        display: flex;
        align-items: center;
        gap: var(--space-4);
      }
      .launch__heroico {
        display: grid;
        place-items: center;
        width: 46px;
        height: 46px;
        border-radius: var(--radius-md);
        background: color-mix(in srgb, var(--brand) 14%, transparent);
        color: var(--brand);
        flex: 0 0 auto;
      }
      .launch__h {
        font-size: var(--text-lg);
        margin: 0;
      }
      .launch__lede {
        margin: 2px 0 0;
        color: var(--text-muted);
        font-size: var(--text-sm);
      }
      .launch__pick {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        flex-wrap: wrap;
      }
      .launch__select {
        flex: 1;
        min-width: 200px;
        padding: 0.55rem 0.7rem;
        border-radius: var(--radius-sm);
        border: 1px solid var(--border);
        background: var(--bg-base);
        color: var(--text-primary);
        font: inherit;
      }
      .launch__list {
        display: flex;
        flex-direction: column;
        gap: var(--space-2);
      }
      .case {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        padding: 0.6rem 0.8rem;
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-base);
        transition: border-color 0.15s ease;
      }
      .case:hover {
        border-color: var(--border-strong);
      }
      .case__ico {
        line-height: 0;
        flex: 0 0 auto;
      }
      .case__name {
        font-weight: var(--weight-medium);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .case__steps {
        margin-left: auto;
        font-size: var(--text-sm);
      }
      .chip {
        padding: 0.12rem 0.5rem;
        border-radius: var(--radius-pill);
        font-size: var(--text-xs);
        font-weight: var(--weight-semibold);
        text-transform: uppercase;
        letter-spacing: 0.03em;
        background: var(--bg-subtle);
        color: var(--text-secondary);
      }
      .launch__none {
        padding: var(--space-4);
        text-align: center;
      }
      .launch__mock {
        align-self: flex-start;
        padding: 0.4rem 0.8rem;
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-sm);
        background: transparent;
        color: var(--text-secondary);
        cursor: pointer;
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
      }
      .launch__mock:hover:not(:disabled) {
        background: var(--bg-subtle);
        color: var(--text-primary);
      }
      .launch__mock:disabled {
        opacity: 0.55;
        cursor: default;
      }
    `,
  ],
})
export class RunMonitorComponent implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly monitor = inject(RunMonitorService);
  private readonly scenarioApi = inject(ScenarioApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly ui = inject(UiService);
  private readonly networks = inject(NetworkFlowsService);

  readonly runId = signal<string | null>(null);
  readonly status = signal<RunStatus>("pending");
  readonly connected = signal(false);
  readonly starting = signal(false);
  readonly cancelling = signal(false);
  readonly lastEventId = signal<string | null>(null);
  readonly steps = signal<StepResult[]>([]);
  private readonly phases = signal<Record<number, PhaseUpdate>>({});

  // live-run identity (resolved from the run detail on attach)
  readonly runLabel = signal<string | null>(null);
  readonly runProjectId = signal<string | null>(null);

  // elapsed clock: start/end epochs + a 1s tick while the run is live
  private readonly startedAt = signal<number | null>(null);
  private readonly completedAt = signal<number | null>(null);
  private readonly nowTick = signal<number>(Date.now());
  private timer?: ReturnType<typeof setInterval>;

  // launcher state (shown when no run is selected)
  readonly projects = signal<Project[]>([]);
  readonly selectedProjectId = signal<string>("");
  readonly projectScenarios = signal<ScenarioSummary[]>([]);
  readonly loadingList = signal(false);

  /** id → project, for O(1) badge lookup. */
  private readonly projectsById = computed(
    () => new Map(this.projects().map((p) => [p.id, p])),
  );

  readonly isRunning = computed(
    () => this.status() === "running" || this.status() === "pending",
  );

  readonly reversedSteps = computed(() => [...this.steps()].reverse());
  readonly filterStatus = signal<RunStatus | "all">("all");
  readonly metrics = computed<MetricsSnapshot>(() => {
    const steps = this.steps();
    const passed = steps.filter((s) => s.status === "passed").length;
    const failed = steps.filter((s) => s.status === "failed").length;
    const blocked = steps.filter((s) => s.status === "blocked").length;
    const durations = steps.map((s) => s.duration_ms ?? 0).filter((d) => d > 0);
    const avgDuration =
      durations.length > 0
        ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
        : 0;
    return {
      totalSteps: steps.length,
      passedSteps: passed,
      failedSteps: failed,
      blockedSteps: blocked,
      avgDuration,
      passRate:
        steps.length > 0 ? Math.round((passed / steps.length) * 100) : 0,
      duration: steps.reduce((max, s) => Math.max(max, s.duration_ms ?? 0), 0),
    };
  });
  readonly filteredSteps = computed(() => {
    const status = this.filterStatus();
    const steps = this.reversedSteps();
    if (status === "all") return steps;
    return steps.filter((s) => s.status === status);
  });
  readonly phaseCards = computed<PhaseCard[]>(() => {
    const ph = this.phases();
    return [1, 2, 3].map((n) => ({
      n,
      label: PHASE_LABELS[n],
      status: (ph[n]?.status ?? "pending") as PhaseCard["status"],
      counts: ph[n] ?? null,
    }));
  });
  readonly statusClass = computed(() => {
    const s = this.status();
    if (s === "running" || s === "pending") return "run";
    if (s === "passed") return "ok";
    if (s === "failed" || s === "error" || s === "cancelled") return "bad";
    return "idle";
  });

  /** Project chip for the live run header (resolved from run detail). */
  readonly runBadge = computed<Badge | null>(() => {
    const pid = this.runProjectId();
    return pid ? this.badgeFor(this.projectsById().get(pid)) : null;
  });

  /** Project chip for the currently-selected launcher project. */
  readonly selectedBadge = computed<Badge | null>(() =>
    this.badgeFor(this.projectsById().get(this.selectedProjectId())),
  );

  /** Elapsed run time in ms — live while running, frozen once finished. */
  readonly elapsedMs = computed(() => {
    const start = this.startedAt();
    if (start == null) return 0;
    const end = this.completedAt() ?? this.nowTick();
    return Math.max(0, end - start);
  });

  readonly elapsedLabel = computed(() => {
    const total = Math.floor(this.elapsedMs() / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
  });

  /** Percentage complete, derived from phases that reached a terminal state. */
  readonly progress = computed(() => {
    if (!this.isRunning()) return 100;
    const ph = this.phases();
    const done = [1, 2, 3].filter((n) =>
      ["passed", "failed", "partial", "blocked"].includes(ph[n]?.status ?? ""),
    ).length;
    return Math.round((done / 3) * 100);
  });

  readonly progressLabel = computed(() => {
    if (!this.isRunning()) return this.status();
    const ph = this.phases();
    const running = [1, 2, 3].find((n) => ph[n]?.status === "running");
    if (running) return `Phase ${running} · ${PHASE_LABELS[running]}`;
    const done = [1, 2, 3].filter((n) =>
      ["passed", "failed", "partial", "blocked"].includes(ph[n]?.status ?? ""),
    ).length;
    return done > 0 ? `${done} of 3 phases` : "Starting…";
  });

  /** Resolve a project's stored (or category-preset) icon + color. */
  private badgeFor(p: Project | undefined): Badge | null {
    if (!p) return null;
    const preset = categoryPreset(p.category);
    return {
      name: p.name,
      icon: p.icon || preset?.icon || "layers",
      color: p.color || preset?.color || "#8b949e",
    };
  }

  /** Icon + color class for a scenario row in the launcher. */
  scenarioBadge(s: ScenarioSummary): { icon: string; color: string } | null {
    if (s.icon || s.color) {
      return { icon: s.icon || "beaker", color: s.color || "#8b949e" };
    }
    const preset = categoryPreset(s.category);
    return preset ? { icon: preset.icon, color: preset.color } : null;
  }

  /** Stepper icon per phase status. */
  phaseIcon(status: PhaseCard["status"]): string {
    if (status === "passed") return "check";
    if (status === "failed" || status === "error") return "x";
    if (status === "running") return "activity";
    return "clock";
  }

  private sub?: Subscription;

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get("id");
    // Projects are always loaded — the launcher needs the list, and the live
    // header needs them to resolve the run's project badge.
    this.loadProjects(!id);
    if (id) this.attach(id);
  }

  // -- launcher --------------------------------------------------------------

  /** Fetch projects. When `pick` (launcher mode) also auto-select the first
   *  non-empty project and load its cases. */
  private loadProjects(pick: boolean): void {
    this.scenarioApi.projects().subscribe({
      next: (ps) => {
        this.projects.set(ps);
        if (!pick) return;
        const chosen = ps.find((p) => p.scenario_count > 0) ?? ps[0];
        if (chosen) {
          this.selectedProjectId.set(chosen.id);
          this.loadScenarios(chosen.id);
        }
      },
      error: () => this.projects.set([]),
    });
  }

  // -- elapsed clock ---------------------------------------------------------

  private startTimer(): void {
    this.stopTimer();
    this.timer = setInterval(() => this.nowTick.set(Date.now()), 1000);
  }
  private stopTimer(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  // -- cancel ----------------------------------------------------------------

  async cancel(): Promise<void> {
    const id = this.runId();
    if (!id || this.cancelling()) return;
    const ok = await this.ui.confirm(
      "Cancel this run? In-flight steps stop now.",
      {
        title: "Cancel run",
        confirmLabel: "Cancel run",
        danger: true,
      },
    );
    if (!ok) return;
    this.cancelling.set(true);
    this.api.cancel(id).subscribe({
      next: () => this.cancelling.set(false),
      error: (e) => {
        this.cancelling.set(false);
        this.ui.toast("error", e?.error?.detail ?? "Could not cancel the run.");
      },
    });
  }

  onProjectChange(id: string): void {
    this.selectedProjectId.set(id);
    this.loadScenarios(id);
  }

  private loadScenarios(projectId: string): void {
    this.loadingList.set(true);
    this.projectScenarios.set([]);
    this.scenarioApi.list({ projectId }).subscribe({
      next: (list) => {
        this.projectScenarios.set(list);
        this.loadingList.set(false);
      },
      error: () => this.loadingList.set(false),
    });
  }

  runProject(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    const name = this.projects().find((p) => p.id === id)?.name ?? "project";
    this.launch({ project_id: id, label: name });
  }

  runScenario(s: ScenarioSummary): void {
    this.launch({ scenario_ids: [s.id], label: s.name });
  }

  /** Mock examples shortcut. */
  start(): void {
    this.launch({});
  }

  private launch(req: CreateRunRequest): void {
    this.starting.set(true);
    this.api.start(req).subscribe({
      next: (res) => {
        this.starting.set(false);
        // navigate so the URL is shareable/reloadable; attach happens after nav
        this.router.navigate(["/run-monitor", res.run_id]).then(() => {
          this.reset();
          this.attach(res.run_id);
        });
      },
      error: (err) => {
        this.starting.set(false);
        void this.onLaunchError(req, err);
      },
    });
  }

  /** A run gated on a network returns 424 with the network id. Rather than a
   *  dead-end error, offer to start that network and retry the run. */
  private async onLaunchError(
    req: CreateRunRequest,
    err: unknown,
  ): Promise<void> {
    const e = err as { status?: number; error?: { detail?: string } };
    const detail = e?.error?.detail ?? "";
    if (e?.status === 424) {
      const nid =
        req.requires_topology ?? detail.match(/'([^']+)'/)?.[1] ?? null;
      if (nid) {
        const go = await this.ui.confirm(
          `The run needs network “${nid}” running first. Start it and retry the run?`,
          { title: "Network not running", confirmLabel: "Start network & run" },
        );
        if (go) {
          this.starting.set(true);
          this.networks.start(nid).subscribe({
            next: () => setTimeout(() => this.launch(req), 1500), // let it bind
            error: (se) => {
              this.starting.set(false);
              this.ui.toast(
                "error",
                se?.error?.detail ?? `Could not start network “${nid}”.`,
              );
            },
          });
        }
        return;
      }
    }
    this.ui.toast("error", detail || "Could not start the run.");
  }

  private reset(): void {
    this.steps.set([]);
    this.phases.set({});
    this.status.set("pending");
    this.lastEventId.set(null);
    this.runLabel.set(null);
    this.runProjectId.set(null);
    this.startedAt.set(null);
    this.completedAt.set(null);
    this.cancelling.set(false);
  }

  private attach(id: string): void {
    this.sub?.unsubscribe();
    this.runId.set(id);
    this.status.set("running");
    this.startedAt.set(Date.now());
    this.startTimer();

    // Pull the run detail to resolve its project + label (for the badge) and
    // its real start/finish timestamps (accurate elapsed on reload/attach).
    this.api.get(id).subscribe({
      next: (d) => {
        this.runLabel.set(d.label);
        this.runProjectId.set(d.project_id);
        if (d.started_at) this.startedAt.set(Date.parse(d.started_at));
        if (d.completed_at) {
          this.completedAt.set(Date.parse(d.completed_at));
          this.status.set(d.status);
          this.stopTimer();
        }
      },
      error: () => {},
    });

    this.sub = this.monitor.stream(id).subscribe({
      next: (event) => {
        this.connected.set(true);
        if (event.id) this.lastEventId.set(event.id);
        switch (event.type) {
          case "run.started":
            this.status.set("running");
            if (this.startedAt() == null) this.startedAt.set(Date.now());
            break;
          case "phase.update": {
            const p = event.data as unknown as PhaseUpdate;
            this.phases.update((m) => ({ ...m, [p.phase]: p }));
            break;
          }
          case "step.result":
            this.steps.update((list) => [
              ...list,
              event.data as unknown as StepResult,
            ]);
            break;
          case "run.completed":
            this.status.set(
              (event.data as { status?: RunStatus }).status ?? "passed",
            );
            this.completedAt.set(Date.now());
            this.stopTimer();
            break;
        }
      },
      error: () => this.connected.set(false),
      complete: () => this.connected.set(false),
    });
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
    this.stopTimer();
  }
}
