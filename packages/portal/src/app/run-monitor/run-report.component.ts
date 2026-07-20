import { CommonModule, DatePipe } from "@angular/common";
import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { ActivatedRoute, RouterLink } from "@angular/router";

import { RunApiService, RunDiagnosis } from "./run-api.service";
import {
  RunCompare,
  RunDetail,
  RunStatus,
  ScenarioReport,
  StepOutcome,
} from "./run.models";
import { AssistantApiService } from "../assistant/assistant-api.service";
import { AssistantService } from "../assistant/assistant.service";
import {
  InsightApiService,
  InsightFailure,
} from "../shared/insight-api.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

@Component({
  selector: "app-run-report",
  standalone: true,
  imports: [PageHeaderComponent, CommonModule, DatePipe, RouterLink],
  template: `
    <div class="rr">
      <pp-page-header>
        @if (detail()) {
          <span class="rr__actions">
            @if (
              detail()!.status === "failed" || detail()!.status === "error"
            ) {
              <button
                class="btn"
                (click)="askAiAboutRun()"
                title="Open the assistant with this run's failures as context"
              >
                ✨ Ask AI
              </button>
            }
            <a
              class="btn btn--signoff"
              [routerLink]="['/reports', detail()!.id, 'signoff']"
              >✓ Go/No-Go</a
            >
            <button class="btn" (click)="downloadHtml()">⤓ HTML</button>
            <button class="btn" (click)="downloadJunit()">⤓ JUnit</button>
            <button class="btn" (click)="download()">⤓ JSON</button>
          </span>
        }
      </pp-page-header>

      @if (error()) {
        <p class="rr__error">
          Run not found, or the orchestrator is unreachable.
        </p>
      } @else if (!detail()) {
        <p class="rr__idle">Loading…</p>
      } @else {
        <section class="rr__meta">
          @if (detail()!.label) {
            <span style="font-weight:500;">{{ detail()!.label }}</span>
          }
          <span class="mono">{{ detail()!.id.slice(0, 8) }}</span>
          <span class="badge" [class]="'badge--' + cls(detail()!.status)">{{
            detail()!.status
          }}</span>
          <span class="muted">env: {{ detail()!.environment || "—" }}</span>
          <span class="muted">{{ detail()!.scenario_count }} case(s)</span>
          @if (detail()!.started_at) {
            <span class="muted"
              >started {{ detail()!.started_at | date: "medium" }}</span
            >
          }
          @if (durationMs() !== null) {
            <span class="muted">· {{ durationMs() }} ms</span>
          }
        </section>

        @if (compare(); as cmp) {
          @if (cmp.base_found) {
            <section class="reg" [class.reg--bad]="cmp.summary.regressed > 0">
              <div class="reg__head">
                <strong>Regression vs previous run</strong>
                <a
                  class="muted mono"
                  [routerLink]="['/reports', cmp.base_run_id]"
                >
                  {{ cmp.base_run_id?.slice(0, 8) }}
                </a>
                <span class="ok">{{ cmp.summary.fixed }} fixed</span>
                <span class="bad">{{ cmp.summary.regressed }} regressed</span>
              </div>
              @if (regressed().length || fixed().length) {
                <div class="reg__list">
                  @for (c of regressed(); track c.scenario + c.step) {
                    <span
                      class="reg__item bad"
                      title="was {{ c.from }}, now {{ c.to }}"
                    >
                      ▼ {{ c.scenario }} · {{ c.step }}
                    </span>
                  }
                  @for (c of fixed(); track c.scenario + c.step) {
                    <span
                      class="reg__item ok"
                      title="was {{ c.from }}, now {{ c.to }}"
                    >
                      ▲ {{ c.scenario }} · {{ c.step }}
                    </span>
                  }
                </div>
              } @else {
                <span class="muted"
                  >No status changes — stable vs the previous run.</span
                >
              }
            </section>
          }
        }

        <!-- diagnosis: why it failed + whether the cause is still broken NOW -->
        @if (diagnosis(); as dx) {
          @if (!dx.ok) {
            <section class="dx">
              <div class="dx__head">
                <strong>Diagnosis</strong>
                <span class="dx__headline">{{ dx.headline }}</span>
              </div>
              @for (f of dx.findings; track f.code) {
                @if (f.severity !== "info") {
                  <div
                    class="dx__finding"
                    [class.dx__finding--warn]="f.severity === 'warn'"
                  >
                    <div class="dx__frow">
                      <span class="dx__sev">{{ f.severity }}</span>
                      <span class="dx__ftitle">{{ f.title }}</span>
                    </div>
                    <p class="dx__cause">{{ f.cause }}</p>
                    <p class="dx__fix">→ {{ f.suggestion }}</p>
                    @if (f.evidence) {
                      <p class="dx__ev mono">{{ f.evidence }}</p>
                    }
                  </div>
                }
              }
              @if (dx.doctor; as doc) {
                <div class="dx__doctor">
                  <div class="dx__frow">
                    <span class="dx__sev">live check</span>
                    <span class="dx__ftitle">
                      {{
                        doc.problems.length
                          ? "The platform doctor confirms " +
                            doc.problems.length +
                            " problem(s) right now"
                          : "The platform doctor finds no live problem — the cause may have been transient"
                      }}
                    </span>
                    <a class="dx__link" routerLink="/diagnostics"
                      >Open Diagnostics →</a
                    >
                  </div>
                  @for (p of doc.problems; track p.id) {
                    <div class="dx__frow dx__prob">
                      <span
                        class="dx__sev"
                        [class.dx__sev--fail]="p.status === 'fail'"
                      >
                        {{ p.status }}
                      </span>
                      <span class="mono">{{ p.name }}</span>
                      @if (p.target) {
                        <span class="mono muted">{{ p.target }}</span>
                      }
                      @if (p.hint) {
                        <span class="dx__hint">{{ p.hint }}</span>
                      }
                    </div>
                  }
                </div>
              }
            </section>
          }
        }

        <!-- advisory model insights: category chips + "why did this fail?"
             evidence packs from the insight service. A model OPINION over run
             history — advisory styling only, never a gate (ADR-0005). The
             section simply doesn't render when the service isn't deployed. -->
        @if (insights(); as ins) {
          @if (ins.length > 0) {
            <section class="ai">
              <div class="ai__head">
                <span class="ai__badge">model insight — advisory</span>
                <span class="muted"
                  >learned from run history; gates stay deterministic</span
                >
                @if (!prose().size) {
                  <button
                    class="ai__why ai__llm"
                    [disabled]="proseBusy()"
                    (click)="explainAll()"
                  >
                    {{
                      proseBusy() ? "asking the assistant…" : "✨ plain words"
                    }}
                  </button>
                }
              </div>
              @if (proseError(); as pe) {
                <p class="ai__novel">{{ pe }}</p>
              }
              @for (f of ins; track f.scenario_id + f.step_id) {
                <div class="ai__item">
                  <div class="ai__row">
                    <span
                      class="ai__chip"
                      [class.ai__chip--novel]="f.novel"
                      [title]="
                        'model ' +
                        f.model_version +
                        ' · confidence ' +
                        (f.confidence * 100).toFixed(0) +
                        '%'
                      "
                      >{{ f.label }}</span
                    >
                    @if (f.novel) {
                      <span class="ai__novel"
                        >novel — no rule anticipated this shape</span
                      >
                    }
                    <span class="mono muted"
                      >{{ f.scenario_name }} · {{ f.step_id }}</span
                    >
                    <button
                      class="ai__why"
                      (click)="toggleWhy(f.scenario_id + f.step_id)"
                    >
                      {{
                        whyOpen().has(f.scenario_id + f.step_id)
                          ? "hide"
                          : "why did this fail?"
                      }}
                    </button>
                  </div>
                  @if (whyOpen().has(f.scenario_id + f.step_id)) {
                    @if (prose().get(f.scenario_id + f.step_id); as p) {
                      <p class="ai__prose">✨ {{ p }}</p>
                    }
                    <p class="ai__explain">{{ f.explanation }}</p>
                    @if (f.evidence.similar_failures.length) {
                      <div class="ai__similar">
                        <span class="muted">same failure shape:</span>
                        @for (
                          s of f.evidence.similar_failures;
                          track s.run_id
                        ) {
                          <a
                            class="ai__link mono"
                            [routerLink]="['/run-report', s.run_id]"
                            >{{ s.run_id }}</a
                          >
                        }
                      </div>
                    }
                    <!-- teach the model: correction feeds the reserved
                         "Operator corrections" dataset (ground truth) -->
                    @if (f.model_version === "human-correction") {
                      <p class="ai__taught">✓ corrected by an operator</p>
                    } @else if (corrected().has(f.scenario_id + f.step_id)) {
                      <p class="ai__taught">
                        ✓ saved to Operator corrections — retrain in
                        <a class="ai__link" routerLink="/model-studio"
                          >Model Studio</a
                        >
                        to teach the model
                      </p>
                    } @else {
                      <div class="ai__correct">
                        <span class="muted">wrong category?</span>
                        <input
                          type="text"
                          placeholder="your label, e.g. hsm-socket-drop"
                          #fix
                          (keyup.enter)="correct(f, fix.value)"
                        />
                        <button class="ai__why" (click)="correct(f, fix.value)">
                          teach the model
                        </button>
                      </div>
                    }
                  }
                </div>
              }
            </section>
          }
        }

        @if (!detail()!.summary) {
          <p class="rr__idle">
            This run hasn't produced a report yet.
            <a [routerLink]="['/run-monitor', detail()!.id]">Watch it live →</a>
          </p>
        } @else {
          <!-- report / trace tabs -->
          <nav class="tabs">
            <button
              class="tab"
              [class.tab--on]="view() === 'report'"
              (click)="view.set('report')"
            >
              Report
            </button>
            <button
              class="tab"
              [class.tab--on]="view() === 'trace'"
              (click)="view.set('trace')"
            >
              Trace
            </button>
          </nav>

          @if (view() === "report") {
            <!-- phase summary -->
            <section class="rr__phases">
              @for (p of phases(); track p.key) {
                <div class="phase" [class]="'phase--' + cls(p.status)">
                  <div class="phase__head">
                    <span>{{ p.label }}</span
                    ><span class="phase__st">{{ p.status }}</span>
                  </div>
                  <div class="phase__counts">
                    @if (p.components !== undefined) {
                      <span>{{ p.components }} components</span>
                    }
                    @if (p.total !== undefined) {
                      <span class="ok">{{ p.passed }}✓</span>
                      <span class="bad">{{ p.failed }}✗</span>
                      <span class="muted">{{ p.blocked }}⊘</span>
                    }
                  </div>
                </div>
              }
            </section>

            <!-- per test case -->
            <h2 class="rr__h2">Test cases</h2>
            @for (sc of detail()!.summary!.scenarios; track sc.scenario_id) {
              <section class="case">
                <div class="case__head">
                  <span class="badge" [class]="'badge--' + cls(sc.status)">{{
                    sc.status
                  }}</span>
                  <span class="case__name">{{ sc.name }}</span>
                  <span class="muted">{{ sc.steps.length }} step(s)</span>
                </div>

                @for (st of sc.steps; track st.step_id) {
                  <div class="step" [class]="'step--' + cls(st.status)">
                    <div class="step__row">
                      <span class="step__dot"></span>
                      <span class="step__id mono">{{ st.step_id }}</span>
                      <span class="step__tgt"
                        >{{ st.target }}.{{ st.action }}</span
                      >
                      @if (st.environment_override) {
                        <span
                          class="step__env"
                          title="Per-step environment override"
                          >env: {{ st.environment_override }}</span
                        >
                      }
                      <span class="step__ms muted">{{ st.duration_ms }}ms</span>
                      <span class="step__st">{{ st.status }}</span>
                    </div>

                    @if (st.error) {
                      <div class="step__err">{{ st.error }}</div>
                    }

                    @if (st.assertions.length) {
                      <table class="asserts">
                        <tr class="asserts__h">
                          <th>field</th>
                          <th>op</th>
                          <th>expected</th>
                          <th>actual</th>
                          <th></th>
                        </tr>
                        @for (a of st.assertions; track $index) {
                          <tr [class.bad]="!a.passed">
                            <td class="mono">{{ a.field }}</td>
                            <td>{{ a.operator }}</td>
                            <td class="mono">{{ fmt(a.expected) }}</td>
                            <td class="mono">{{ fmt(a.actual) }}</td>
                            <td>{{ a.passed ? "✓" : "✗" }}</td>
                          </tr>
                        }
                      </table>
                    }

                    <details class="payloads">
                      <summary>request / response</summary>
                      <div class="payloads__grid">
                        <div>
                          <h4>request</h4>
                          <pre>{{ json(st.request) }}</pre>
                        </div>
                        <div>
                          <h4>response</h4>
                          <pre>{{ json(st.response) }}</pre>
                        </div>
                      </div>
                    </details>
                  </div>
                }
              </section>
            }
          } @else {
            <!-- ===== Trace view: waterfall + per-step execution detail ===== -->
            <section class="trace">
              <div class="trace__bar">
                <span class="muted">{{ traceSteps().length }} step(s)</span>
                @if (totalSpanMs() > 0) {
                  <span class="muted">· span {{ totalSpanMs() }} ms</span>
                }
                <button class="btn btn--sm" (click)="downloadTrace()">
                  ⤓ Trace JSON
                </button>
              </div>

              @if (!traceSteps().length) {
                <p class="rr__idle">No executed steps to trace in this run.</p>
              }

              @for (t of traceSteps(); track t.key) {
                <div class="trow" [class]="'trow--' + cls(t.step.status)">
                  <button class="trow__head" (click)="toggle(t.key)">
                    <span class="trow__chev">{{
                      expanded().has(t.key) ? "▾" : "▸"
                    }}</span>
                    <span class="trow__id mono">{{ t.step.step_id }}</span>
                    <span class="trow__tgt"
                      >{{ t.step.target }}.{{ t.step.action }}</span
                    >
                    @if (t.step.environment_override) {
                      <span
                        class="step__env"
                        title="Per-step environment override"
                        >env: {{ t.step.environment_override }}</span
                      >
                    }
                    <span
                      class="wf"
                      [title]="
                        t.step.duration_ms + 'ms @ +' + t.offsetMs + 'ms'
                      "
                    >
                      <span
                        class="wf__bar"
                        [class]="'wf__bar--' + cls(t.step.status)"
                        [style.left.%]="t.leftPct"
                        [style.width.%]="t.widthPct"
                      ></span>
                    </span>
                    <span class="trow__ms muted"
                      >{{ t.step.duration_ms }}ms</span
                    >
                    <span
                      class="trow__st"
                      [class]="'fg--' + cls(t.step.status)"
                      >{{ t.step.status }}</span
                    >
                  </button>

                  @if (expanded().has(t.key)) {
                    <div class="trow__body">
                      <div class="tmeta muted mono">
                        {{ t.scenario }} · phase dispatch +{{ t.offsetMs }}ms
                      </div>

                      <!-- engine log lines -->
                      <div class="tlog">
                        @for (ln of t.step.trace ?? []; track $index) {
                          <div class="tlog__ln" [class]="'lvl--' + ln.level">
                            <span class="tlog__lvl">{{ ln.level }}</span>
                            <span class="tlog__msg mono">{{ ln.msg }}</span>
                          </div>
                        }
                        @if (!(t.step.trace ?? []).length) {
                          <div class="muted">No engine log lines.</div>
                        }
                      </div>

                      <!-- wire detail -->
                      @if (t.step.raw_log) {
                        <h4 class="tsub">Wire</h4>
                        <pre class="wire">{{ t.step.raw_log }}</pre>
                      }

                      <!-- payloads -->
                      <div class="payloads__grid">
                        <div>
                          <h4 class="tsub">request</h4>
                          <pre>{{ json(t.step.request) }}</pre>
                        </div>
                        <div>
                          <h4 class="tsub">response</h4>
                          <pre>{{ json(t.step.response) }}</pre>
                        </div>
                      </div>
                    </div>
                  }
                </div>
              }
            </section>
          }
        }
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .rr {
        padding: 1.5rem;
        margin: 0 auto;
        color: var(--color-text);
      }
      .step__env {
        font-size: 0.72rem;
        font-weight: 600;
        padding: 1px 7px;
        border-radius: 999px;
        border: 1px solid var(--color-border);
        color: var(--color-muted);
        white-space: nowrap;
      }
      .rr__bar {
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        margin-bottom: 1rem;
      }
      .rr__back {
        color: var(--color-muted);
        text-decoration: none;
        font-size: 0.85rem;
      }
      .rr__title {
        margin: 0.25rem 0 0;
        font-size: 1.4rem;
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
      .rr__error {
        color: var(--color-fail);
      }
      .rr__idle {
        color: var(--color-muted);
      }
      .rr__actions {
        display: flex;
        gap: 0.5rem;
      }
      .rr__meta {
        display: flex;
        gap: 0.85rem;
        align-items: center;
        flex-wrap: wrap;
        margin-bottom: 1rem;
      }
      .reg {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 0.7rem 0.9rem;
        margin-bottom: 1.1rem;
        background: var(--color-card);
      }
      .reg--bad {
        border-color: var(--color-fail);
      }
      .reg__head {
        display: flex;
        gap: 0.85rem;
        align-items: center;
        flex-wrap: wrap;
        font-size: 0.9rem;
      }
      .reg__list {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin-top: 0.55rem;
      }
      .reg__item {
        font-size: 0.78rem;
        padding: 0.15rem 0.5rem;
        border-radius: 6px;
        border: 1px solid var(--color-border);
      }
      .reg__item.bad {
        color: var(--color-fail);
        border-color: color-mix(in srgb, var(--color-fail) 40%, transparent);
      }
      .reg__item.ok {
        color: var(--color-accent);
        border-color: color-mix(in srgb, var(--color-accent) 40%, transparent);
      }
      /* advisory model-insight panel (ADR-0005) — deliberately styled as an
         opinion (accent, not fail-red): it informs, it never gates. */
      .ai {
        border: 1px dashed
          color-mix(in srgb, var(--color-accent) 45%, transparent);
        border-radius: 10px;
        padding: 0.7rem 0.9rem;
        margin-bottom: 1.1rem;
        background: var(--color-card);
        display: flex;
        flex-direction: column;
        gap: 0.55rem;
      }
      .ai__head {
        display: flex;
        gap: 0.85rem;
        align-items: baseline;
        flex-wrap: wrap;
        font-size: 0.85rem;
      }
      .ai__badge {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--color-accent);
        border: 1px solid
          color-mix(in srgb, var(--color-accent) 50%, transparent);
        border-radius: 999px;
        padding: 0.1rem 0.55rem;
      }
      .ai__item {
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
      }
      .ai__row {
        display: flex;
        gap: 0.6rem;
        align-items: center;
        flex-wrap: wrap;
        font-size: 0.85rem;
      }
      .ai__chip {
        border-radius: 999px;
        padding: 0.12rem 0.6rem;
        font-size: 0.78rem;
        background: color-mix(in srgb, var(--color-accent) 14%, transparent);
        border: 1px solid
          color-mix(in srgb, var(--color-accent) 40%, transparent);
      }
      .ai__chip--novel {
        background: color-mix(in srgb, var(--color-warn) 16%, transparent);
        border-color: color-mix(in srgb, var(--color-warn) 50%, transparent);
      }
      .ai__novel {
        font-size: 0.75rem;
        color: var(--color-warn);
      }
      .ai__why {
        margin-left: auto;
        background: none;
        border: none;
        color: var(--color-accent);
        cursor: pointer;
        font-size: 0.78rem;
        padding: 0;
      }
      .ai__explain {
        margin: 0;
        font-size: 0.83rem;
        color: var(--color-muted);
        border-left: 2px solid
          color-mix(in srgb, var(--color-accent) 40%, transparent);
        padding-left: 0.6rem;
      }
      .ai__similar {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        font-size: 0.75rem;
      }
      .ai__correct {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        font-size: 0.78rem;
      }
      .ai__correct input {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 0.75rem;
        width: 220px;
      }
      .ai__taught {
        margin: 0;
        font-size: 0.78rem;
        color: var(--color-accent);
      }
      .ai__llm {
        margin-left: auto;
      }
      .ai__prose {
        margin: 0;
        font-size: 0.85rem;
        border-left: 2px solid
          color-mix(in srgb, var(--color-primary) 55%, transparent);
        padding-left: 0.6rem;
      }
      .ai__link {
        color: var(--color-accent);
        text-decoration: none;
      }
      /* diagnosis panel */
      .dx {
        border: 1px solid color-mix(in srgb, var(--color-fail) 45%, transparent);
        border-left: 3px solid var(--color-fail);
        border-radius: 10px;
        padding: 0.7rem 0.9rem;
        margin-bottom: 1.1rem;
        background: var(--color-card);
        display: flex;
        flex-direction: column;
        gap: 0.55rem;
      }
      .dx__head {
        display: flex;
        gap: 0.85rem;
        align-items: baseline;
        flex-wrap: wrap;
        font-size: 0.9rem;
      }
      .dx__headline {
        color: var(--color-muted);
      }
      .dx__finding {
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
        padding: 0.45rem 0.6rem;
        border: 1px solid var(--color-border);
        border-radius: 8px;
      }
      .dx__frow {
        display: flex;
        gap: 0.55rem;
        align-items: baseline;
        flex-wrap: wrap;
      }
      .dx__sev {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 0.1rem 0.45rem;
        border-radius: 6px;
        border: 1px solid var(--color-border);
        color: var(--color-muted);
        flex: none;
      }
      .dx__sev--fail {
        color: var(--color-fail);
        border-color: color-mix(in srgb, var(--color-fail) 40%, transparent);
      }
      .dx__ftitle {
        font-weight: 500;
        font-size: 0.9rem;
      }
      .dx__cause,
      .dx__fix {
        margin: 0;
        font-size: 0.85rem;
      }
      .dx__cause {
        color: var(--color-muted);
      }
      .dx__ev {
        margin: 0;
        font-size: 0.78rem;
        color: var(--color-fail);
        word-break: break-word;
      }
      .dx__doctor {
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
        padding: 0.45rem 0.6rem;
        border: 1px dashed var(--color-border);
        border-radius: 8px;
      }
      .dx__prob {
        font-size: 0.85rem;
      }
      .dx__hint {
        color: var(--color-muted);
        font-size: 0.8rem;
      }
      .dx__link {
        margin-left: auto;
        font-size: 0.85rem;
        color: var(--color-accent);
        text-decoration: none;
      }
      .dx__link:hover {
        text-decoration: underline;
      }
      .mono {
        font-family: ui-monospace, monospace;
      }
      .muted {
        color: var(--color-muted);
      }
      .ok {
        color: var(--color-accent);
      }
      .bad {
        color: var(--color-fail);
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
      .rr__phases {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 0.75rem;
        margin-bottom: 1.25rem;
      }
      .phase {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 0.8rem;
        background: var(--color-card);
      }
      .phase--ok {
        border-color: var(--color-accent);
      }
      .phase--bad {
        border-color: var(--color-fail);
      }
      .phase__head {
        display: flex;
        justify-content: space-between;
        font-size: 0.9rem;
        font-weight: 600;
      }
      .phase__st {
        text-transform: uppercase;
        font-size: 0.72rem;
        color: var(--color-muted);
      }
      .phase__counts {
        display: flex;
        gap: 0.5rem;
        font-size: 0.85rem;
        margin-top: 0.4rem;
      }
      .rr__h2 {
        font-size: 1.1rem;
        margin: 0 0 0.6rem;
      }
      .case {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        margin-bottom: 0.9rem;
        overflow: hidden;
      }
      .case__head {
        display: flex;
        gap: 0.7rem;
        align-items: center;
        padding: 0.7rem 0.9rem;
        background: var(--color-bg-soft);
      }
      .case__name {
        font-weight: 600;
      }
      .step {
        padding: 0.6rem 0.9rem;
        border-top: 1px solid var(--color-border);
      }
      .step__row {
        display: grid;
        grid-template-columns: 12px 90px 1fr 70px 70px;
        gap: 0.6rem;
        align-items: center;
        font-size: 0.86rem;
      }
      .step__dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--color-muted);
      }
      .step--ok .step__dot {
        background: var(--color-accent);
      }
      .step--bad .step__dot {
        background: var(--color-fail);
      }
      .step__st {
        text-transform: uppercase;
        font-weight: 700;
        font-size: 0.74rem;
      }
      .step__err {
        color: var(--color-fail);
        font-size: 0.82rem;
        margin: 0.4rem 0;
      }
      .asserts {
        width: 100%;
        border-collapse: collapse;
        margin: 0.5rem 0;
        font-size: 0.8rem;
      }
      .asserts th {
        text-align: left;
        color: var(--color-muted);
        font-weight: 600;
        padding: 0.2rem 0.4rem;
      }
      .asserts td {
        padding: 0.2rem 0.4rem;
        border-top: 1px solid var(--color-border);
      }
      .asserts tr.bad td {
        color: var(--color-fail);
      }
      .payloads {
        margin-top: 0.4rem;
      }
      .payloads summary {
        cursor: pointer;
        color: var(--color-primary);
        font-size: 0.82rem;
      }
      .payloads__grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.6rem;
        margin-top: 0.4rem;
      }
      .payloads h4 {
        margin: 0 0 0.2rem;
        font-size: 0.74rem;
        color: var(--color-muted);
        text-transform: uppercase;
      }
      .payloads pre {
        background: var(--color-terminal);
        color: var(--color-terminal-text);
        padding: 0.5rem;
        border-radius: 8px;
        overflow: auto;
        font-size: 0.78rem;
        margin: 0;
        max-height: 240px;
      }

      /* tabs */
      .tabs {
        display: flex;
        gap: 0.25rem;
        margin-bottom: 1rem;
        border-bottom: 1px solid var(--color-border);
      }
      .tab {
        padding: 0.5rem 1rem;
        border: none;
        background: none;
        color: var(--color-muted);
        cursor: pointer;
        font-weight: 600;
        font-size: 0.9rem;
        border-bottom: 2px solid transparent;
      }
      .tab--on {
        color: var(--color-text);
        border-bottom-color: var(--color-primary);
      }

      /* trace view */
      .btn--sm {
        padding: 0.3rem 0.6rem;
        font-size: 0.78rem;
      }
      .trace__bar {
        display: flex;
        gap: 0.7rem;
        align-items: center;
        margin-bottom: 0.8rem;
      }
      .trace__bar .btn--sm {
        margin-left: auto;
      }
      .trow {
        border: 1px solid var(--color-border);
        border-radius: 9px;
        margin-bottom: 0.5rem;
        overflow: hidden;
      }
      .trow--bad {
        border-color: color-mix(
          in srgb,
          var(--color-fail) 45%,
          var(--color-border)
        );
      }
      .trow__head {
        width: 100%;
        display: grid;
        grid-template-columns: 14px 90px minmax(120px, 1fr) 220px 60px 70px;
        gap: 0.6rem;
        align-items: center;
        padding: 0.55rem 0.8rem;
        background: none;
        border: none;
        color: var(--color-text);
        cursor: pointer;
        text-align: left;
        font-size: 0.84rem;
      }
      .trow__head:hover {
        background: var(--color-bg-soft);
      }
      .trow__chev {
        color: var(--color-muted);
      }
      .trow__tgt {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .trow__st {
        text-transform: uppercase;
        font-weight: 700;
        font-size: 0.72rem;
      }
      .fg--ok {
        color: var(--color-accent);
      }
      .fg--bad {
        color: var(--color-fail);
      }
      .fg--run {
        color: var(--color-info);
      }
      .fg--idle {
        color: var(--color-muted);
      }
      /* waterfall */
      .wf {
        position: relative;
        height: 12px;
        background: var(--color-bg-soft);
        border-radius: 6px;
        overflow: hidden;
      }
      .wf__bar {
        position: absolute;
        top: 0;
        height: 100%;
        min-width: 2px;
        border-radius: 6px;
        background: var(--color-muted);
      }
      .wf__bar--ok {
        background: var(--color-accent);
      }
      .wf__bar--bad {
        background: var(--color-fail);
      }
      .wf__bar--run {
        background: var(--color-info);
      }
      .trow__body {
        padding: 0.6rem 0.9rem 0.9rem;
        border-top: 1px solid var(--color-border);
        background: var(--color-bg-soft);
      }
      .tmeta {
        font-size: 0.76rem;
        margin-bottom: 0.55rem;
      }
      .tsub {
        margin: 0.7rem 0 0.25rem;
        font-size: 0.72rem;
        color: var(--color-muted);
        text-transform: uppercase;
      }
      .tlog {
        display: flex;
        flex-direction: column;
        gap: 1px;
        background: var(--color-terminal);
        border-radius: 8px;
        padding: 0.5rem 0.6rem;
      }
      .tlog__ln {
        display: grid;
        grid-template-columns: 52px 1fr;
        gap: 0.6rem;
        font-size: 0.77rem;
        color: var(--color-terminal-text);
      }
      .tlog__lvl {
        text-transform: uppercase;
        font-size: 0.66rem;
        font-weight: 700;
        opacity: 0.85;
        align-self: center;
      }
      .tlog__msg {
        white-space: pre-wrap;
        word-break: break-all;
      }
      .lvl--info .tlog__lvl {
        color: var(--color-info);
      }
      .lvl--debug .tlog__lvl {
        color: var(--color-muted);
      }
      .lvl--wire .tlog__lvl {
        color: var(--color-primary);
      }
      .lvl--wire .tlog__msg {
        color: var(--color-primary);
      }
      .lvl--pass .tlog__lvl {
        color: var(--color-accent);
      }
      .lvl--fail .tlog__lvl,
      .lvl--error .tlog__lvl {
        color: var(--color-fail);
      }
      .lvl--fail .tlog__msg,
      .lvl--error .tlog__msg {
        color: var(--color-fail);
      }
      .wire {
        background: var(--color-terminal);
        color: var(--color-terminal-text);
        padding: 0.5rem 0.6rem;
        border-radius: 8px;
        overflow: auto;
        font-size: 0.77rem;
        margin: 0;
        white-space: pre-wrap;
        word-break: break-all;
      }
    `,
  ],
})
export class RunReportComponent implements OnInit {
  private readonly api = inject(RunApiService);
  private readonly insightApi = inject(InsightApiService);
  private readonly assistantApi = inject(AssistantApiService);
  private readonly assistant = inject(AssistantService);
  private readonly route = inject(ActivatedRoute);

  readonly detail = signal<RunDetail | null>(null);
  readonly error = signal(false);
  readonly compare = signal<RunCompare | null>(null);
  /** Run triage (+ live doctor pass) — fetched only for non-passed runs. */
  readonly diagnosis = signal<RunDiagnosis | null>(null);

  /** Advisory categorized failures from the insight service (ADR-0005).
   *  null = unavailable/not deployed — the section silently doesn't render. */
  readonly insights = signal<InsightFailure[] | null>(null);
  /** Which "why did this fail?" packs are expanded. */
  readonly whyOpen = signal<Set<string>>(new Set());
  /** Failures corrected in this session (scenario_id + step_id keys). */
  readonly corrected = signal<Set<string>>(new Set());
  /** LLM prose per failure (Phase-2), keyed scenario_id + step_id. */
  readonly prose = signal<Map<string, string>>(new Map());
  readonly proseBusy = signal(false);
  readonly proseError = signal<string | null>(null);

  /** Active tab: summary report vs. execution trace. */
  readonly view = signal<"report" | "trace">("report");
  /** Trace rows currently expanded, keyed by `${scenario}::${step_id}`. */
  readonly expanded = signal<Set<string>>(new Set());

  readonly regressed = computed(() =>
    (this.compare()?.changes ?? []).filter((c) => c.kind === "regressed"),
  );
  readonly fixed = computed(() =>
    (this.compare()?.changes ?? []).filter((c) => c.kind === "fixed"),
  );

  readonly durationMs = computed(() => {
    const d = this.detail();
    if (!d?.started_at || !d?.completed_at) return null;
    return (
      new Date(d.completed_at).getTime() - new Date(d.started_at).getTime()
    );
  });

  readonly phases = computed(() => {
    const labels: Record<string, string> = {
      phase_1: "Phase 1 · Components",
      phase_2: "Phase 2 · Integration",
      phase_3: "Phase 3 · End-to-End",
    };
    const ph = this.detail()?.summary?.phases ?? {};
    return Object.keys(labels).map((key) => ({
      key,
      label: labels[key],
      ...ph[key],
      status: (ph[key]?.status ?? "pending") as RunStatus,
    }));
  });

  /**
   * Flatten every executed step across scenarios into a time-ordered list with
   * waterfall geometry (left/width %) relative to the run's earliest dispatch.
   * Falls back to sequential layout from cumulative durations for older runs
   * that predate per-step timestamps.
   */
  readonly traceSteps = computed(() => {
    const scs = this.detail()?.summary?.scenarios ?? [];
    const rows: {
      key: string;
      scenario: string;
      step: StepOutcome;
      start: number;
    }[] = [];
    let cursor = 0;
    for (const sc of scs) {
      for (const step of sc.steps) {
        const hasTs =
          typeof step.started_at_ms === "number" && step.started_at_ms > 0;
        const start = hasTs ? (step.started_at_ms as number) : cursor;
        cursor = start + (step.duration_ms || 0);
        rows.push({
          key: `${sc.scenario_id}::${step.step_id}`,
          scenario: sc.name,
          step,
          start,
        });
      }
    }
    if (!rows.length) return [];
    const min = Math.min(...rows.map((r) => r.start));
    const max = Math.max(
      ...rows.map((r) => r.start + (r.step.duration_ms || 0)),
    );
    const span = Math.max(max - min, 1);
    rows.sort((a, b) => a.start - b.start);
    return rows.map((r) => {
      const offsetMs = r.start - min;
      const leftPct = (offsetMs / span) * 100;
      const widthPct = Math.max(((r.step.duration_ms || 0) / span) * 100, 1.5);
      return {
        ...r,
        offsetMs,
        leftPct,
        widthPct: Math.min(widthPct, 100 - leftPct),
      };
    });
  });

  readonly totalSpanMs = computed(() => {
    const rows = this.traceSteps();
    if (!rows.length) return 0;
    return Math.max(...rows.map((r) => r.offsetMs + (r.step.duration_ms || 0)));
  });

  toggleWhy(key: string): void {
    const next = new Set(this.whyOpen());
    next.has(key) ? next.delete(key) : next.add(key);
    this.whyOpen.set(next);
  }

  /** Phase-2: ask the assistant's LLM to rewrite the evidence packs as
   *  plain prose. Grounded — the model sees only the deterministic packs. */
  explainAll(): void {
    const d = this.detail();
    if (!d) return;
    this.proseBusy.set(true);
    this.proseError.set(null);
    this.assistantApi.explainRun(d.id).subscribe({
      next: (r) => {
        this.proseBusy.set(false);
        if (r.needs_config) {
          this.proseError.set(
            "No LLM configured. Add a provider and API key in Settings → " +
              "AI assistant (takes effect within seconds), or set " +
              "ASSIST_LLM_API_KEY in the assistant container's environment.",
          );
          return;
        }
        if (r.error) {
          this.proseError.set(r.error);
          return;
        }
        const map = new Map<string, string>();
        for (const e of r.explanations) {
          if (e.text) map.set(e.scenario_id + e.step_id, e.text);
        }
        this.prose.set(map);
        // open every panel so the prose is visible
        this.whyOpen.set(new Set(map.keys()));
      },
      error: () => {
        this.proseBusy.set(false);
        this.proseError.set("Assistant service unreachable.");
      },
    });
  }

  /** Send an operator correction — the label becomes ground truth and feeds
   *  the "Operator corrections" dataset for retraining in Model Studio. */
  correct(f: InsightFailure, label: string): void {
    const clean = label.trim();
    const d = this.detail();
    if (!clean || !d) return;
    this.insightApi
      .correctFailure({
        run_id: d.id,
        scenario_id: f.scenario_id,
        step_id: f.step_id,
        label: clean,
      })
      .subscribe({
        next: () => {
          f.label = clean; // reflect the new ground truth in place
          const next = new Set(this.corrected());
          next.add(f.scenario_id + f.step_id);
          this.corrected.set(next);
        },
      });
  }

  toggle(key: string): void {
    const next = new Set(this.expanded());
    next.has(key) ? next.delete(key) : next.add(key);
    this.expanded.set(next);
  }

  downloadTrace(): void {
    const d = this.detail();
    if (!d) return;
    const payload = {
      run_id: d.id,
      label: d.label,
      status: d.status,
      steps: this.traceSteps().map((r) => ({
        scenario: r.scenario,
        offset_ms: r.offsetMs,
        ...r.step,
      })),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trace-${d.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get("id");
    if (!id) {
      this.error.set(true);
      return;
    }
    this.api.get(id).subscribe({
      next: (d) => {
        this.detail.set(d);
        // failed run → fetch the diagnosis (likely cause + live doctor pass)
        // so the reader is informed here, without visiting Diagnostics.
        if (d.status === "failed" || d.status === "error") {
          this.api.diagnose(d.id).subscribe({
            next: (dx) => this.diagnosis.set(dx),
            error: () => this.diagnosis.set(null),
          });
          // Advisory model insights (optional service — errors mean "no
          // insights", never a page failure).
          this.insightApi.runFailures(d.id).subscribe({
            next: (r) => this.insights.set(r.failures),
            error: () => this.insights.set(null),
          });
        }
      },
      error: () => this.error.set(true),
    });
    this.api.compare(id).subscribe({
      next: (c) => this.compare.set(c),
      error: () => this.compare.set(null),
    });
  }

  /** Fetch through HttpClient (auth interceptor attaches the token) and save;
   *  a plain download anchor would hit the JWT gate and save the 401 JSON. */
  downloadHtml(): void {
    const d = this.detail();
    if (!d) return;
    this.api
      .fetchBlob(this.api.htmlUrl(d.id))
      .subscribe((b) => this.saveBlob(b, `run-${d.id}.html`));
  }

  downloadJunit(): void {
    const d = this.detail();
    if (!d) return;
    this.api
      .fetchBlob(this.api.junitUrl(d.id))
      .subscribe((b) => this.saveBlob(b, `junit-${d.id}.xml`));
  }

  private saveBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  cls(status: RunStatus): string {
    if (status === "running" || status === "pending") return "run";
    if (status === "passed") return "ok";
    if (status === "failed" || status === "error" || status === "cancelled")
      return "bad";
    return "idle";
  }

  fmt(v: unknown): string {
    if (v === null || v === undefined) return "—";
    return typeof v === "object" ? JSON.stringify(v) : String(v);
  }

  json(v: unknown): string {
    return JSON.stringify(v ?? null, null, 2);
  }

  download(): void {
    const d = this.detail();
    if (!d) return;
    const blob = new Blob([JSON.stringify(d, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `run-${d.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  /** Open the assistant pre-seeded with this run's failures (hidden context —
   *  the transcript just shows the question). */
  askAiAboutRun(): void {
    const d = this.detail();
    if (!d) return;
    const failures: string[] = [];
    for (const s of d.summary?.scenarios ?? []) {
      for (const st of s.steps) {
        if (st.status === "failed" || st.status === "error") {
          failures.push(
            `${s.name} → ${st.step_id} (${st.target}.${st.action}): ` +
              `${st.error ?? "assertion failure"}`,
          );
        }
      }
    }
    const context =
      `the user is viewing run report ${d.id}` +
      `${d.label ? ` "${d.label}"` : ""} — status ${d.status}, ` +
      `environment ${d.environment ?? "default"}. ` +
      (failures.length
        ? `Failed steps: ${failures.slice(0, 6).join("; ")}` +
          (failures.length > 6 ? ` (+${failures.length - 6} more)` : "")
        : "No per-step failures recorded.");
    this.assistant.askAbout(
      "Why did this run fail, and what should I check first?",
      context,
    );
  }
}
