import {
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import { GraphNode } from "../run-monitor/run-api.service";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";

interface CodeRow {
  code: string;
  count: number;
  share: number; // 0..1 of the total
  ok: boolean; // approval code ("00") — colors the bar
}

/**
 * Node action drawer for the Topology Map — answers "so what's wrong with
 * *it*?" in place. Identity + live stats + response/traffic breakdowns, and
 * per-kind deep links (simulator metrics & chaos, flow editor, network trace,
 * live sessions, diagnostics) so the next step never requires knowing which
 * of five pages holds the answer.
 */
@Component({
  selector: "app-tm2-node-drawer",
  standalone: true,
  imports: [RouterLink, IconComponent, BadgeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (node(); as n) {
      <aside class="dw">
        <header class="dw__head">
          <span class="dw__ico"><pp-icon [name]="icon" [size]="18" /></span>
          <div class="dw__title">
            <h3>{{ n.label }}</h3>
            <span class="mono muted">{{ sub }}</span>
          </div>
          <button class="dw__x" (click)="closed.emit()" aria-label="Close">
            <pp-icon name="x" [size]="14" />
          </button>
        </header>

        <div class="dw__badges">
          <pp-badge tone="neutral">{{ n.kind }}</pp-badge>
          <pp-badge [tone]="statusTone">{{ n.status }}</pp-badge>
          @if (n.saved) {
            <pp-badge tone="neutral">saved</pp-badge>
          }
          @if (n.owner && n.owner !== "standalone") {
            <span class="muted sm">in {{ n.owner }}</span>
          }
        </div>

        <div class="dw__stats">
          <div class="st">
            <span class="st__k">Rate</span>
            <span class="st__v">{{ rate }}<span class="st__u">/s</span></span>
          </div>
          <div class="st">
            <span class="st__k">Total</span>
            <span class="st__v">{{ totalLabel }}</span>
          </div>
          <div class="st">
            <span class="st__k">Peers</span>
            <span class="st__v">{{ n.peers ?? 0 }}</span>
          </div>
          <div class="st">
            <span class="st__k">Approved</span>
            <span
              class="st__v"
              [class.st__v--ok]="approval() !== null && approval()! >= 90"
              [class.st__v--bad]="approval() !== null && approval()! < 90"
            >
              {{ approval() === null ? "—" : approval() + "%" }}
            </span>
          </div>
        </div>

        @if (rcRows().length) {
          <section class="sec">
            <h4>
              Responses <span class="muted">({{ rcTitle }})</span>
            </h4>
            @for (r of rcRows(); track r.code) {
              <div class="crow">
                <span class="mono">{{ r.code }}</span>
                <span class="crow__track">
                  <span
                    class="crow__bar"
                    [class.crow__bar--decl]="!r.ok"
                    [style.width.%]="r.share * 100"
                  ></span>
                </span>
                <span class="num">{{ r.count }}</span>
              </div>
            }
          </section>
        }

        @if (mtiRows().length) {
          <section class="sec">
            <h4>Traffic mix <span class="muted">(by MTI / command)</span></h4>
            @for (r of mtiRows(); track r.code) {
              <div class="crow">
                <span class="mono">{{ r.code }}</span>
                <span class="crow__track">
                  <span
                    class="crow__bar crow__bar--mix"
                    [style.width.%]="r.share * 100"
                  ></span>
                </span>
                <span class="num">{{ r.count }}</span>
              </div>
            }
          </section>
        }

        @if (n.status === "unresolved") {
          <p class="hint">
            <pp-icon name="alert-circle" [size]="13" />
            This target isn't defined — the flow names a connection that doesn't
            resolve to a host or listener.
          </p>
        }

        <nav class="dw__actions">
          @if (simulatorId) {
            <a class="act" [routerLink]="['/simulators', simulatorId]">
              <pp-icon name="server" [size]="14" /> Metrics &amp; chaos
            </a>
          }
          @if (n.flow_id) {
            <a class="act" [routerLink]="['/flow-editor', n.flow_id]">
              <pp-icon name="flow" [size]="14" /> Open flow in editor
            </a>
          }
          @if (n.status === "unresolved") {
            <a class="act" routerLink="/connections">
              <pp-icon name="plug" [size]="14" /> Open Connections
            </a>
          }
          @if (isListener) {
            <a
              class="act"
              routerLink="/network-trace"
              [queryParams]="n.flow_id ? { flow: n.flow_id } : {}"
            >
              <pp-icon name="activity" [size]="14" />
              {{ n.flow_id ? "Traces through this node" : "Network trace" }}
            </a>
            <a class="act" routerLink="/peers">
              <pp-icon name="user" [size]="14" /> Live sessions
            </a>
          }
          <a class="act" routerLink="/diagnostics">
            <pp-icon name="sparkles" [size]="14" /> Diagnose platform
          </a>
        </nav>
      </aside>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono);
        font-size: var(--text-xs);
      }
      .num {
        font-variant-numeric: tabular-nums;
        text-align: right;
      }

      .dw {
        display: flex;
        flex-direction: column;
        gap: var(--space-3);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-xs);
        padding: var(--space-4);
        position: sticky;
        top: var(--space-4);
      }

      .dw__head {
        display: flex;
        align-items: flex-start;
        gap: var(--space-2-5);
      }
      .dw__ico {
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        flex: none;
        border-radius: var(--radius-md);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
      }
      .dw__title {
        min-width: 0;
        flex: 1;
      }
      .dw__title h3 {
        margin: 0;
        font-size: var(--text-base);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .dw__x {
        display: inline-grid;
        place-items: center;
        width: 24px;
        height: 24px;
        flex: none;
        border: none;
        background: transparent;
        color: var(--text-muted);
        cursor: pointer;
        border-radius: var(--radius-sm);
      }
      .dw__x:hover {
        background: var(--bg-subtle);
        color: var(--text-primary);
      }

      .dw__badges {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        flex-wrap: wrap;
      }

      .dw__stats {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-2);
      }
      .st {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: var(--space-2) var(--space-2-5);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-base);
      }
      .st__k {
        font-size: var(--text-xs);
        color: var(--text-muted);
      }
      .st__v {
        font-size: var(--text-lg);
        font-weight: var(--weight-semibold);
        line-height: 1.1;
        display: inline-flex;
        align-items: baseline;
        gap: 3px;
        font-variant-numeric: tabular-nums;
      }
      .st__u {
        font-size: var(--text-xs);
        font-weight: var(--weight-regular);
        color: var(--text-muted);
      }
      .st__v--ok {
        color: var(--color-success);
      }
      .st__v--bad {
        color: var(--color-danger);
      }

      .sec {
        display: flex;
        flex-direction: column;
        gap: var(--space-1-5);
      }
      .sec h4 {
        margin: 0 0 2px;
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-secondary);
      }
      .crow {
        display: grid;
        grid-template-columns: 48px 1fr 48px;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-xs);
      }
      .crow__track {
        height: 6px;
        border-radius: 3px;
        background: var(--bg-subtle);
        overflow: hidden;
      }
      .crow__bar {
        display: block;
        height: 100%;
        border-radius: 3px;
        background: var(--color-success);
      }
      .crow__bar--decl {
        background: var(--color-danger);
      }
      .crow__bar--mix {
        background: var(--brand);
      }

      .hint {
        display: flex;
        align-items: flex-start;
        gap: var(--space-1-5);
        margin: 0;
        font-size: var(--text-sm);
        color: var(--text-primary);
        padding: var(--space-2) var(--space-2-5);
        border-radius: var(--radius-md);
        background: color-mix(in srgb, var(--color-danger) 8%, transparent);
      }
      .hint pp-icon {
        color: var(--color-danger);
        flex: none;
        margin-top: 2px;
      }

      .dw__actions {
        display: flex;
        flex-direction: column;
        gap: var(--space-1);
      }
      .act {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        padding: var(--space-2) var(--space-2-5);
        border-radius: var(--radius-md);
        font-size: var(--text-sm);
        color: var(--text-primary);
        text-decoration: none;
      }
      .act:hover {
        background: var(--bg-subtle);
        color: var(--brand);
      }
      .act pp-icon {
        color: var(--text-secondary);
      }
      .act:hover pp-icon {
        color: var(--brand);
      }
    `,
  ],
})
export class Tm2NodeDrawerComponent {
  readonly node = signal<GraphNode | null>(null);
  @Input({ required: true, alias: "node" }) set nodeIn(n: GraphNode | null) {
    this.node.set(n);
  }
  /** Live msg/s (map-derived — the graph only carries cumulative counts). */
  @Input() rate = 0;
  @Output() closed = new EventEmitter<void>();

  readonly rcRows = computed<CodeRow[]>(() =>
    this.rows(this.node()?.by_rc, "00"),
  );
  readonly mtiRows = computed<CodeRow[]>(() => this.rows(this.node()?.by_mti));

  readonly approval = computed<number | null>(() => {
    const rc = this.node()?.by_rc;
    if (!rc) return null;
    let ok = 0;
    let total = 0;
    for (const [k, v] of Object.entries(rc)) {
      total += v;
      if (k === "00") ok += v;
    }
    return total ? Math.round((ok / total) * 100) : null;
  });

  private rows(
    src: Record<string, number> | undefined,
    okCode?: string,
  ): CodeRow[] {
    if (!src) return [];
    const entries = Object.entries(src).filter(([, v]) => v > 0);
    const total = entries.reduce((a, [, v]) => a + v, 0);
    if (!total) return [];
    return entries
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([code, count]) => ({
        code,
        count,
        share: count / total,
        ok: okCode === undefined || code === okCode,
      }));
  }

  // -- derived identity ----------------------------------------------------
  get simulatorId(): string | null {
    const n = this.node();
    return n && n.kind === "simulator" && n.id.startsWith("sim:")
      ? n.id.slice(4)
      : null;
  }
  get isListener(): boolean {
    const k = this.node()?.kind;
    return k === "participant" || k === "simulator";
  }
  get icon(): string {
    switch (this.node()?.kind) {
      case "participant":
        return "flow";
      case "simulator":
        return "server";
      case "group":
        return "group";
      case "driver":
        return "play";
      case "clients":
        return "user";
      case "host":
        return "cpu";
      default:
        return "plug";
    }
  }
  get sub(): string {
    const n = this.node();
    if (!n) return "";
    const proto = n.protocol || n.sublabel || "";
    return n.port ? `${proto} :${n.port}` : proto || n.sublabel || "";
  }
  get statusTone(): "success" | "warning" | "danger" | "neutral" {
    switch (this.node()?.status) {
      case "up":
        return "success";
      case "degraded":
      case "external":
        return "warning";
      case "down":
      case "unresolved":
        return "danger";
      default:
        return "neutral";
    }
  }
  get totalLabel(): string {
    const r = this.node()?.received ?? 0;
    return r >= 1000 ? (r / 1000).toFixed(1) + "k" : String(r);
  }
  get rcTitle(): string {
    const p = this.node()?.protocol || "";
    return p.includes("http") ? "HTTP status" : "DE39";
  }
}
