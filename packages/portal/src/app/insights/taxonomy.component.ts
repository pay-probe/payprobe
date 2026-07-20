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
import { RouterLink } from "@angular/router";

import {
  InsightApiService,
  InsightCategory,
} from "../shared/insight-api.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/**
 * Failure Taxonomy — the discovered/heuristic category landscape (ADR-0005).
 *
 * Shows every category the insight service has assigned, with counts and
 * inline renaming for discovered clusters. The headline is the **gate
 * meter**: the share of failures in `error`/`unknown` — the shapes no rule
 * anticipated. That number decides whether the learned layer earns its keep
 * (design-doc phase gate: ≥15%); if it stays low, the heuristics are winning
 * and that is a success, not a failure.
 */
@Component({
  selector: "app-insight-taxonomy",
  standalone: true,
  imports: [PageHeaderComponent, CommonModule, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tx">
      <pp-page-header>
        <div subtitle>
          <span class="ai-badge">model — advisory</span>
          <span class="muted"
            >every failure category the service has assigned, and how much the
            rules didn't anticipate</span
          >
        </div>
      </pp-page-header>

      <div class="tx__body">
        @if (unavailable()) {
          <div class="card warn">
            The insight service isn't reachable. It's an optional deployment —
            start it (compose service <code>insight-service</code>, port 8500)
            and reload.
          </div>
        } @else {
          <!-- drift watch: has the failure landscape moved since the fit? -->
          @if (drift(); as d) {
            @if (d.stale) {
              <div class="card stale">
                <strong>Models look stale.</strong>
                {{ d.reason }} — the failure landscape has moved since the last
                fit. Retrain (Model Studio → Ingest + retrain, or POST /train),
                or teach the new shapes via the label queue.
              </div>
            }
          }

          <!-- the gate meter -->
          <div class="card">
            <div class="card__h">
              Novel-failure share
              <span class="muted hint"
                >— failures landing in <code>error</code>/<code>unknown</code>
                (no rule anticipated their shape)</span
              >
            </div>
            <div class="gate">
              <div class="gate__bar">
                <div
                  class="gate__fill"
                  [class.gate__fill--over]="novelShare() >= 15"
                  [style.width.%]="novelShare()"
                ></div>
                <div class="gate__mark" [style.left.%]="15"></div>
              </div>
              <div class="gate__num">
                {{ novelShare() }}%
                <span class="muted">of {{ corpusSize() }} failures</span>
              </div>
            </div>
            <p class="muted note">
              @if (corpusSize() === 0) {
                No failures ingested yet — run some scenarios, then POST /train
                (or wait for the nightly self-train).
              } @else if (novelShare() >= 15) {
                Above the 15% phase gate: the regex taxonomy is missing real
                failure families — the learned layer (and your custom models in
                <a routerLink="/model-studio">Model Studio</a>) earn their keep
                here.
              } @else {
                Below the 15% phase gate: the deterministic taxonomy covers
                almost everything. The learned layer staying quiet is a success,
                not a failure.
              }
            </p>
          </div>

          <!-- the taxonomy table -->
          <div class="card">
            <div class="card__h">Categories</div>
            @if (!categories().length) {
              <p class="muted">No categorized failures yet.</p>
            } @else {
              <table>
                <thead>
                  <tr>
                    <th>Label</th>
                    <th>Id</th>
                    <th>Kind</th>
                    <th>Heuristic floor</th>
                    <th class="num">Failures</th>
                    <th>Last seen</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  @for (c of categories(); track c.category) {
                    <tr>
                      <td>
                        @if (editing() === c.category) {
                          <input
                            type="text"
                            [ngModel]="c.label"
                            #edit
                            (keyup.enter)="rename(c, edit.value)"
                            (keyup.escape)="editing.set(null)"
                          />
                        } @else {
                          {{ c.label }}
                        }
                      </td>
                      <td class="mono muted">{{ c.category }}</td>
                      <td>
                        <span class="chip" [class]="'chip--' + kind(c)">
                          {{ kind(c) }}
                        </span>
                      </td>
                      <td class="mono muted">{{ c.heuristic }}</td>
                      <td class="num">{{ c.count }}</td>
                      <td class="muted">{{ c.last_seen | slice: 0 : 10 }}</td>
                      <td class="actions">
                        @if (editing() === c.category) {
                          <button class="lnk" (click)="editing.set(null)">
                            cancel
                          </button>
                        } @else {
                          <button class="lnk" (click)="editing.set(c.category)">
                            rename
                          </button>
                        }
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
              <p class="muted note">
                Kinds: <strong>heuristic</strong> = the shared regex taxonomy,
                <strong>learned</strong> = a discovered cluster
                (<code>ins-cat-*</code>, stable across retrains),
                <strong>human</strong> = a label operators taught via
                corrections or a custom model. Renaming only changes the display
                name — ids and history stay intact.
              </p>
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
      .tx {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .tx__body {
        padding: 1rem 1.25rem;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 1rem;
        max-width: 980px;
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
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        background: var(--color-card);
        padding: 0.9rem 1rem;
      }
      .card.stale {
        border-color: color-mix(in srgb, var(--color-warn) 55%, transparent);
        border-left: 3px solid var(--color-warn);
        font-size: 0.85rem;
      }
      .card.warn {
        border-color: color-mix(in srgb, var(--color-warn) 50%, transparent);
      }
      .card__h {
        font-weight: 600;
        margin-bottom: 0.7rem;
      }
      .hint {
        font-weight: 400;
        font-size: 0.8rem;
      }
      .gate {
        display: flex;
        align-items: center;
        gap: 1rem;
      }
      .gate__bar {
        position: relative;
        flex: 1;
        height: 10px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--color-border) 60%, transparent);
        overflow: visible;
      }
      .gate__fill {
        height: 100%;
        border-radius: 999px;
        background: var(--color-accent);
        transition: width 0.3s;
      }
      .gate__fill--over {
        background: var(--color-warn);
      }
      .gate__mark {
        position: absolute;
        top: -3px;
        width: 2px;
        height: 16px;
        background: var(--color-muted);
      }
      .gate__num {
        font-size: 1.1rem;
        font-weight: 600;
        white-space: nowrap;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
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
      .chip {
        border-radius: 999px;
        padding: 0.05rem 0.55rem;
        font-size: 0.72rem;
        border: 1px solid var(--color-border);
      }
      .chip--learned {
        background: color-mix(in srgb, var(--color-primary) 14%, transparent);
        border-color: color-mix(in srgb, var(--color-primary) 40%, transparent);
      }
      .chip--human {
        background: color-mix(in srgb, var(--color-accent) 14%, transparent);
        border-color: color-mix(in srgb, var(--color-accent) 45%, transparent);
      }
      .actions {
        text-align: right;
      }
      .lnk {
        background: none;
        border: none;
        color: var(--color-primary);
        cursor: pointer;
        font-size: 0.78rem;
        padding: 0;
      }
      input {
        background: var(--color-bg);
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 0.8rem;
      }
      .muted {
        color: var(--color-muted);
      }
      .mono {
        font-family: monospace;
        font-size: 0.78rem;
      }
      .note {
        margin: 0.6rem 0 0;
        font-size: 0.78rem;
      }
    `,
  ],
})
export class InsightTaxonomyComponent implements OnInit {
  private readonly api = inject(InsightApiService);

  readonly categories = signal<InsightCategory[]>([]);
  readonly drift = signal<{
    stale: boolean;
    reason: string | null;
  } | null>(null);
  readonly corpusSize = signal(0);
  readonly unavailable = signal(false);
  /** Category id currently being renamed inline. */
  readonly editing = signal<string | null>(null);

  /** % of failures whose shape no rule anticipated — the phase-gate metric. */
  readonly novelShare = computed(() => {
    const total = this.corpusSize();
    if (!total) return 0;
    const novel = this.categories()
      .filter((c) => c.heuristic === "error" || c.heuristic === "unknown")
      .reduce((sum, c) => sum + c.count, 0);
    return Math.round((novel / total) * 100);
  });

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.api.status().subscribe({
      next: (st) => this.drift.set(st.categorizer.drift ?? null),
      error: () => this.drift.set(null),
    });
    this.api.listCategories().subscribe({
      next: (r) => {
        this.categories.set(r.categories);
        this.corpusSize.set(r.corpus_size);
        this.unavailable.set(false);
      },
      error: () => this.unavailable.set(true),
    });
  }

  kind(c: InsightCategory): "heuristic" | "learned" | "human" {
    if (c.category.startsWith("ins-cat-")) return "learned";
    return c.category === c.heuristic ? "heuristic" : "human";
  }

  rename(c: InsightCategory, label: string): void {
    const clean = label.trim();
    this.editing.set(null);
    if (!clean || clean === c.label) return;
    this.api
      .renameCategory(c.category, clean)
      .subscribe({ next: () => this.refresh() });
  }
}
