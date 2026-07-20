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
import { RouterModule } from "@angular/router";

import { AssistantApiService } from "../assistant/assistant-api.service";
import {
  InsightApiService,
  InsightDataset,
  InsightModel,
  InsightStatus,
  LabelQueueEntry,
} from "../shared/insight-api.service";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/**
 * Model Studio — custom training for the advisory insight service (ADR-0005).
 *
 * The dashboard half of the story: status hero, dataset and model cards, and
 * the active-learning label queue. Creation flows through the guided wizard
 * at /model-studio/new. Everything stays ADVISORY — a custom model changes
 * how failures are *labeled and explained*, never a verdict.
 */
@Component({
  selector: "app-model-studio",
  standalone: true,
  imports: [
    PageHeaderComponent,
    IconComponent,
    CommonModule,
    FormsModule,
    RouterModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ms">
      <pp-page-header>
        <div subtitle>
          <span class="ai-badge">model — advisory</span>
          <span class="muted">
            custom training data → your own failure taxonomy; never a gate
          </span>
        </div>
      </pp-page-header>

      <div class="ms__body">
        @if (unavailable()) {
          <div class="card banner banner--warn">
            <pp-icon name="alert-circle" [size]="16" />
            The insight service isn't reachable. It's an optional deployment —
            start it (compose service <code>insight-service</code>, port 8500)
            and reload.
          </div>
        } @else {
          <!-- hero: status + primary CTA -->
          <div class="hero">
            @if (status(); as st) {
              <div class="hero__stats">
                <div class="stat">
                  <span class="stat__n">{{ st.corpus_size }}</span>
                  <span class="stat__l">failures in corpus</span>
                </div>
                <div class="stat">
                  <span class="stat__n stat__n--sm">{{ activeName(st) }}</span>
                  <span class="stat__l">active categorizer</span>
                </div>
                <div class="stat">
                  <span class="stat__n">{{ models().length }}</span>
                  <span class="stat__l">custom models</span>
                </div>
                <div
                  class="stat"
                  [title]="
                    calib().length
                      ? 'daily Brier over scored predictions, last 30 days'
                      : ''
                  "
                >
                  <span class="stat__n">{{ st.predictor.brier ?? "—" }}</span>
                  <span class="stat__l">prediction Brier (lower = better)</span>
                  @if (calib().length > 1) {
                    <svg
                      class="spark"
                      viewBox="0 0 120 28"
                      preserveAspectRatio="none"
                    >
                      <polyline [attr.points]="sparkPoints()" />
                    </svg>
                  }
                </div>
              </div>
            }
            <a class="btn btn--lg" routerLink="/model-studio/new">
              <pp-icon name="sparkles" [size]="15" /> New model
            </a>
          </div>

          @if (status() && !status()!.sklearn_available) {
            <div class="banner banner--warn">
              <pp-icon name="alert-circle" [size]="14" />
              scikit-learn not installed — training disabled, heuristic +
              statistical baselines stay active.
            </div>
          }

          <!-- the model chain, so the precedence is never a mystery -->
          <div class="chain">
            <span
              class="chain__tier"
              [class.chain__tier--on]="tier() === 'custom'"
            >
              <pp-icon name="sparkles" [size]="13" /> custom model
            </span>
            <pp-icon name="chevron-right" [size]="13" />
            <span
              class="chain__tier"
              [class.chain__tier--on]="tier() === 'clusters'"
            >
              <pp-icon name="group" [size]="13" /> auto-clusters
            </span>
            <pp-icon name="chevron-right" [size]="13" />
            <span
              class="chain__tier"
              [class.chain__tier--on]="tier() === 'heuristics'"
            >
              <pp-icon name="shield" [size]="13" /> heuristic floor
            </span>
            <span class="muted chain__note">
              — highest available tier answers; the floor never goes away
            </span>
          </div>

          <!-- retrain nudge: training data moved on / drift detected -->
          @if (retrainNudge(); as n) {
            <div class="banner banner--warn">
              <pp-icon name="zap" [size]="14" />
              <span>{{ n.text }}</span>
              <span class="spacer"></span>
              <a
                class="btn"
                [routerLink]="['/model-studio/new']"
                [queryParams]="{ from: n.model.id }"
              >
                Retrain
              </a>
            </div>
          }

          <div class="cols">
            <!-- datasets -->
            <section class="card">
              <div class="card__h">
                <pp-icon name="database" [size]="15" /> Training datasets
                <span class="muted count">{{ datasets().length }}</span>
              </div>
              @if (!datasets().length) {
                <div class="empty">
                  <pp-icon name="database" [size]="22" />
                  <p>
                    No datasets yet. The
                    <a routerLink="/model-studio/new">wizard</a> builds them
                    from CSV, pasted rows, or live failures.
                  </p>
                </div>
              }
              @for (d of datasets(); track d.id) {
                <div class="ds">
                  <div class="ds__head">
                    <span class="ds__name">{{ d.name }}</span>
                    <span class="muted mono">{{ d.id }}</span>
                    <span class="spacer"></span>
                    <span class="muted">{{ d.row_count }} rows</span>
                    <button
                      class="ibtn"
                      [title]="viewerId() === d.id ? 'Close rows' : 'View rows'"
                      (click)="toggleViewer(d)"
                    >
                      <pp-icon
                        [name]="viewerId() === d.id ? 'x' : 'table'"
                        [size]="13"
                      />
                    </button>
                    <button
                      class="ibtn"
                      title="Delete dataset"
                      (click)="removeDs(d)"
                    >
                      <pp-icon name="x" [size]="13" />
                    </button>
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

                  <!-- top-5 peek: the exact records, always visible -->
                  @if (viewerId() !== d.id && previews()[d.id]; as peek) {
                    <div class="peek">
                      <div class="peek__h muted">
                        top {{ peek.length }} records
                        <button class="peek__more" (click)="toggleViewer(d)">
                          view all {{ d.row_count }}
                        </button>
                      </div>
                      <table class="viewer__t">
                        <tbody>
                          @for (r of peek; track $index) {
                            <tr>
                              <td class="mono vtext" [title]="r.text">
                                {{ r.text }}
                              </td>
                              <td class="vlabel">
                                <span class="chip">{{ r.label }}</span>
                              </td>
                            </tr>
                          }
                        </tbody>
                      </table>
                    </div>
                  }

                  <!-- dataset viewer: the actual rows, filter + search -->
                  @if (viewerId() === d.id) {
                    <div class="viewer">
                      <div class="viewer__bar">
                        <input
                          type="text"
                          placeholder="search text…"
                          [(ngModel)]="viewerQ"
                          (keyup.enter)="loadViewer(true)"
                        />
                        <button
                          class="btn btn--ghost"
                          (click)="loadViewer(true)"
                        >
                          <pp-icon name="search" [size]="13" />
                        </button>
                        <div class="labels">
                          <button
                            class="chip chip--btn"
                            [class.chip--on]="!viewerLabel()"
                            (click)="filterLabel(null)"
                          >
                            all
                          </button>
                          @for (l of labelStats(d); track l.name) {
                            <button
                              class="chip chip--btn"
                              [class.chip--on]="viewerLabel() === l.name"
                              (click)="filterLabel(l.name)"
                            >
                              {{ l.name }}
                            </button>
                          }
                        </div>
                        @if (viewerLabel(); as vl) {
                          <input
                            type="text"
                            class="relabel"
                            [placeholder]="'rename \\'' + vl + '\\' to…'"
                            #rn
                            (keyup.enter)="renameLabel(d, vl, rn.value)"
                          />
                        }
                      </div>
                      @if (viewerBusy()) {
                        <p class="muted">loading…</p>
                      } @else if (!viewerRows().length) {
                        <p class="muted">no rows match.</p>
                      } @else {
                        <table class="viewer__t">
                          <tbody>
                            @for (r of viewerRows(); track r.id) {
                              <tr>
                                <td class="mono vtext" [title]="r.text">
                                  {{ r.text }}
                                </td>
                                <td class="vlabel">
                                  @if (editRowId() === r.id) {
                                    <input
                                      type="text"
                                      class="relabel"
                                      [value]="r.label"
                                      #rl
                                      (keyup.enter)="relabelRow(d, r, rl.value)"
                                      (keyup.escape)="editRowId.set(null)"
                                    />
                                  } @else {
                                    <button
                                      class="chip chip--btn"
                                      title="Click to relabel"
                                      (click)="editRowId.set(r.id)"
                                    >
                                      {{ r.label }}
                                    </button>
                                  }
                                </td>
                                <td class="vdel">
                                  <button
                                    class="ibtn"
                                    title="Delete row"
                                    (click)="deleteRow(d, r)"
                                  >
                                    <pp-icon name="x" [size]="12" />
                                  </button>
                                </td>
                              </tr>
                            }
                          </tbody>
                        </table>
                        <div class="row viewer__foot">
                          <span class="muted">
                            {{ viewerRows().length }} of
                            {{ viewerTotal() }} rows — text is normalized
                            (digits/hosts/ids masked)
                          </span>
                          @if (viewerRows().length < viewerTotal()) {
                            <button
                              class="btn btn--ghost"
                              (click)="moreViewer()"
                            >
                              show more
                            </button>
                          }
                        </div>
                      }
                    </div>
                  }
                </div>
              }
              <details class="quick">
                <summary>Quick CSV upload (or use the wizard)</summary>
                <div class="form">
                  <input
                    type="text"
                    placeholder="Dataset name"
                    [(ngModel)]="dsName"
                  />
                  <textarea
                    rows="5"
                    placeholder="CSV: text,label — one example per line"
                    [(ngModel)]="dsCsv"
                  ></textarea>
                  <div class="row">
                    <input
                      type="file"
                      accept=".csv,.txt,.json"
                      (change)="onFile($event)"
                    />
                    <button
                      class="btn"
                      [disabled]="!dsName.trim() || !dsCsv.trim() || busy()"
                      (click)="upload()"
                    >
                      Upload
                    </button>
                  </div>
                  @if (error(); as e) {
                    <p class="warn-text">{{ e }}</p>
                  }
                </div>
              </details>
            </section>

            <!-- models -->
            <section class="card">
              <div class="card__h">
                <pp-icon name="cpu" [size]="15" /> Custom models
                <span class="muted count">{{ models().length }}</span>
              </div>
              @if (!models().length) {
                <div class="empty">
                  <pp-icon name="sparkles" [size]="22" />
                  <p>
                    No custom models yet.
                    <a routerLink="/model-studio/new">Create one</a> — holdout
                    accuracy is measured on a 20% split the model never saw.
                  </p>
                </div>
              }
              @for (m of models(); track m.id) {
                <div class="model" [class.model--active]="m.active">
                  <div class="model__ring">
                    <svg viewBox="0 0 60 60" width="56" height="56">
                      <circle cx="30" cy="30" r="24" class="ring-bg" />
                      <circle
                        cx="30"
                        cy="30"
                        r="24"
                        class="ring"
                        [style.stroke-dasharray]="accDash(m)"
                      />
                    </svg>
                    <span class="model__pct">{{ accPct(m) }}</span>
                  </div>
                  <div class="model__meta">
                    <div class="model__name">
                      {{ m.name }}
                      @if (m.active) {
                        <span class="chip chip--on">active</span>
                      }
                    </div>
                    <div class="muted model__sub">
                      {{ m.metrics.rows ?? "—" }} rows ·
                      {{ (m.metrics.labels ?? []).length }} labels ·
                      <span class="mono">{{ m.id }}</span>
                    </div>
                    <div class="labels">
                      @for (l of m.metrics.labels ?? []; track l) {
                        <span class="chip">{{ l }}</span>
                      }
                    </div>
                  </div>
                  <div class="model__actions">
                    @if (m.active) {
                      <button class="btn btn--ghost" (click)="deactivate()">
                        deactivate
                      </button>
                    } @else {
                      <button class="btn" (click)="activate(m)">
                        activate
                      </button>
                    }
                    <button
                      class="ibtn"
                      title="Delete model"
                      (click)="removeModel(m)"
                    >
                      <pp-icon name="x" [size]="13" />
                    </button>
                  </div>
                </div>
              }

              <!-- test drive the live chain: what would a failure be called NOW -->
              <div class="try">
                <div class="try__h muted">
                  Test drive — how the platform would label a failure right now
                </div>
                <div class="row">
                  <input
                    type="text"
                    class="try__in"
                    placeholder="paste an error text…"
                    [(ngModel)]="chainTryText"
                    (keyup.enter)="chainTry()"
                  />
                  <button
                    class="btn btn--ghost"
                    [disabled]="!chainTryText.trim() || chainTryBusy()"
                    (click)="chainTry()"
                  >
                    classify
                  </button>
                </div>
                @if (chainTryResult(); as t) {
                  <div class="row">
                    <span class="chip" [class.chip--novel]="t.novel">{{
                      t.label
                    }}</span>
                    <span class="muted">
                      {{ (t.confidence * 100).toFixed(0) }}% ·
                      {{ t.model_version }}
                    </span>
                  </div>
                }
              </div>
            </section>
          </div>

          <!-- active learning: the failures most worth a human label -->
          <section class="card">
            <div class="card__h">
              <pp-icon name="zap" [size]="15" /> Label these next
              <span class="muted hint">
                — the shapes the models are least sure about, ranked by how much
                labeling them would teach
              </span>
              <span class="spacer"></span>
              @if (queue().length) {
                <button
                  class="btn btn--ghost"
                  [disabled]="suggestBusy()"
                  (click)="suggest()"
                >
                  <pp-icon name="sparkles" [size]="13" />
                  {{ suggestBusy() ? "asking the model…" : "Suggest labels" }}
                </button>
                @if (hasSuggestions()) {
                  <button
                    class="btn"
                    [disabled]="suggestBusy()"
                    (click)="applyAllSuggestions()"
                  >
                    apply all
                  </button>
                }
              }
            </div>
            @if (suggestError(); as e) {
              <div class="banner banner--warn">
                <pp-icon name="alert-circle" [size]="14" /> {{ e }}
              </div>
            }
            @if (!queue().length) {
              <div class="empty">
                <pp-icon name="check" [size]="22" />
                <p>
                  Nothing to label — every known failure shape is confidently
                  categorized or already corrected.
                </p>
              </div>
            } @else {
              <table>
                <thead>
                  <tr>
                    <th>Failure shape</th>
                    <th>Model says</th>
                    <th class="num">Instances</th>
                    <th>Your label</th>
                  </tr>
                </thead>
                <tbody>
                  @for (e of queue(); track e.signature) {
                    <tr>
                      <td class="mono shape" [title]="e.sample_text">
                        {{ e.sample_text }}
                      </td>
                      <td>
                        <span class="chip" [class.chip--novel]="e.current.novel"
                          >{{ e.current.label }}
                          {{ (e.current.confidence * 100).toFixed(0) }}%</span
                        >
                        @if (e.current.novel) {
                          <span class="warn-text">novel</span>
                        }
                      </td>
                      <td class="num">{{ e.count }}</td>
                      <td class="teach">
                        <input
                          type="text"
                          placeholder="your label"
                          #lbl
                          [value]="suggestions()[e.signature]?.label ?? ''"
                          [title]="suggestions()[e.signature]?.reason ?? ''"
                          (keyup.enter)="teach(e, lbl.value)"
                        />
                        @if (suggestions()[e.signature]; as s) {
                          <span
                            class="chip"
                            [class.chip--novel]="s.is_new"
                            [title]="s.reason"
                            >{{ s.is_new ? "new" : "known" }}</span
                          >
                        }
                        <button class="btn" (click)="teach(e, lbl.value)">
                          teach
                        </button>
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
              <p class="muted note">
                A label applies to every failure with the same shape and lands
                in the "Operator corrections" dataset — retrain on it (the
                wizard auto-selects it) to make the model learn it.
              </p>
            }
          </section>
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
      .ms {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .ms__body {
        padding: 1rem 1.25rem 2rem;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 1rem;
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
      .hero {
        display: flex;
        align-items: center;
        gap: 1rem;
        flex-wrap: wrap;
        justify-content: space-between;
      }
      .hero__stats {
        display: flex;
        gap: 0.7rem;
        flex-wrap: wrap;
      }
      .stat {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        background: var(--color-card);
        padding: 0.6rem 1rem;
        display: flex;
        flex-direction: column;
        min-width: 130px;
      }
      .stat__n {
        font-size: 1.35rem;
        font-weight: 700;
        line-height: 1.3;
      }
      .stat__n--sm {
        font-size: 0.95rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 180px;
      }
      .stat__l {
        font-size: 0.72rem;
        color: var(--color-muted);
      }
      .spark {
        width: 120px;
        height: 28px;
        margin-top: 0.25rem;
      }
      .spark polyline {
        fill: none;
        stroke: var(--color-primary);
        stroke-width: 1.5;
        stroke-linejoin: round;
      }
      .chain {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        font-size: 0.78rem;
        color: var(--color-muted);
        flex-wrap: wrap;
      }
      .chain__tier {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        border: 1px solid var(--color-border);
        border-radius: 999px;
        padding: 0.15rem 0.6rem;
      }
      .chain__tier--on {
        border-color: var(--color-primary);
        color: var(--color-primary);
        background: color-mix(in srgb, var(--color-primary) 8%, transparent);
      }
      .chain__note {
        font-size: 0.74rem;
      }
      .cols {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1rem;
        align-items: start;
      }
      @media (max-width: 1100px) {
        .cols {
          grid-template-columns: 1fr;
        }
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        background: var(--color-card);
        padding: 0.9rem 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.7rem;
      }
      .card__h {
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 0.45rem;
      }
      .count {
        font-weight: 400;
        font-size: 0.8rem;
      }
      .hint {
        font-weight: 400;
        font-size: 0.78rem;
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
      .empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0.4rem;
        color: var(--color-muted);
        padding: 1.2rem 0.5rem;
        text-align: center;
        font-size: 0.85rem;
      }
      .empty a {
        color: var(--color-primary);
      }
      .ds {
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        padding: 0.6rem 0.75rem;
        display: flex;
        flex-direction: column;
        gap: 0.45rem;
      }
      .ds__head {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        font-size: 0.85rem;
      }
      .ds__name {
        font-weight: 600;
      }
      .spacer {
        flex: 1;
      }
      .bars {
        display: flex;
        flex-direction: column;
        gap: 0.22rem;
      }
      .bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        font-size: 0.74rem;
      }
      .bar__name {
        width: 130px;
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
        width: 2rem;
        text-align: right;
        color: var(--color-muted);
      }
      .peek {
        border-top: 1px dashed var(--color-border);
        padding-top: 0.4rem;
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
      }
      .peek__h {
        display: flex;
        align-items: center;
        justify-content: space-between;
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      .peek__more {
        border: none;
        background: none;
        color: var(--color-primary);
        font-size: 0.74rem;
        cursor: pointer;
        text-transform: none;
        letter-spacing: normal;
        padding: 0;
      }
      .peek__more:hover {
        text-decoration: underline;
      }
      .viewer {
        border-top: 1px dashed var(--color-border);
        padding-top: 0.5rem;
        display: flex;
        flex-direction: column;
        gap: 0.45rem;
      }
      .viewer__bar {
        display: flex;
        gap: 0.4rem;
        align-items: center;
        flex-wrap: wrap;
      }
      .viewer__bar input {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 4px 8px;
        font-size: 12px;
        width: 170px;
      }
      .chip--btn {
        cursor: pointer;
        background: transparent;
        color: var(--color-muted);
      }
      .chip--btn.chip--on {
        color: var(--color-primary);
        background: color-mix(in srgb, var(--color-primary) 10%, transparent);
      }
      .viewer__t {
        font-size: 0.78rem;
      }
      .viewer__t td {
        padding: 0.25rem 0.4rem;
        border-top: 1px solid
          color-mix(in srgb, var(--color-border) 60%, transparent);
      }
      .vtext {
        max-width: 420px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .vlabel {
        width: 1%;
        white-space: nowrap;
      }
      .viewer__foot {
        justify-content: space-between;
        font-size: 0.76rem;
      }
      .relabel {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-primary);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 12px;
        width: 150px;
      }
      .vdel {
        width: 1%;
        white-space: nowrap;
        text-align: right;
      }
      .try {
        border-top: 1px dashed var(--color-border);
        padding-top: 0.6rem;
        display: flex;
        flex-direction: column;
        gap: 0.45rem;
      }
      .try__h {
        font-size: 0.74rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      .try__in {
        flex: 1;
        min-width: 220px;
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 12px;
        font-family: monospace;
      }
      .quick summary {
        cursor: pointer;
        color: var(--color-muted);
        font-size: 0.8rem;
      }
      .form {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        margin-top: 0.6rem;
      }
      .form input[type="text"],
      .form textarea,
      .teach input {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 7px 10px;
        font-size: 13px;
        font-family: inherit;
      }
      .form textarea {
        font-family: monospace;
        font-size: 12px;
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
        padding: 9px 18px;
        font-size: 13px;
      }
      .ibtn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 26px;
        height: 26px;
        border-radius: 6px;
        border: 1px solid transparent;
        background: transparent;
        color: var(--color-muted);
        cursor: pointer;
      }
      .ibtn:hover {
        border-color: var(--color-border);
        color: var(--color-text);
      }
      .model {
        display: flex;
        gap: 0.9rem;
        align-items: center;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        padding: 0.6rem 0.75rem;
      }
      .model--active {
        border-color: color-mix(
          in srgb,
          var(--color-accent, #2e9e6b) 55%,
          transparent
        );
        background: color-mix(
          in srgb,
          var(--color-accent, #2e9e6b) 5%,
          var(--color-bg)
        );
      }
      .model__ring {
        position: relative;
        flex: none;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .ring-bg {
        fill: none;
        stroke: color-mix(in srgb, var(--color-border) 70%, transparent);
        stroke-width: 6;
      }
      .ring {
        fill: none;
        stroke: var(--color-primary);
        stroke-width: 6;
        stroke-linecap: round;
        transform: rotate(-90deg);
        transform-origin: 30px 30px;
      }
      .model__pct {
        position: absolute;
        font-size: 0.72rem;
        font-weight: 700;
      }
      .model__meta {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
        min-width: 0;
      }
      .model__name {
        font-weight: 600;
        display: flex;
        gap: 0.4rem;
        align-items: center;
      }
      .model__sub {
        font-size: 0.76rem;
      }
      .model__actions {
        display: flex;
        gap: 0.4rem;
        align-items: center;
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
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.83rem;
      }
      th,
      td {
        text-align: left;
        padding: 0.4rem 0.5rem;
        border-top: 1px solid var(--color-border);
      }
      th {
        color: var(--color-muted);
        font-weight: 500;
        border-top: none;
      }
      .num {
        text-align: right;
      }
      .shape {
        max-width: 340px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .teach {
        display: flex;
        gap: 0.4rem;
        align-items: center;
      }
      .teach input {
        padding: 4px 8px;
        font-size: 12px;
        width: 160px;
      }
      .muted {
        color: var(--color-muted);
      }
      .mono {
        font-family: monospace;
        font-size: 0.75rem;
      }
      .warn-text {
        color: var(--color-warn);
        font-size: 0.8rem;
      }
      .note {
        margin: 0.6rem 0 0;
        font-size: 0.78rem;
      }
    `,
  ],
})
export class ModelStudioComponent implements OnInit {
  private readonly api = inject(InsightApiService);
  private readonly assistant = inject(AssistantApiService);

  readonly status = signal<InsightStatus | null>(null);
  readonly datasets = signal<InsightDataset[]>([]);
  readonly models = signal<InsightModel[]>([]);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly unavailable = signal(false);
  readonly queue = signal<LabelQueueEntry[]>([]);

  dsName = "";
  dsCsv = "";

  // -- test drive the active chain ---------------------------------------------
  chainTryText = "";
  readonly chainTryBusy = signal(false);
  readonly chainTryResult = signal<{
    label: string;
    confidence: number;
    novel: boolean;
    model_version: string;
  } | null>(null);

  chainTry(): void {
    const text = this.chainTryText.trim();
    if (!text) return;
    this.chainTryBusy.set(true);
    this.api.inferCategorize(text).subscribe({
      next: (r) => {
        this.chainTryBusy.set(false);
        this.chainTryResult.set(r);
      },
      error: () => this.chainTryBusy.set(false),
    });
  }

  /** Retrain nudge: the active model's training data moved on, or drift. */
  readonly retrainNudge = computed(() => {
    const m = this.models().find((x) => x.active);
    if (!m) return null;
    const parts: string[] = [];
    const its = this.datasets().filter((d) =>
      (m.dataset_ids ?? []).includes(d.id),
    );
    if (its.length) {
      const now = its.reduce((n, d) => n + d.row_count, 0);
      const grown = now - (m.metrics.rows ?? now);
      if (grown >= 5) {
        parts.push(
          `${grown} new rows in "${m.name}"'s training datasets since it was trained`,
        );
      }
    }
    const drift = this.status()?.categorizer?.drift;
    if (drift?.stale) parts.push(drift.reason || "category drift detected");
    return parts.length ? { model: m, text: parts.join(" · ") } : null;
  });

  // -- prediction calibration sparkline ----------------------------------------
  readonly calib = signal<{ day: string; brier: number; n: number }[]>([]);

  /** Sparkline points: days spread over 120px, Brier scaled to the max. */
  sparkPoints(): string {
    const t = this.calib();
    if (t.length < 2) return "";
    const maxB = Math.max(0.05, ...t.map((p) => p.brier));
    const dx = 120 / (t.length - 1);
    return t
      .map(
        (p, i) =>
          `${(i * dx).toFixed(1)},${(26 - (p.brier / maxB) * 24).toFixed(1)}`,
      )
      .join(" ");
  }

  // -- top-5 record peek per dataset -------------------------------------------
  readonly previews = signal<Record<string, { text: string; label: string }[]>>(
    {},
  );

  // -- dataset viewer state ---------------------------------------------------
  readonly viewerId = signal<string | null>(null);
  readonly viewerRows = signal<{ id: number; text: string; label: string }[]>(
    [],
  );
  readonly editRowId = signal<number | null>(null);

  /** Row edits: the API returns the refreshed dataset — sync card + list. */
  private syncDataset(ds: InsightDataset): void {
    this.datasets.set(this.datasets().map((x) => (x.id === ds.id ? ds : x)));
  }

  deleteRow(
    d: InsightDataset,
    r: { id: number; text: string; label: string },
  ): void {
    this.api.deleteDatasetRow(d.id, r.id).subscribe({
      next: (res) => {
        this.viewerRows.set(this.viewerRows().filter((x) => x.id !== r.id));
        this.viewerTotal.set(Math.max(0, this.viewerTotal() - 1));
        this.syncDataset(res.dataset);
      },
    });
  }

  relabelRow(
    d: InsightDataset,
    r: { id: number; text: string; label: string },
    label: string,
  ): void {
    const clean = label.trim();
    this.editRowId.set(null);
    if (!clean || clean === r.label) return;
    this.api.relabelDatasetRow(d.id, r.id, clean).subscribe({
      next: (res) => {
        this.viewerRows.set(
          this.viewerRows().map((x) =>
            x.id === r.id ? { ...x, label: clean } : x,
          ),
        );
        this.syncDataset(res.dataset);
      },
    });
  }

  renameLabel(d: InsightDataset, from: string, to: string): void {
    const clean = to.trim();
    if (!clean || clean === from) return;
    this.api.renameDatasetLabel(d.id, from, clean).subscribe({
      next: (res) => {
        this.syncDataset(res.dataset);
        this.viewerLabel.set(clean);
        this.loadViewer(true);
      },
    });
  }
  readonly viewerTotal = signal(0);
  readonly viewerLabel = signal<string | null>(null);
  readonly viewerBusy = signal(false);
  viewerQ = "";
  private readonly viewerPage = 25;

  ngOnInit(): void {
    this.refresh();
  }

  toggleViewer(d: InsightDataset): void {
    if (this.viewerId() === d.id) {
      this.viewerId.set(null);
      return;
    }
    this.viewerId.set(d.id);
    this.viewerLabel.set(null);
    this.viewerQ = "";
    this.loadViewer(true);
  }

  filterLabel(label: string | null): void {
    this.viewerLabel.set(label);
    this.loadViewer(true);
  }

  loadViewer(reset: boolean): void {
    const id = this.viewerId();
    if (!id) return;
    if (reset) this.viewerRows.set([]);
    this.viewerBusy.set(reset);
    this.api
      .datasetRows(id, {
        label: this.viewerLabel() ?? undefined,
        q: this.viewerQ.trim() || undefined,
        limit: this.viewerPage,
        offset: this.viewerRows().length,
      })
      .subscribe({
        next: (r) => {
          this.viewerRows.set([...this.viewerRows(), ...r.rows]);
          this.viewerTotal.set(r.total);
          this.viewerBusy.set(false);
        },
        error: () => this.viewerBusy.set(false),
      });
  }

  moreViewer(): void {
    this.loadViewer(false);
  }

  activeName(st: InsightStatus): string {
    return (
      st.categorizer.custom?.name ??
      (st.categorizer.learned ? "auto-clusters" : "heuristics")
    );
  }

  /** Which tier of the categorizer chain is currently answering. */
  tier(): "custom" | "clusters" | "heuristics" {
    const st = this.status();
    if (st?.categorizer.custom) return "custom";
    if (st?.categorizer.learned) return "clusters";
    return "heuristics";
  }

  // -- LLM label suggestions (human approves every teach) ----------------------
  readonly suggestions = signal<
    Record<string, { label: string; reason: string; is_new: boolean }>
  >({});
  readonly suggestBusy = signal(false);
  readonly suggestError = signal<string | null>(null);

  hasSuggestions(): boolean {
    return Object.keys(this.suggestions()).length > 0;
  }

  suggest(): void {
    this.suggestBusy.set(true);
    this.suggestError.set(null);
    this.assistant.suggestLabels(10).subscribe({
      next: (r) => {
        this.suggestBusy.set(false);
        if (r.needs_config) {
          this.suggestError.set(
            "No language model configured on the assistant — set it up in " +
              "Settings → AI assistant.",
          );
          return;
        }
        if (r.error) {
          this.suggestError.set(r.error);
          return;
        }
        const map: Record<
          string,
          { label: string; reason: string; is_new: boolean }
        > = {};
        for (const s of r.suggestions) {
          if (s.label) {
            map[s.signature] = {
              label: s.label,
              reason: s.reason,
              is_new: s.is_new,
            };
          }
        }
        this.suggestions.set(map);
      },
      error: () => {
        this.suggestBusy.set(false);
        this.suggestError.set("Couldn't reach the assistant service.");
      },
    });
  }

  /** Approve every suggestion at once — each one is still a normal teach. */
  applyAllSuggestions(): void {
    for (const e of this.queue()) {
      const s = this.suggestions()[e.signature];
      if (s?.label) this.teach(e, s.label);
    }
    this.suggestions.set({});
  }

  /** Bulk-teach: label one shape, cover every instance, feed the dataset. */
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
      .subscribe({ next: () => this.refresh() });
  }

  refresh(): void {
    this.api.status().subscribe({
      next: (s) => {
        this.status.set(s);
        this.unavailable.set(false);
      },
      error: () => this.unavailable.set(true),
    });
    this.api.listDatasets().subscribe({
      next: (r) => {
        this.datasets.set(r.datasets);
        // hydrate the top-5 record peek for each card (tiny, parallel reads)
        for (const d of r.datasets) {
          this.api.datasetRows(d.id, { limit: 5 }).subscribe({
            next: (page) =>
              this.previews.set({ ...this.previews(), [d.id]: page.rows }),
            error: () => {}, // older service without /rows — peek just absent
          });
        }
      },
      error: () => this.datasets.set([]),
    });
    this.api.listModels().subscribe({
      next: (r) => this.models.set(r.models),
      error: () => this.models.set([]),
    });
    this.api.labelQueue(10).subscribe({
      next: (r) => this.queue.set(r.queue),
      error: () => this.queue.set([]),
    });
    this.api.calibration(30).subscribe({
      next: (r) => this.calib.set(r.trend),
      error: () => this.calib.set([]), // older service — sparkline just absent
    });
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

  accPct(m: InsightModel): string {
    const a = m.metrics.holdout_accuracy;
    return a != null ? (a * 100).toFixed(0) + "%" : "—";
  }

  /** Ring circumference is 2π·24 ≈ 150.8; dash = accuracy share of it. */
  accDash(m: InsightModel): string {
    const a = m.metrics.holdout_accuracy ?? 0;
    const c = 2 * Math.PI * 24;
    return `${(a * c).toFixed(1)} ${c.toFixed(1)}`;
  }

  onFile(ev: Event): void {
    const file = (ev.target as HTMLInputElement).files?.[0];
    if (!file) return;
    if (!this.dsName.trim()) this.dsName = file.name.replace(/\.\w+$/, "");
    void file.text().then((t) => (this.dsCsv = t));
  }

  upload(): void {
    this.busy.set(true);
    this.error.set(null);
    this.api
      .createDataset({ name: this.dsName.trim(), csv: this.dsCsv })
      .subscribe({
        next: () => {
          this.dsName = "";
          this.dsCsv = "";
          this.busy.set(false);
          this.refresh();
        },
        error: (e) => {
          this.busy.set(false);
          this.error.set(e?.error?.detail ?? "upload failed");
        },
      });
  }

  activate(m: InsightModel): void {
    this.api.activateModel(m.id).subscribe({ next: () => this.refresh() });
  }

  deactivate(): void {
    this.api.activateModel("none").subscribe({ next: () => this.refresh() });
  }

  removeDs(d: InsightDataset): void {
    this.api.deleteDataset(d.id).subscribe({ next: () => this.refresh() });
  }

  removeModel(m: InsightModel): void {
    this.api.deleteModel(m.id).subscribe({ next: () => this.refresh() });
  }
}
