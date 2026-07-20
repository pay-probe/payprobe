import { Component, OnDestroy, computed, inject, signal } from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, Router } from "@angular/router";

import {
  ParticipantsService,
  TraceHop,
  TxnSummary,
} from "../participants/participants.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface WfRow {
  label: string;
  ok: boolean;
  /** percentages of the total span, for the bar track */
  left: number;
  width: number;
  /** absolute ms, for the row caption */
  offMs: number;
  durMs: number;
}

/** Network Trace — pick a recent transaction and see its full end-to-end path
 *  across the participant-flow network: a timing waterfall over the hops,
 *  every hop's downstream calls, the request/response DEs, and the failing
 *  hop + reason highlighted. */
@Component({
  selector: "app-network-trace",
  standalone: true,
  imports: [FormsModule, PageHeaderComponent],
  styles: [
    `
      .nt {
        display: grid;
        grid-template-columns: 22rem 1fr;
        gap: 12px;
        padding: 12px;
        height: 100%;
        box-sizing: border-box;
      }
      .list {
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: 10px;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        border-bottom: 1px solid var(--border);
        cursor: pointer;
        font-size: 13px;
      }
      .row:hover {
        background: var(--bg-subtle);
      }
      .row.sel {
        background: color-mix(in srgb, var(--brand) 12%, transparent);
      }
      .cid {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .kind {
        font-size: 0.62rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--text-muted);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 1px 5px;
        flex: 0 0 auto;
      }
      .time {
        margin-left: auto;
        flex: 0 0 auto;
        font-size: 11px;
        font-variant-numeric: tabular-nums;
        color: var(--text-muted);
      }
      .cap {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
        color: var(--text-muted);
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex: 0 0 auto;
      }
      .ok {
        background: var(--color-success);
      }
      .bad {
        background: var(--color-danger);
      }
      .muted {
        color: var(--text-muted);
        font-size: 12px;
      }
      .cap-banner {
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin: 10px;
        padding: 12px 14px;
        border: 1px solid var(--border);
        border-left: 3px solid var(--color-warning);
        border-radius: 10px;
        background: color-mix(in srgb, var(--color-warning) 8%, transparent);
        font-size: 13px;
      }
      .cap-resume {
        align-self: flex-start;
        margin-top: 4px;
        padding: 6px 12px;
        border: none;
        border-radius: 8px;
        background: var(--brand);
        color: #fff;
        font-weight: 600;
        cursor: pointer;
      }
      .cap-resume:hover {
        filter: brightness(1.05);
      }
      .search {
        width: 100%;
        padding: 8px 10px;
        border: none;
        border-bottom: 1px solid var(--border);
        background: var(--bg-base);
        color: var(--text-primary);
        box-sizing: border-box;
      }
      .flowchip {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 8px 10px 0;
        padding: 4px 8px;
        font-size: 12px;
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 10%, transparent);
        border-radius: 999px;
        width: fit-content;
      }
      .flowchip__x {
        border: none;
        background: transparent;
        color: inherit;
        cursor: pointer;
        font-size: 11px;
        padding: 0 2px;
      }
      .detail {
        overflow: auto;
        padding: 4px;
      }

      /* timing waterfall over the hops */
      .wf {
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 10px 12px;
        margin-bottom: 10px;
        background: var(--bg-surface);
      }
      .wf__head {
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--text-muted);
        margin-bottom: 6px;
      }
      .wf__row {
        display: grid;
        grid-template-columns: minmax(120px, 14rem) 1fr 84px;
        align-items: center;
        gap: 10px;
        font-size: 12px;
        padding: 2px 0;
      }
      .wf__lbl {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .wf__track {
        position: relative;
        height: 10px;
        border-radius: 5px;
        background: var(--bg-subtle);
      }
      .wf__bar {
        position: absolute;
        top: 0;
        height: 100%;
        border-radius: 5px;
        background: var(--brand);
        min-width: 3px;
      }
      .wf__bar--bad {
        background: var(--color-danger);
      }
      .wf__ms {
        text-align: right;
        font-variant-numeric: tabular-nums;
        color: var(--text-muted);
      }

      .hop {
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 10px 12px;
        margin-bottom: 10px;
      }
      .hop.fail {
        border-color: var(--color-danger);
      }
      .hop__head {
        display: flex;
        align-items: center;
        gap: 8px;
        font-weight: 600;
      }
      .call {
        margin: 6px 0 0 16px;
        padding: 6px 10px;
        border-left: 2px solid var(--border);
        font-size: 13px;
      }
      .call.fail {
        border-left-color: var(--color-danger);
      }
      .err {
        color: var(--color-danger);
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 12px;
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 12px;
      }
      .placeholder {
        display: grid;
        place-items: center;
        color: var(--text-muted);
        height: 100%;
      }
    `,
  ],
  template: `
    <pp-page-header subtitle="Follow one transaction across the flow network">
      @if (capturing() !== null) {
        <span
          class="cap"
          [title]="
            capturing()
              ? 'Recording transactions'
              : 'Recording paused (listeners still serving)'
          "
        >
          <span
            class="dot"
            [class.ok]="capturing()"
            [class.bad]="!capturing()"
          ></span>
          {{ capturing() ? "Capturing" : "Paused" }}
        </span>
        <button
          class="btn btn--sm"
          (click)="toggleCapture()"
          [title]="
            capturing()
              ? 'Pause trace recording (listeners keep serving)'
              : 'Resume trace recording'
          "
        >
          {{ capturing() ? "⏸ Pause" : "▶ Start capture" }}
        </button>
      }
      <button
        class="btn btn--sm"
        (click)="clearTraces()"
        title="Empty the trace buffer"
      >
        Clear
      </button>
      <button class="btn btn--sm" (click)="reload()">Reload</button>
    </pp-page-header>

    <div class="nt">
      <div class="list">
        <input
          class="search"
          placeholder="Search by STAN / correlation id…"
          [(ngModel)]="query"
          (ngModelChange)="onQuery()"
        />
        @if (flow()) {
          <div
            class="flowchip"
            title="Deep-linked from the map — only transactions through this flow"
          >
            via <span class="mono">{{ flow() }}</span>
            <button
              type="button"
              class="flowchip__x"
              (click)="clearFlow()"
              aria-label="Remove flow filter"
            >
              ✕
            </button>
          </div>
        }
        @for (t of transactions(); track t.correlation_id) {
          <div
            class="row"
            [class.sel]="selected()?.correlation_id === t.correlation_id"
            (click)="open(t)"
          >
            <span class="dot" [class.ok]="t.ok" [class.bad]="!t.ok"></span>
            <span class="kind">{{ kindLabel(t.kind) }}</span>
            <span class="cid">{{ t.correlation_id }}</span>
            <span class="muted">{{ t.mti }} · {{ t.hops }} hops</span>
            <span class="time">{{ timeLabel(t.ts) }}</span>
          </div>
        } @empty {
          @if (capturing() === false) {
            <!-- the #1 confused moment: empty because capture is OFF by default.
                 Say so and offer the fix inline instead of making the user hunt
                 for the toggle. -->
            <div class="cap-banner">
              <strong>Trace capture is paused.</strong>
              <span class="muted"
                >Nothing is being recorded — resume capture, then drive traffic
                to see transactions.</span
              >
              <button
                type="button"
                class="cap-resume"
                (click)="toggleCapture()"
              >
                ▶ Resume capture
              </button>
            </div>
          } @else {
            <div class="muted" style="padding: 10px">
              No transactions yet. Drive traffic through a network — the list
              refreshes on its own while capture is on.
            </div>
          }
        }
      </div>

      <div class="detail">
        @if (hops().length) {
          @if (waterfall(); as wf) {
            <div class="wf">
              <div class="wf__head">
                <span>Timing</span>
                <span>{{ wf.total }} ms end to end</span>
              </div>
              @for (r of wf.rows; track $index) {
                <div class="wf__row">
                  <span class="wf__lbl">{{ r.label }}</span>
                  <span class="wf__track">
                    <span
                      class="wf__bar"
                      [class.wf__bar--bad]="!r.ok"
                      [style.left.%]="r.left"
                      [style.width.%]="r.width"
                    ></span>
                  </span>
                  <span class="wf__ms">+{{ r.offMs }} · {{ r.durMs }} ms</span>
                </div>
              }
            </div>
          }
          @for (h of hops(); track $index) {
            <div class="hop" [class.fail]="h.ok === false">
              <div class="hop__head">
                <span
                  class="dot"
                  [class.ok]="h.ok !== false"
                  [class.bad]="h.ok === false"
                ></span>
                <span class="kind">{{ kindLabel(h.kind) }}</span>
                {{ h.flow_name || h.flow_id }}
                <span class="muted">:{{ h.port }} · {{ h.mti }}</span>
                @if (h.response_code) {
                  <span class="muted"
                    >→ {{ rcLabel(h.kind) }} {{ h.response_code }}</span
                  >
                }
              </div>
              @if (reqLine(h); as rl) {
                <div class="mono muted">in: {{ rl }}</div>
              }
              @if (h.error) {
                <div class="err">✗ {{ h.error }}</div>
              }
              @for (c of h.calls; track $index) {
                <div class="call" [class.fail]="c.ok === false">
                  <span
                    class="dot"
                    [class.ok]="c.ok !== false"
                    [class.bad]="c.ok === false"
                  ></span>
                  <span class="kind">{{ kindLabel(c.kind) }}</span>
                  → {{ c.target }} · {{ c.action }}
                  <span class="muted">{{ c.duration_ms }}ms</span>
                  @if (c.error) {
                    <div class="err">✗ {{ c.error }}</div>
                  } @else {
                    <div class="mono muted">out: {{ fmt(c.response) }}</div>
                  }
                </div>
              }
            </div>
          }
        } @else {
          <div class="placeholder">
            Pick a transaction on the left to see its full path.
          </div>
        }
      </div>
    </div>
  `,
})
export class NetworkTraceComponent implements OnDestroy {
  private readonly svc = inject(ParticipantsService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly transactions = signal<TxnSummary[]>([]);
  readonly selected = signal<TxnSummary | null>(null);
  readonly hops = signal<TraceHop[]>([]);
  /** null until the capture state is known (hides the Pause/Resume button). */
  readonly capturing = signal<boolean | null>(null);
  /** Flow filter, deep-linked from the map's node drawer (?flow=…). */
  readonly flow = signal<string | null>(
    this.route.snapshot.queryParamMap.get("flow"),
  );
  query = "";

  private searchT: ReturnType<typeof setTimeout> | null = null;
  private autoT: ReturnType<typeof setInterval> | null = null;

  constructor() {
    this.reload();
    this.refreshCapture();
    // gentle keep-fresh: while capture is on and the tab is visible, new
    // transactions appear without hammering Reload.
    this.autoT = setInterval(() => {
      if (this.capturing() === true && !document.hidden) this.reload();
    }, 5000);
  }

  ngOnDestroy(): void {
    if (this.searchT) clearTimeout(this.searchT);
    if (this.autoT) clearInterval(this.autoT);
  }

  /** Debounced search — one request after typing settles, not per keystroke. */
  onQuery(): void {
    if (this.searchT) clearTimeout(this.searchT);
    this.searchT = setTimeout(() => this.reload(), 300);
  }

  reload(): void {
    this.svc
      .recentTraces(this.query.trim() || undefined, this.flow() || undefined)
      .subscribe({
        next: (r) => this.transactions.set(r.transactions ?? []),
        error: () => this.transactions.set([]),
      });
  }

  clearFlow(): void {
    this.flow.set(null);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { flow: null },
      queryParamsHandling: "merge",
      replaceUrl: true,
    });
    this.reload();
  }

  open(t: TxnSummary): void {
    this.selected.set(t);
    this.svc.trace(t.correlation_id).subscribe({
      next: (r) => this.hops.set(r.hops ?? []),
      error: () => this.hops.set([]),
    });
  }

  // -- timing waterfall ------------------------------------------------------

  /** Hop offsets from hop timestamps; hop duration from its calls' ms. */
  readonly waterfall = computed<{ rows: WfRow[]; total: number } | null>(() => {
    const hops = this.hops();
    if (hops.length < 2) return null;
    const stamps = hops.map((h) => h.ts ?? 0).filter((t) => t > 0);
    if (!stamps.length) return null;
    const t0 = Math.min(...stamps);
    const raw = hops.map((h, i) => {
      const offMs = h.ts ? Math.max(0, Math.round((h.ts - t0) * 1000)) : 0;
      const durMs = Math.max(
        1,
        Math.round(
          (h.calls ?? []).reduce((a, c) => a + (c.duration_ms || 0), 0),
        ),
      );
      return {
        label: h.flow_name || h.flow_id || "hop " + (i + 1),
        ok: h.ok !== false,
        offMs,
        durMs,
      };
    });
    const total = Math.max(1, ...raw.map((r) => r.offMs + r.durMs));
    return {
      total,
      rows: raw.map((r) => ({
        ...r,
        left: (r.offMs / total) * 100,
        width: Math.max(1.5, (r.durMs / total) * 100),
      })),
    };
  });

  timeLabel(ts?: number): string {
    return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
  }

  // -- capture controls ----------------------------------------------------

  refreshCapture(): void {
    this.svc.captureState().subscribe({
      next: (s) => this.capturing.set(s.capturing),
      error: () => this.capturing.set(null),
    });
  }

  toggleCapture(): void {
    const next = !this.capturing();
    this.svc.setCapture(next).subscribe({
      next: (s) => this.capturing.set(s.capturing),
      error: () => this.refreshCapture(),
    });
  }

  clearTraces(): void {
    this.svc.clearTraces().subscribe({
      next: () => {
        this.transactions.set([]);
        this.hops.set([]);
        this.selected.set(null);
      },
      error: () => this.reload(),
    });
  }

  // -- adapter-aware rendering --------------------------------------------

  kindLabel(kind?: string): string {
    switch (kind) {
      case "http":
        return "HTTP";
      case "hsm":
        return "HSM";
      case "grpc":
        return "gRPC";
      case "db":
        return "DB";
      case "group":
        return "GROUP";
      case "tcp":
      case undefined:
      case null:
        return "ISO";
      default:
        return String(kind).toUpperCase();
    }
  }

  /** Label for the reply-code chip on a hop head, by adapter. */
  rcLabel(kind?: string): string {
    return kind === "http" ? "HTTP" : kind === "hsm" ? "rc" : "DE39";
  }

  /** One-line inbound view, shaped per adapter: HTTP → "METHOD path · body",
   *  ISO/HSM → the DE map. Returns "" when there's nothing to show. */
  reqLine(h: TraceHop): string {
    const r = h.request;
    if (!r) return "";
    if (h.kind === "http") {
      const head = `${r.method ?? "?"} ${r.path ?? ""}`.trim();
      const body = r.body == null ? "" : ` · ${this.fmt(r.body)}`;
      return head + body;
    }
    if (r.de && Object.keys(r.de).length) return this.fmt(r.de);
    return "";
  }

  fmt(v: unknown): string {
    if (v === null || v === undefined) return "—";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
}
