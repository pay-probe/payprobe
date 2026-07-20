import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import {
  EndpointHealthService,
  ProbeResult,
} from "../shared/endpoint-health.service";
import {
  EndpointConfig,
  EndpointKey,
  RuntimeConfigService,
} from "../shared/runtime-config.service";
import { UiService } from "../shared/ui.service";

/** Editable row state for one endpoint. */
interface Row {
  key: EndpointKey;
  label: string;
  hint: string;
  kind: "http" | "redis" | "mcp";
  monitorOnly: boolean;
  healthPath?: string;
  scheme: string;
  host: string;
  port: number;
  custom: boolean;
  testing: boolean;
  test?: ProbeResult;
}

/**
 * Settings → Endpoints. Edit the scheme/host/port the portal uses to reach each
 * backend service, and watch live reachability. Overrides persist locally and
 * take effect immediately (every API service resolves its base from the runtime
 * config on each request — no reload). Leaving a row untouched keeps the
 * compiled default, so production's same-origin routing is preserved.
 */
@Component({
  selector: "app-endpoints",
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="eps">
      <div class="eps__head">
        <p class="muted">
          Where the portal reaches each service. Changes save locally and apply
          right away. Use <strong>Test</strong> to probe a row before saving;
          <strong>Revert</strong> drops back to the built-in default.
        </p>
        <div class="eps__actions">
          <button class="btn" (click)="reload()">Reload defaults</button>
          <button
            class="btn btn--danger"
            (click)="resetAll()"
            [disabled]="!anyCustom()"
          >
            Reset all
          </button>
        </div>
      </div>

      @for (row of rows(); track row.key) {
        <section class="card">
          <div class="card__top">
            <div>
              <h2>
                {{ row.label }}
                @if (row.custom) {
                  <span class="chip chip--custom">custom</span>
                } @else {
                  <span class="chip">default</span>
                }
                @if (row.monitorOnly) {
                  <span
                    class="chip chip--info"
                    title="Monitored only — not used as an API base"
                    >monitor</span
                  >
                }
              </h2>
              <p class="muted muted--sm">{{ row.hint }}</p>
            </div>
            <div class="status">
              @if (row.testing) {
                <span class="dot dot--unknown"></span
                ><span class="status__txt">testing…</span>
              } @else if (row.test) {
                <span
                  class="dot"
                  [class.dot--online]="row.test.status === 'online'"
                  [class.dot--degraded]="
                    row.test.status === 'degraded' ||
                    row.test.status === 'unknown'
                  "
                  [class.dot--offline]="row.test.status === 'offline'"
                ></span>
                <span class="status__txt">{{ row.test.detail }}</span>
                @if (row.test.latencyMs != null) {
                  <span class="status__lat mono"
                    >{{ row.test.latencyMs }} ms</span
                  >
                }
              }
            </div>
          </div>

          <div class="grid">
            <label class="fld fld--scheme">
              <span>Scheme</span>
              @if (row.kind === "redis") {
                <input type="text" [(ngModel)]="row.scheme" />
              } @else {
                <select [(ngModel)]="row.scheme">
                  <option value="http">http</option>
                  <option value="https">https</option>
                </select>
              }
            </label>
            <label class="fld fld--host">
              <span>Host</span>
              <input
                type="text"
                [(ngModel)]="row.host"
                placeholder="localhost"
              />
            </label>
            <label class="fld fld--port">
              <span>Port</span>
              <input type="number" [(ngModel)]="row.port" min="1" max="65535" />
            </label>
          </div>

          <div class="card__foot">
            <code class="url">{{ url(row) }}{{ row.healthPath ?? "" }}</code>
            <div class="card__btns">
              <button
                class="btn btn--mini"
                (click)="test(row)"
                [disabled]="row.testing"
              >
                Test
              </button>
              <button
                class="btn btn--mini"
                (click)="save(row)"
                [disabled]="!dirty(row)"
              >
                Save
              </button>
              <button
                class="btn btn--mini btn--danger"
                (click)="revert(row)"
                [disabled]="!row.custom"
              >
                Revert
              </button>
            </div>
          </div>
        </section>
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
      }
      .eps {
        display: flex;
        flex-direction: column;
        gap: 14px;
      }
      .eps__head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
      }
      .eps__actions {
        display: flex;
        gap: 8px;
        flex-shrink: 0;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
        margin: 0;
        max-width: 60ch;
      }
      .muted--sm {
        font-size: 12px;
        margin-top: 2px;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px 16px;
        background: var(--color-node, var(--color-bg));
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .card__top {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
      }
      .card h2 {
        margin: 0;
        font-size: 14px;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .grid {
        display: grid;
        grid-template-columns: 130px 1fr 130px;
        gap: 12px;
      }
      .fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .fld input,
      .fld select {
        padding: 7px 9px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
        background: var(--color-bg);
        color: var(--color-text);
        font: inherit;
        font-size: 13px;
      }
      .card__foot {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .url {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        border-radius: 5px;
        padding: 3px 7px;
        color: var(--color-text-muted, var(--color-text));
      }
      .card__btns {
        display: flex;
        gap: 8px;
      }
      .status {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .status__txt {
        font-size: 11px;
        color: var(--color-text-muted, var(--color-text));
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .status__lat {
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--color-border);
      }
      .dot--online {
        background: var(--color-success, #2ea043);
      }
      .dot--degraded,
      .dot--unknown {
        background: var(--color-warn, #d29922);
      }
      .dot--offline {
        background: var(--color-danger, #f85149);
      }
      .chip {
        font-size: 10px;
        padding: 1px 7px;
        border-radius: 999px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        color: var(--color-text-muted, var(--color-text));
        text-transform: uppercase;
        letter-spacing: 0.03em;
      }
      .chip--custom {
        color: var(--color-primary, #58a6ff);
        border-color: var(--color-primary, #58a6ff);
      }
      .chip--info {
        color: var(--color-text-muted, var(--color-text));
      }
      .btn {
        padding: 7px 14px;
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
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn--mini {
        padding: 4px 11px;
        font-size: 11px;
      }
      .btn--danger {
        color: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
        background: transparent;
      }
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      }
      @media (max-width: 720px) {
        .grid {
          grid-template-columns: 1fr;
        }
      }
    `,
  ],
})
export class EndpointsComponent implements OnInit {
  private readonly cfg = inject(RuntimeConfigService);
  private readonly health = inject(EndpointHealthService);
  private readonly ui = inject(UiService);

  readonly rows = signal<Row[]>([]);

  ngOnInit(): void {
    this.reload();
  }

  /** Reload rows from the current runtime config. */
  reload(): void {
    this.rows.set(this.cfg.endpoints().map((e) => this.toRow(e)));
  }

  anyCustom(): boolean {
    return this.rows().some((r) => r.custom);
  }

  /** Absolute base (no health path) for a row, for display + testing. */
  url(row: Row): string {
    return `${row.scheme}://${row.host}:${row.port}`;
  }

  /** Has the user changed this row away from its currently-saved values? */
  dirty(row: Row): boolean {
    const cur = this.cfg.endpoint(row.key);
    return (
      row.scheme !== cur.scheme ||
      row.host !== cur.host ||
      Number(row.port) !== cur.port
    );
  }

  /** Probe a row's current (possibly unsaved) values. */
  async test(row: Row): Promise<void> {
    this.patch(row.key, { testing: true, test: undefined });
    const probeCfg: EndpointConfig = {
      ...this.cfg.endpoint(row.key),
      scheme: row.scheme,
      host: row.host,
      port: Number(row.port),
    };
    const result = await this.health.probe(probeCfg);
    this.patch(row.key, { testing: false, test: result });
  }

  /** Persist a single row's override and apply it live. */
  save(row: Row): void {
    if (!this.dirty(row)) return;
    this.cfg.update(row.key, {
      scheme: row.scheme,
      host: row.host,
      port: Number(row.port),
    });
    this.patch(row.key, { custom: true });
    this.ui.toast("success", `${row.label} endpoint updated`);
  }

  /** Drop a row's override, reverting to the built-in default. */
  revert(row: Row): void {
    this.cfg.reset(row.key);
    const fresh = this.cfg.endpoint(row.key);
    this.rows.update((rs) =>
      rs.map((r) => (r.key === row.key ? this.toRow(fresh) : r)),
    );
    this.ui.toast("success", `${row.label} reverted to default`);
  }

  resetAll(): void {
    this.cfg.resetAll();
    this.reload();
    this.ui.toast("success", "All endpoints reset to defaults");
  }

  private toRow(e: EndpointConfig): Row {
    return {
      key: e.key,
      label: e.label,
      hint: e.hint,
      kind: e.kind,
      monitorOnly: e.monitorOnly,
      healthPath: e.healthPath,
      scheme: e.scheme,
      host: e.host,
      port: e.port,
      custom: this.cfg.hasOverride(e.key),
      testing: false,
    };
  }

  private patch(key: EndpointKey, partial: Partial<Row>): void {
    this.rows.update((rs) =>
      rs.map((r) => (r.key === key ? { ...r, ...partial } : r)),
    );
  }
}
