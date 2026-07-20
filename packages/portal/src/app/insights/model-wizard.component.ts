import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, Router, RouterModule } from "@angular/router";

import {
  InsightApiService,
  InsightDataset,
  InsightModel,
  InsightStatus,
  LabelQueueEntry,
} from "../shared/insight-api.service";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/** A review-step warning: blocking errors stop training, soft ones advise. */
interface ReviewIssue {
  severity: "error" | "warn";
  text: string;
}

/**
 * Model Wizard (/model-studio/new) — guided creation of a custom categorizer.
 *
 * Steps: 1 basics → 2 training data (existing datasets · upload · seed from
 * the live failure corpus) → 3 review label balance → 4 train + results.
 * Same advisory contract as the studio: an activated model only changes how
 * failures are labeled and explained, never a verdict.
 */
@Component({
  selector: "app-model-wizard",
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterModule,
    PageHeaderComponent,
    IconComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="wz">
      <pp-page-header>
        <div subtitle>
          <span class="ai-badge">model — advisory</span>
          <span class="muted">guided custom-model creation</span>
        </div>
      </pp-page-header>

      <div class="wz__body">
        <a class="back" routerLink="/model-studio">
          <pp-icon name="arrow-left" [size]="14" /> Model Studio
        </a>

        <!-- stepper -->
        <ol class="steps">
          @for (s of stepDefs; track s.n) {
            <li
              [class.on]="step() === s.n"
              [class.done]="step() > s.n"
              (click)="jump(s.n)"
            >
              <span class="steps__dot">
                @if (step() > s.n) {
                  <pp-icon name="check" [size]="12" />
                } @else {
                  {{ s.n }}
                }
              </span>
              <span class="steps__label">{{ s.label }}</span>
            </li>
          }
        </ol>

        @if (status() && !status()!.sklearn_available) {
          <div class="card banner banner--warn">
            <pp-icon name="alert-circle" [size]="16" />
            scikit-learn isn't installed on the insight service — training is
            disabled. The wizard can still assemble datasets.
          </div>
        }

        <!-- STEP 1 · basics -->
        @if (step() === 1) {
          <section class="card">
            <h3>Name your model</h3>
            <p class="muted">
              A custom categorizer learns <em>your</em> failure taxonomy from
              labeled examples. When activated it outranks the auto-discovered
              clusters and the built-in heuristics — for labeling and
              explanations only, never for pass/fail verdicts.
            </p>
            <label class="field">
              <span>Model name</span>
              <input
                type="text"
                placeholder="e.g. switch-declines-v2"
                [(ngModel)]="name"
                autofocus
              />
            </label>
            <label class="pick">
              <input type="checkbox" [(ngModel)]="activateAfter" />
              Activate immediately when training succeeds
            </label>
          </section>
        }

        <!-- STEP 2 · data -->
        @if (step() === 2) {
          <section class="card">
            <h3>Training data</h3>
            <div class="tabs">
              <button
                [class.on]="tab() === 'existing'"
                (click)="tab.set('existing')"
              >
                <pp-icon name="database" [size]="14" /> Existing datasets
              </button>
              <button
                [class.on]="tab() === 'upload'"
                (click)="tab.set('upload')"
              >
                <pp-icon name="plus" [size]="14" /> Upload / paste
              </button>
              <button [class.on]="tab() === 'seed'" (click)="tab.set('seed')">
                <pp-icon name="sparkles" [size]="14" /> Seed from live failures
              </button>
            </div>

            @if (tab() === "existing") {
              @if (!datasets().length) {
                <p class="muted">
                  No datasets yet — upload one, or seed from live failures.
                </p>
              }
              <div class="ds-grid">
                @for (d of datasets(); track d.id) {
                  <button
                    class="ds"
                    [class.ds--picked]="picked().has(d.id)"
                    (click)="togglePick(d.id)"
                  >
                    <div class="ds__top">
                      <span class="ds__name">{{ d.name }}</span>
                      <span class="ds__rows">{{ d.row_count }} rows</span>
                    </div>
                    <div class="bars">
                      @for (l of labelStats(d); track l.name) {
                        <div class="bar">
                          <span class="bar__name">{{ l.name }}</span>
                          <span class="bar__track">
                            <span
                              class="bar__fill"
                              [style.width.%]="l.pct"
                            ></span>
                          </span>
                          <span class="bar__n">{{ l.count }}</span>
                        </div>
                      }
                    </div>
                    @if (picked().has(d.id)) {
                      <span class="ds__check"
                        ><pp-icon name="check" [size]="14"
                      /></span>
                    }
                  </button>
                }
              </div>
            }

            @if (tab() === "upload") {
              <div class="form">
                <label class="field">
                  <span>Dataset name</span>
                  <input
                    type="text"
                    placeholder="e.g. settlement failures"
                    [(ngModel)]="upName"
                  />
                </label>
                <label class="field">
                  <span>Rows — CSV <code>text,label</code>, one per line</span>
                  <textarea
                    rows="7"
                    placeholder="response_code 91 issuer inoperative,issuer-down
payShield LMK check failed,hsm-key-problem"
                    [(ngModel)]="upCsv"
                  ></textarea>
                </label>
                <div class="row">
                  <input
                    type="file"
                    accept=".csv,.txt,.json"
                    (change)="onFile($event)"
                  />
                  <button
                    class="btn"
                    [disabled]="!upName.trim() || !upCsv.trim() || busy()"
                    (click)="upload()"
                  >
                    Add dataset
                  </button>
                </div>
                <p class="muted">
                  Text is normalized before storage (digits, hosts and ids
                  masked) — PANs never persist. Uploaded datasets are
                  auto-selected for this model.
                </p>
              </div>
            }

            @if (tab() === "seed") {
              <p class="muted">
                These are the failure shapes the current models are least sure
                about, straight from your run history. Label one and every
                failure with the same shape becomes ground truth in the reserved
                <strong>Operator corrections</strong> dataset — which is
                auto-selected for this model.
              </p>
              @if (!queue().length) {
                <p class="muted">
                  Nothing to seed — every known failure shape is confidently
                  categorized or already corrected.
                </p>
              }
              @for (e of queue(); track e.signature) {
                <div class="seed">
                  <div class="seed__text mono">{{ e.sample_text }}</div>
                  <div class="seed__meta">
                    <span class="chip" [class.chip--novel]="e.current.novel"
                      >{{ e.current.label }}
                      {{ (e.current.confidence * 100).toFixed(0) }}%</span
                    >
                    <span class="muted">× {{ e.count }}</span>
                  </div>
                  <div class="seed__teach">
                    <input
                      type="text"
                      placeholder="your label"
                      #lbl
                      [disabled]="taught().has(e.signature)"
                      (keyup.enter)="teach(e, lbl.value)"
                    />
                    @if (taught().has(e.signature)) {
                      <span class="ok"
                        ><pp-icon name="check" [size]="14" /> taught</span
                      >
                    } @else {
                      <button class="btn" (click)="teach(e, lbl.value)">
                        teach
                      </button>
                    }
                  </div>
                </div>
              }
            }

            <div class="picked-strip">
              <pp-icon name="layers" [size]="14" />
              {{ picked().size }} dataset(s) · {{ totalRows() }} rows ·
              {{ combinedLabels().length }} labels selected
            </div>
          </section>
        }

        <!-- STEP 3 · review -->
        @if (step() === 3) {
          <section class="card">
            <h3>Review the training mix</h3>
            <div class="review-grid">
              <div class="stat">
                <span class="stat__n">{{ totalRows() }}</span>
                <span class="stat__l">rows</span>
              </div>
              <div class="stat">
                <span class="stat__n">{{ combinedLabels().length }}</span>
                <span class="stat__l">labels</span>
              </div>
              <div class="stat">
                <span class="stat__n">{{ picked().size }}</span>
                <span class="stat__l">datasets</span>
              </div>
              <div class="stat">
                <span class="stat__n">~{{ holdoutRows() }}</span>
                <span class="stat__l">holdout rows (20%)</span>
              </div>
            </div>
            <div class="bars bars--wide">
              @for (l of combinedLabels(); track l.name) {
                <div class="bar">
                  <span class="bar__name">{{ l.name }}</span>
                  <span class="bar__track">
                    <span class="bar__fill" [style.width.%]="l.pct"></span>
                  </span>
                  <span class="bar__n">{{ l.count }}</span>
                </div>
              }
            </div>
            @for (w of issues(); track w.text) {
              <div
                class="banner"
                [class.banner--warn]="w.severity === 'warn'"
                [class.banner--err]="w.severity === 'error'"
              >
                <pp-icon name="alert-circle" [size]="14" /> {{ w.text }}
              </div>
            }
            @if (!issues().length) {
              <div class="banner banner--ok">
                <pp-icon name="check" [size]="14" /> Balanced enough to train.
                Holdout accuracy is measured on a 20% split the model never sees
                — trust the number, not the vibe.
              </div>
            }
          </section>
        }

        <!-- STEP 4 · train + result -->
        @if (step() === 4) {
          <section class="card">
            @if (!result() && !busy()) {
              <h3>Train “{{ name }}”</h3>
              <p class="muted">
                {{ totalRows() }} rows · {{ combinedLabels().length }} labels ·
                {{ picked().size }} dataset(s)
                @if (activateAfter) {
                  · will activate on success
                }
              </p>
              <button class="btn btn--lg" (click)="train()">
                <pp-icon name="zap" [size]="15" /> Train model
              </button>
            }
            @if (busy()) {
              <div class="training">
                <span class="spinner"></span> Training — fitting TF-IDF +
                classifier, scoring the holdout…
              </div>
            }
            @if (error(); as e) {
              <div class="banner banner--err">
                <pp-icon name="alert-circle" [size]="14" /> {{ e }}
              </div>
            }
            @if (result(); as m) {
              <div class="result">
                <div class="result__acc">
                  <svg viewBox="0 0 80 80" width="92" height="92">
                    <circle cx="40" cy="40" r="34" class="ring-bg" />
                    <circle
                      cx="40"
                      cy="40"
                      r="34"
                      class="ring"
                      [style.stroke-dasharray]="accDash(m)"
                    />
                  </svg>
                  <div class="result__pct">{{ accPct(m) }}</div>
                  <div class="muted">holdout accuracy</div>
                </div>
                <div class="result__meta">
                  <h3>
                    {{ m.name }}
                    @if (m.active) {
                      <span class="chip chip--on">active</span>
                    }
                  </h3>
                  <p class="muted">
                    {{ m.metrics.rows }} rows ·
                    {{ (m.metrics.labels ?? []).length }} labels ·
                    <span class="mono">{{ m.id }}</span>
                  </p>
                  <div class="labels">
                    @for (l of m.metrics.labels ?? []; track l) {
                      <span class="chip">{{ l }}</span>
                    }
                  </div>
                  <div class="row">
                    @if (!m.active) {
                      <button class="btn" (click)="activate(m)">
                        Activate now
                      </button>
                    }
                    <a class="btn btn--ghost" routerLink="/model-studio">
                      Back to Model Studio
                    </a>
                  </div>
                </div>
              </div>

              <!-- test drive: paste an error, see what THIS model says -->
              <div class="try">
                <h3>Test drive</h3>
                <p class="muted">
                  Paste a failure text and see how this exact model labels it —
                  before you trust it with the live queue.
                </p>
                <div class="row">
                  <input
                    type="text"
                    class="try__in"
                    placeholder="e.g. ConnectionResetError: peer closed mid transaction"
                    [(ngModel)]="tryText"
                    (keyup.enter)="tryIt(m)"
                  />
                  <button
                    class="btn"
                    [disabled]="!tryText.trim() || tryBusy()"
                    (click)="tryIt(m)"
                  >
                    classify
                  </button>
                </div>
                @if (tryResult(); as t) {
                  <div class="row">
                    <span class="chip" [class.chip--novel]="t.novel">{{
                      t.label
                    }}</span>
                    <span class="muted">
                      {{ (t.confidence * 100).toFixed(0) }}% confidence ·
                      {{ t.model_version }}
                      @if (t.novel) {
                        · no rule anticipated this shape
                      }
                    </span>
                  </div>
                }
              </div>
            }
          </section>
        }

        <!-- footer nav -->
        @if (!result()) {
          <div class="nav">
            <button
              class="btn btn--ghost"
              [disabled]="step() === 1 || busy()"
              (click)="step.set(step() - 1)"
            >
              Back
            </button>
            @if (step() < 4) {
              <button class="btn" [disabled]="!canNext()" (click)="next()">
                {{ step() === 3 ? "Continue to training" : "Next" }}
                <pp-icon name="chevron-right" [size]="14" />
              </button>
            }
          </div>
        }
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .wz {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .wz__body {
        padding: 1rem 1.25rem 2rem;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 1rem;
        max-width: 880px;
        width: 100%;
        margin: 0 auto;
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
        margin-right: 0.5rem;
      }
      .back {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        color: var(--color-muted);
        text-decoration: none;
        font-size: 0.82rem;
      }
      .back:hover {
        color: var(--color-text);
      }
      .steps {
        display: flex;
        gap: 0;
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .steps li {
        flex: 1;
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.55rem 0.75rem;
        border-bottom: 2px solid var(--color-border);
        color: var(--color-muted);
        cursor: pointer;
        font-size: 0.83rem;
        user-select: none;
      }
      .steps li.on {
        color: var(--color-text);
        border-bottom-color: var(--color-primary);
      }
      .steps li.done {
        color: var(--color-primary);
        border-bottom-color: color-mix(
          in srgb,
          var(--color-primary) 45%,
          transparent
        );
      }
      .steps__dot {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        border: 1px solid currentColor;
        font-size: 0.7rem;
        flex: none;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        background: var(--color-card);
        padding: 1rem 1.1rem;
        display: flex;
        flex-direction: column;
        gap: 0.7rem;
      }
      h3 {
        margin: 0;
        font-size: 1rem;
      }
      .field {
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
        font-size: 0.82rem;
        color: var(--color-muted);
      }
      .field input,
      .field textarea {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 13px;
        font-family: inherit;
      }
      .field textarea {
        font-family: monospace;
        font-size: 12px;
      }
      .pick {
        font-size: 0.82rem;
        color: var(--color-muted);
        display: inline-flex;
        gap: 0.4rem;
        align-items: center;
      }
      .tabs {
        display: flex;
        gap: 0.4rem;
        flex-wrap: wrap;
      }
      .tabs button {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        border: 1px solid var(--color-border);
        background: transparent;
        color: var(--color-muted);
        border-radius: 999px;
        padding: 5px 12px;
        font-size: 12px;
        cursor: pointer;
      }
      .tabs button.on {
        border-color: var(--color-primary);
        color: var(--color-primary);
        background: color-mix(in srgb, var(--color-primary) 8%, transparent);
      }
      .ds-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
        gap: 0.7rem;
      }
      .ds {
        position: relative;
        text-align: left;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        color: var(--color-text);
        padding: 0.7rem 0.8rem;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      .ds--picked {
        border-color: var(--color-primary);
        box-shadow: 0 0 0 1px var(--color-primary);
      }
      .ds__top {
        display: flex;
        justify-content: space-between;
        gap: 0.5rem;
        font-size: 0.85rem;
      }
      .ds__name {
        font-weight: 600;
      }
      .ds__rows {
        color: var(--color-muted);
        font-size: 0.78rem;
      }
      .ds__check {
        position: absolute;
        top: 0.5rem;
        right: 0.55rem;
        color: var(--color-primary);
      }
      .bars {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
      }
      .bars--wide .bar__name {
        width: 160px;
      }
      .bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        font-size: 0.75rem;
      }
      .bar__name {
        width: 110px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--color-muted);
      }
      .bar__track {
        flex: 1;
        height: 6px;
        border-radius: 3px;
        background: color-mix(in srgb, var(--color-border) 60%, transparent);
        overflow: hidden;
      }
      .bar__fill {
        display: block;
        height: 100%;
        border-radius: 3px;
        background: var(--color-primary);
      }
      .bar__n {
        width: 2.2rem;
        text-align: right;
        color: var(--color-muted);
      }
      .row {
        display: flex;
        gap: 0.6rem;
        align-items: center;
        flex-wrap: wrap;
      }
      .btn {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        border: 1px solid var(--color-primary);
        background: var(--color-primary);
        color: #fff;
        border-radius: 6px;
        padding: 6px 12px;
        font-size: 12px;
        cursor: pointer;
        text-decoration: none;
      }
      .btn[disabled] {
        opacity: 0.5;
        cursor: default;
      }
      .btn--ghost {
        background: transparent;
        color: var(--color-text);
        border-color: var(--color-border);
      }
      .btn--lg {
        align-self: flex-start;
        padding: 9px 18px;
        font-size: 13px;
      }
      .picked-strip {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        font-size: 0.8rem;
        color: var(--color-muted);
        border-top: 1px dashed var(--color-border);
        padding-top: 0.6rem;
      }
      .seed {
        display: grid;
        grid-template-columns: 1fr auto auto;
        gap: 0.8rem;
        align-items: center;
        border-top: 1px solid var(--color-border);
        padding: 0.5rem 0;
      }
      .seed__text {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 380px;
      }
      .seed__meta {
        display: flex;
        gap: 0.4rem;
        align-items: center;
        font-size: 0.78rem;
      }
      .seed__teach {
        display: flex;
        gap: 0.4rem;
        align-items: center;
      }
      .seed__teach input {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 4px 8px;
        font-size: 12px;
        width: 150px;
      }
      .ok {
        color: var(--color-accent, #2e9e6b);
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        font-size: 0.8rem;
      }
      .review-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        gap: 0.7rem;
      }
      .stat {
        border: 1px solid var(--color-border);
        border-radius: 8px;
        padding: 0.6rem 0.8rem;
        display: flex;
        flex-direction: column;
        background: var(--color-bg);
      }
      .stat__n {
        font-size: 1.3rem;
        font-weight: 700;
      }
      .stat__l {
        font-size: 0.74rem;
        color: var(--color-muted);
      }
      .banner {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        border-radius: 8px;
        padding: 0.55rem 0.8rem;
        font-size: 0.82rem;
        border: 1px solid var(--color-border);
      }
      .banner--warn {
        border-color: color-mix(in srgb, var(--color-warn) 50%, transparent);
        color: var(--color-warn);
      }
      .banner--err {
        border-color: color-mix(in srgb, #d64545 55%, transparent);
        color: #d64545;
      }
      .banner--ok {
        border-color: color-mix(
          in srgb,
          var(--color-accent, #2e9e6b) 50%,
          transparent
        );
        color: var(--color-accent, #2e9e6b);
      }
      .nav {
        display: flex;
        justify-content: space-between;
      }
      .training {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        color: var(--color-muted);
        font-size: 0.9rem;
        padding: 1rem 0;
      }
      .spinner {
        width: 16px;
        height: 16px;
        border-radius: 50%;
        border: 2px solid var(--color-border);
        border-top-color: var(--color-primary);
        animation: spin 0.8s linear infinite;
      }
      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }
      .result {
        display: flex;
        gap: 1.5rem;
        align-items: center;
        flex-wrap: wrap;
      }
      .result__acc {
        position: relative;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0.15rem;
      }
      .ring-bg {
        fill: none;
        stroke: color-mix(in srgb, var(--color-border) 70%, transparent);
        stroke-width: 8;
      }
      .ring {
        fill: none;
        stroke: var(--color-primary);
        stroke-width: 8;
        stroke-linecap: round;
        transform: rotate(-90deg);
        transform-origin: 40px 40px;
      }
      .result__pct {
        position: absolute;
        top: 34px;
        font-weight: 700;
        font-size: 1.05rem;
      }
      .result__meta {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      .try {
        border-top: 1px dashed var(--color-border);
        margin-top: 1rem;
        padding-top: 0.8rem;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        width: 100%;
      }
      .try__in {
        flex: 1;
        min-width: 260px;
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 7px 10px;
        font-size: 13px;
        font-family: monospace;
      }
      .labels {
        display: flex;
        gap: 0.3rem;
        flex-wrap: wrap;
      }
      .chip {
        border-radius: 999px;
        padding: 0.05rem 0.5rem;
        font-size: 0.72rem;
        background: color-mix(in srgb, var(--color-primary) 12%, transparent);
        border: 1px solid
          color-mix(in srgb, var(--color-primary) 35%, transparent);
      }
      .chip--novel {
        background: color-mix(in srgb, var(--color-warn) 16%, transparent);
        border-color: color-mix(in srgb, var(--color-warn) 50%, transparent);
      }
      .chip--on {
        background: color-mix(
          in srgb,
          var(--color-accent, #2e9e6b) 18%,
          transparent
        );
        border-color: color-mix(
          in srgb,
          var(--color-accent, #2e9e6b) 50%,
          transparent
        );
      }
      .muted {
        color: var(--color-muted);
      }
      .mono {
        font-family: monospace;
        font-size: 0.75rem;
      }
      .form {
        display: flex;
        flex-direction: column;
        gap: 0.6rem;
      }
    `,
  ],
})
export class ModelWizardComponent implements OnInit {
  private readonly api = inject(InsightApiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  readonly stepDefs = [
    { n: 1, label: "Basics" },
    { n: 2, label: "Training data" },
    { n: 3, label: "Review" },
    { n: 4, label: "Train" },
  ];

  readonly step = signal(1);
  readonly tab = signal<"existing" | "upload" | "seed">("existing");
  readonly status = signal<InsightStatus | null>(null);
  readonly datasets = signal<InsightDataset[]>([]);
  readonly picked = signal<Set<string>>(new Set());
  readonly queue = signal<LabelQueueEntry[]>([]);
  readonly taught = signal<Set<string>>(new Set());
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly result = signal<InsightModel | null>(null);

  name = "";
  activateAfter = true;
  upName = "";
  upCsv = "";

  // -- test drive (result step) -------------------------------------------------
  tryText = "";
  readonly tryBusy = signal(false);
  readonly tryResult = signal<{
    label: string;
    confidence: number;
    novel: boolean;
    model_version: string;
  } | null>(null);

  tryIt(m: InsightModel): void {
    const text = this.tryText.trim();
    if (!text) return;
    this.tryBusy.set(true);
    this.api.inferCategorize(text, m.id).subscribe({
      next: (r) => {
        this.tryBusy.set(false);
        this.tryResult.set(r);
      },
      error: () => this.tryBusy.set(false),
    });
  }

  /** Combined label → count across the picked datasets. */
  readonly combinedLabels = computed(() => {
    const total: Record<string, number> = {};
    for (const d of this.datasets()) {
      if (!this.picked().has(d.id)) continue;
      for (const [l, n] of Object.entries(d.labels ?? {})) {
        total[l] = (total[l] ?? 0) + n;
      }
    }
    const max = Math.max(1, ...Object.values(total));
    return Object.entries(total)
      .sort((a, b) => b[1] - a[1])
      .map(([name, count]) => ({ name, count, pct: (count / max) * 100 }));
  });

  readonly totalRows = computed(() =>
    this.datasets()
      .filter((d) => this.picked().has(d.id))
      .reduce((n, d) => n + d.row_count, 0),
  );

  readonly holdoutRows = computed(() => Math.floor(this.totalRows() * 0.2));

  /** Review-step verdicts: what would make this training weak or impossible. */
  readonly issues = computed<ReviewIssue[]>(() => {
    const out: ReviewIssue[] = [];
    const labels = this.combinedLabels();
    if (this.status() && !this.status()!.sklearn_available) {
      out.push({
        severity: "error",
        text: "scikit-learn is not installed on the insight service — training will fail.",
      });
    }
    if (!this.picked().size) {
      out.push({ severity: "error", text: "No datasets selected." });
    }
    if (labels.length < 2) {
      out.push({
        severity: "error",
        text: "A categorizer needs at least 2 distinct labels.",
      });
    }
    if (this.totalRows() < 30) {
      out.push({
        severity: "warn",
        text: `Only ${this.totalRows()} rows — accuracy numbers get noisy under ~30. More examples per label help.`,
      });
    }
    if (labels.length >= 2) {
      const counts = labels.map((l) => l.count);
      if (Math.max(...counts) > 5 * Math.min(...counts)) {
        out.push({
          severity: "warn",
          text: "Label balance is skewed (>5×) — the model will favor the big labels. Add examples to the small ones.",
        });
      }
    }
    return out;
  });

  ngOnInit(): void {
    this.refresh();
    // Retrain flow: /model-studio/new?from=<model-id> preselects that
    // model's datasets and proposes a bumped name.
    const from = this.route.snapshot.queryParamMap.get("from");
    if (from) this.prefillFromModel(from);
  }

  private prefillFromModel(modelId: string): void {
    this.api.listModels().subscribe({
      next: (r) => {
        const rec = r.models.find((m) => m.id === modelId);
        if (!rec) return;
        const bump = /(\d+)$/.exec(rec.name);
        this.name = bump
          ? rec.name.replace(/\d+$/, String(Number(bump[1]) + 1))
          : `${rec.name}-2`;
        this.picked.set(new Set(rec.dataset_ids ?? []));
        this.step.set(2); // land on the data step with everything selected
      },
      error: () => {},
    });
  }

  refresh(): void {
    this.api.status().subscribe({
      next: (s) => this.status.set(s),
      error: () => this.status.set(null),
    });
    this.api.listDatasets().subscribe({
      next: (r) => this.datasets.set(r.datasets),
      error: () => this.datasets.set([]),
    });
    this.api.labelQueue(8).subscribe({
      next: (r) => this.queue.set(r.queue),
      error: () => this.queue.set([]),
    });
  }

  jump(n: number): void {
    if (n < this.step()) this.step.set(n);
  }

  canNext(): boolean {
    if (this.busy()) return false;
    if (this.step() === 1) return !!this.name.trim();
    if (this.step() === 2) return this.picked().size > 0;
    if (this.step() === 3) {
      return !this.issues().some((i) => i.severity === "error");
    }
    return false;
  }

  next(): void {
    if (this.canNext()) this.step.set(this.step() + 1);
  }

  togglePick(id: string): void {
    const next = new Set(this.picked());
    next.has(id) ? next.delete(id) : next.add(id);
    this.picked.set(next);
    // Older insight-service list responses omit the label breakdown —
    // hydrate it from the detail read so the review step can count labels.
    const d = this.datasets().find((x) => x.id === id);
    if (next.has(id) && d && d.labels == null) {
      this.api.getDataset(id).subscribe({
        next: (full) =>
          this.datasets.set(
            this.datasets().map((x) => (x.id === id ? full : x)),
          ),
      });
    }
  }

  labelStats(
    d: InsightDataset,
  ): { name: string; count: number; pct: number }[] {
    const entries = Object.entries(d.labels ?? {});
    const max = Math.max(1, ...entries.map(([, n]) => n));
    return entries
      .sort((a, b) => b[1] - a[1])
      .map(([name, count]) => ({ name, count, pct: (count / max) * 100 }));
  }

  onFile(ev: Event): void {
    const file = (ev.target as HTMLInputElement).files?.[0];
    if (!file) return;
    if (!this.upName.trim()) this.upName = file.name.replace(/\.\w+$/, "");
    void file.text().then((t) => (this.upCsv = t));
  }

  upload(): void {
    this.busy.set(true);
    this.error.set(null);
    this.api
      .createDataset({ name: this.upName.trim(), csv: this.upCsv })
      .subscribe({
        next: (ds) => {
          this.upName = "";
          this.upCsv = "";
          this.busy.set(false);
          // auto-select what was just uploaded
          const next = new Set(this.picked());
          next.add(ds.id);
          this.picked.set(next);
          this.refresh();
        },
        error: (e) => {
          this.busy.set(false);
          this.error.set(e?.error?.detail ?? "upload failed");
        },
      });
  }

  /** Seed path: label a live failure shape → corrections dataset, auto-pick. */
  teach(e: LabelQueueEntry, label: string): void {
    const clean = label.trim();
    if (!clean) return;
    this.api
      .correctFailure({
        run_id: e.run_id,
        scenario_id: e.scenario_id,
        step_id: e.step_id,
        label: clean,
        apply_to_signature: true,
      })
      .subscribe({
        next: (r) => {
          const t = new Set(this.taught());
          t.add(e.signature);
          this.taught.set(t);
          const next = new Set(this.picked());
          next.add(r.dataset_id);
          this.picked.set(next);
          this.refresh();
        },
      });
  }

  train(): void {
    this.busy.set(true);
    this.error.set(null);
    this.api
      .trainModel({
        name: this.name.trim(),
        dataset_ids: [...this.picked()],
        activate: this.activateAfter,
      })
      .subscribe({
        next: (m) => {
          this.busy.set(false);
          this.result.set(m);
        },
        error: (e) => {
          this.busy.set(false);
          this.error.set(e?.error?.detail ?? "training failed");
        },
      });
  }

  activate(m: InsightModel): void {
    this.api.activateModel(m.id).subscribe({
      next: () => this.result.set({ ...m, active: true }),
    });
  }

  accPct(m: InsightModel): string {
    const a = m.metrics.holdout_accuracy;
    return a != null ? (a * 100).toFixed(0) + "%" : "—";
  }

  /** Ring circumference is 2π·34 ≈ 213.6; dash = accuracy share of it. */
  accDash(m: InsightModel): string {
    const a = m.metrics.holdout_accuracy ?? 0;
    const c = 2 * Math.PI * 34;
    return `${(a * c).toFixed(1)} ${c.toFixed(1)}`;
  }
}
