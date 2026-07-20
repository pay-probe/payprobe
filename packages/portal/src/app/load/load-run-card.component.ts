import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
  signal,
} from "@angular/core";

import { Diagnosis, LoadProfileType, LoadSnapshot } from "./load-api.service";

/** One point of the fleet time-series, appended off each `load.sample`. */
export interface Point {
  t: number;
  tps: number;
  p95: number;
  p99: number;
}

/**
 * Presentational metrics + charts for a single load run. Driven entirely by
 * inputs so it can be reused both for a live card in the active-run stack and
 * for the (read-only) historical detail page. Controls that only make sense
 * while a run is live (stop / re-tune / scale) stay in the parent; the only
 * action wired here is Diagnose, which is useful live and after the fact.
 */
@Component({
  selector: "pp-load-run-card",
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (snap(); as s) {
      @if (s.draining) {
        <div class="drainbox">
          <span class="drainbox__dot"></span>
          <div class="drainbox__body">
            <strong>Draining</strong> — send window over; waiting for in-flight
            requests to finish.
            <div class="drainbox__meta">
              <span
                >{{
                  (s.pending || 0) + (s.in_flight || 0) | number
                }}
                outstanding</span
              >
              <span>·</span>
              @if ((s.drain_s ?? 0) < 0) {
                <span
                  >waiting for all ({{
                    s.drain_elapsed_s || 0 | number: "1.0-0"
                  }}s)</span
                >
              } @else {
                <span
                  >{{ s.drain_elapsed_s || 0 | number: "1.0-0" }}s /
                  {{ s.drain_s || 0 | number: "1.0-0" }}s budget</span
                >
              }
              @if ((s.aborted || 0) > 0) {
                <span>·</span>
                <span class="warn">{{ s.aborted | number }} timed out</span>
              }
            </div>
          </div>
        </div>
      }

      <!-- throughput / accounting -->
      <h3 class="statgroup statgroup--throughput">Throughput</h3>
      <div class="stats stats--throughput">
        <div class="stat">
          <span class="accent">{{ s.tps | number: "1.0-0" }}</span
          ><label>TPS now</label>
        </div>
        <div class="stat">
          <span>{{ s.target_tps | number: "1.0-0" }}</span
          ><label>target TPS</label>
        </div>
        @if (s.generated != null) {
          <div class="stat">
            <span>{{ s.generated | number }}</span
            ><label>generated</label>
          </div>
        }
        <div class="stat">
          <span>{{ s.sent | number }}</span
          ><label>sent</label>
        </div>
        <div class="stat">
          <span>{{ s.received | number }}</span
          ><label>received</label>
        </div>
        <div class="stat">
          <span
            [class.good]="(s.errors || 0) === 0"
            [class.bad]="(s.errors || 0) > 0"
            >{{ s.errors | number }}</span
          ><label>errors</label>
        </div>
        <div class="stat">
          <span
            [class.good]="successRate() >= 99.5"
            [class.warn]="successRate() < 99.5 && successRate() >= 95"
            [class.bad]="successRate() < 95"
            >{{ successRate() | number: "1.0-2" }}<i>%</i></span
          ><label>success</label>
        </div>
        <div
          class="stat"
          title="Goodput: received ÷ generated — of the offered load, how much succeeded end-to-end (throttled and errors lower it)"
        >
          <span
            [class.good]="goodput() >= 99.5"
            [class.warn]="goodput() < 99.5 && goodput() >= 90"
            [class.bad]="goodput() < 90"
            >{{ goodput() | number: "1.0-2" }}<i>%</i></span
          ><label>goodput</label>
        </div>
        <div class="stat">
          <span>{{ s.pending | number }}</span
          ><label>pending</label>
        </div>
        @if ((s.aborted || 0) > 0) {
          <div class="stat">
            <span class="warn">{{ s.aborted | number }}</span
            ><label>aborted</label>
          </div>
        }
        <div class="stat">
          <span>{{ fmtElapsed(s.send_elapsed_s ?? s.elapsed_s) }}</span
          ><label>send time</label>
        </div>
        @if ((s.drain_elapsed_s || 0) > 0 || s.draining) {
          <div class="stat">
            <span [class.accent]="s.draining">{{
              fmtElapsed(s.drain_elapsed_s || 0)
            }}</span
            ><label>drain time</label>
          </div>
        }
      </div>

      <!-- connections / concurrency -->
      <h3 class="statgroup statgroup--conn">
        {{ type === "soak" ? "Connections" : "Concurrency" }}
      </h3>
      <div class="stats stats--conn">
        @if (type === "soak") {
          <div class="stat">
            <span class="good">{{ s.live | number }}</span
            ><label>live conns</label>
          </div>
          <div class="stat">
            <span [class.warn]="(s.reconnects || 0) > 0">{{
              s.reconnects | number
            }}</span
            ><label>reconnects</label>
          </div>
          <div class="stat">
            <span
              [class.good]="(s.dead || 0) === 0"
              [class.bad]="(s.dead || 0) > 0"
              >{{ s.dead | number }}</span
            ><label>dead conns</label>
          </div>
        } @else {
          <div class="stat">
            <span
              [class.good]="
                (s.workers_reporting || 0) >= (s.workers || workers)
              "
              [class.warn]="(s.workers_reporting || 0) < (s.workers || workers)"
              >{{ s.workers_reporting || 0 }}/{{ s.workers || workers }}</span
            ><label>workers</label>
          </div>
          @if (s.max_in_flight) {
            <div class="stat">
              <span
                [class.warn]="
                  (s.saturation || 0) >= 0.8 && (s.saturation || 0) < 0.99
                "
                [class.bad]="(s.saturation || 0) >= 0.99"
                >{{ s.in_flight || 0 | number }}/{{
                  s.max_in_flight | number
                }}</span
              ><label>in-flight</label>
            </div>
            <div class="stat">
              <span>{{ s.peak_in_flight || 0 | number }}</span
              ><label>peak in-flight</label>
            </div>
            <div class="stat">
              <span
                [class.good]="(s.saturation || 0) < 0.8"
                [class.warn]="
                  (s.saturation || 0) >= 0.8 && (s.saturation || 0) < 0.99
                "
                [class.bad]="(s.saturation || 0) >= 0.99"
                >{{ (s.saturation || 0) * 100 | number: "1.0-0" }}<i>%</i></span
              ><label>saturation</label>
            </div>
            <div class="stat">
              <span
                [class.good]="(s.throttled || 0) === 0"
                [class.bad]="(s.throttled || 0) > 0"
                >{{ s.throttled || 0 | number }}</span
              ><label>throttled</label>
            </div>
          }
          @if ((s.reconnects || 0) > 0 || (s.dead || 0) > 0) {
            <div class="stat">
              <span [class.bad]="(s.dead || 0) > 0">{{ s.dead | number }}</span
              ><label>dead conns</label>
            </div>
          }
        }
      </div>

      <!-- latency distribution -->
      <h3 class="statgroup statgroup--latency">Latency</h3>
      <div class="stats stats--latency">
        <div class="stat">
          <span class="good"
            >{{ s.latency_ms.p50 | number: "1.0-1" }}<i>ms</i></span
          ><label>p50</label>
        </div>
        <div class="stat">
          <span class="good"
            >{{ s.latency_ms.mean | number: "1.0-1" }}<i>ms</i></span
          ><label>avg</label>
        </div>
        <div class="stat">
          <span class="warn"
            >{{ s.latency_ms.p95 | number: "1.0-1" }}<i>ms</i></span
          ><label>p95</label>
        </div>
        <div class="stat">
          <span class="warn"
            >{{ s.latency_ms.p99 | number: "1.0-1" }}<i>ms</i></span
          ><label>p99</label>
        </div>
        <div class="stat">
          <span class="bad"
            >{{ s.latency_ms.max | number: "1.0-1" }}<i>ms</i></span
          ><label>max</label>
        </div>
        <div class="stat">
          <span>{{ s.latency_ms.samples | number }}</span
          ><label>samples</label>
        </div>
      </div>

      @if (s.max_in_flight && (s.saturation || 0) >= 0.99) {
        <div class="noticebox">
          <strong>Saturated.</strong> Every in-flight slot is busy and
          {{ s.throttled || 0 | number }} sends have been throttled — the target
          is the bottleneck, not the fleet. Lower the target TPS or add
          capacity.
        </div>
      }

      @if (s.notice) {
        <div class="noticebox">
          <strong>Action needed.</strong> <span>{{ s.notice }}</span>
        </div>
      }

      @if (s.errors > 0 && s.last_error) {
        <div class="errbox">
          <strong>Transactions are failing.</strong> Last error:
          <code>{{ s.last_error }}</code>
        </div>
      }

      @if (s.errors > 0 || !running) {
        <div class="diag">
          <button
            class="btn btn--sm"
            (click)="diagnose.emit()"
            [disabled]="diagnosing"
          >
            {{ diagnosing ? "Diagnosing…" : "Diagnose this run" }}
          </button>
          @if (diagnosis; as dg) {
            <div class="findings">
              @for (f of dg.findings; track f.code) {
                <div class="finding finding--{{ f.severity }}">
                  <div class="ft">{{ f.title }}</div>
                  <div class="fc">{{ f.cause }}</div>
                  <div class="fs"><strong>Try:</strong> {{ f.suggestion }}</div>
                  @if (f.evidence) {
                    <code class="fe">{{ f.evidence }}</code>
                  }
                </div>
              }
            </div>
          }
        </div>
      }

      <div class="charts">
        <div class="chart">
          <h3>Throughput (TPS)</h3>
          <svg viewBox="0 0 320 80" preserveAspectRatio="none">
            <polyline
              [attr.points]="tpsPath()"
              fill="none"
              class="spark spark--tps"
            />
          </svg>
        </div>
        <div class="chart">
          <h3>Latency p95 / p99 (ms)</h3>
          <svg viewBox="0 0 320 80" preserveAspectRatio="none">
            <polyline
              [attr.points]="p95Path()"
              fill="none"
              class="spark spark--p95"
            />
            <polyline
              [attr.points]="p99Path()"
              fill="none"
              class="spark spark--p99"
            />
          </svg>
        </div>
      </div>
    } @else {
      <p class="muted empty">
        No samples yet — waiting for the first fleet report.
      </p>
    }
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .muted {
        color: var(--pp-text-muted, #8a93a6);
      }
      .statgroup {
        display: flex;
        align-items: center;
        gap: 0.35rem;
        margin: 0.5rem 0 0;
        font-size: 0.6rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--pp-text-muted, #8a93a6);
      }
      .statgroup::before {
        content: "";
        width: 7px;
        height: 7px;
        border-radius: 2px;
        background: currentColor;
      }
      .statgroup--throughput::before {
        background: #2f6fed;
      }
      .statgroup--conn::before {
        background: #30a46c;
      }
      .statgroup--latency::before {
        background: #f5a524;
      }
      .stats {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(84px, 1fr));
        gap: 1px;
        margin: 0.25rem 0 0;
        background: var(--pp-border, #e6e8ee);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-left-width: 3px;
        border-radius: 8px;
        overflow: hidden;
      }
      .stats--throughput {
        border-left-color: #2f6fed;
      }
      .stats--conn {
        border-left-color: #30a46c;
      }
      .stats--latency {
        border-left-color: #f5a524;
      }
      .stat {
        display: flex;
        flex-direction: column-reverse;
        gap: 0.1rem;
        background: var(--pp-surface, #fff);
        padding: 0.38rem 0.5rem;
        transition: background 0.12s ease;
      }
      .stat:hover {
        background: var(--pp-surface-2, #fafbfc);
      }
      .stat span {
        font-size: 1rem;
        font-weight: 700;
        line-height: 1.1;
        display: block;
        font-variant-numeric: tabular-nums;
        letter-spacing: -0.01em;
      }
      .stat span i {
        font-size: 0.62rem;
        font-weight: 600;
        color: var(--pp-text-muted, #8a93a6);
        font-style: normal;
        margin-left: 0.05rem;
      }
      .stat span.good {
        color: #2a9d63;
      }
      .stat span.warn {
        color: #d98a0b;
      }
      .stat span.bad {
        color: #e5484d;
      }
      .stat span.accent {
        color: var(--pp-primary, #2f6fed);
      }
      .stat label {
        font-size: 0.58rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--pp-text-muted, #8a93a6);
      }
      .charts {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.6rem;
        margin-top: 0.6rem;
      }
      .chart h3 {
        margin: 0 0 0.25rem;
        font-size: 0.68rem;
        color: var(--pp-text-muted, #8a93a6);
      }
      .chart svg {
        width: 100%;
        height: 44px;
        background: var(--pp-surface-2, #fafbfc);
        border: 1px solid var(--pp-border, #e6e8ee);
        border-radius: 8px;
      }
      .spark {
        stroke-width: 1.6;
        vector-effect: non-scaling-stroke;
      }
      .spark--tps {
        stroke: #2f6fed;
      }
      .spark--p95 {
        stroke: #f5a524;
      }
      .spark--p99 {
        stroke: #e5484d;
      }
      .errbox {
        margin-top: 0.6rem;
        padding: 0.5rem 0.65rem;
        border-radius: 8px;
        background: rgba(229, 72, 77, 0.1);
        border: 1px solid rgba(229, 72, 77, 0.35);
        font-size: 0.78rem;
      }
      .errbox code {
        display: block;
        margin-top: 0.25rem;
        word-break: break-word;
        font-size: 0.74rem;
        opacity: 0.9;
      }
      .noticebox {
        margin-top: 0.6rem;
        padding: 0.5rem 0.65rem;
        border-radius: 8px;
        background: rgba(245, 165, 36, 0.12);
        border: 1px solid rgba(245, 165, 36, 0.4);
        font-size: 0.78rem;
      }
      .drainbox {
        display: flex;
        align-items: flex-start;
        gap: 0.55rem;
        margin: 0 0 0.6rem;
        padding: 0.55rem 0.7rem;
        border-radius: 8px;
        background: rgba(56, 132, 255, 0.1);
        border: 1px solid rgba(56, 132, 255, 0.4);
        font-size: 0.78rem;
      }
      .drainbox__dot {
        margin-top: 0.3rem;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--pp-primary, #3884ff);
        flex: 0 0 auto;
        animation: drainpulse 1.1s ease-in-out infinite;
      }
      @keyframes drainpulse {
        0%,
        100% {
          opacity: 0.35;
          transform: scale(0.8);
        }
        50% {
          opacity: 1;
          transform: scale(1.15);
        }
      }
      .drainbox__meta {
        margin-top: 0.2rem;
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        color: var(--pp-text-muted, #6b7280);
        font-variant-numeric: tabular-nums;
      }
      .drainbox__meta .warn {
        color: var(--pp-warn, #d98315);
        font-weight: 600;
      }
      .diag {
        margin-top: 0.6rem;
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
      .btn--sm:disabled {
        opacity: 0.6;
        cursor: default;
      }
      .findings {
        margin-top: 0.6rem;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      .finding {
        border-radius: 8px;
        padding: 0.55rem 0.7rem;
        border: 1px solid var(--pp-border, #e6e8ee);
        font-size: 0.82rem;
      }
      .finding--error {
        background: rgba(229, 72, 77, 0.08);
        border-color: rgba(229, 72, 77, 0.35);
      }
      .finding--warn {
        background: rgba(245, 165, 36, 0.1);
        border-color: rgba(245, 165, 36, 0.35);
      }
      .finding--info {
        background: rgba(48, 164, 108, 0.08);
        border-color: rgba(48, 164, 108, 0.3);
      }
      .finding .ft {
        font-weight: 700;
      }
      .finding .fc {
        margin-top: 0.15rem;
      }
      .finding .fs {
        margin-top: 0.3rem;
        opacity: 0.95;
      }
      .finding .fe {
        display: block;
        margin-top: 0.3rem;
        font-size: 0.75rem;
        opacity: 0.8;
        word-break: break-word;
      }
      .empty {
        padding: 1.5rem 0;
        text-align: center;
      }
    `,
  ],
})
export class LoadRunCardComponent {
  private readonly _snap = signal<LoadSnapshot | null>(null);
  private readonly _points = signal<Point[]>([]);

  @Input({ required: true }) set snapshot(v: LoadSnapshot | null) {
    this._snap.set(v);
  }
  @Input() set points(v: Point[]) {
    this._points.set(v ?? []);
  }
  @Input() type: LoadProfileType = "steady";
  @Input() workers = 1;
  @Input() running = false;
  @Input() diagnosing = false;
  @Input() diagnosis: Diagnosis | null = null;

  @Output() diagnose = new EventEmitter<void>();

  /** Exposed for the template. */
  readonly snap = this._snap;

  /** Response success rate (received / (received + errors)), %. Target-health:
   *  of the requests that got a response, how many succeeded. Throttled/pending
   *  are excluded — they never reached the target. */
  readonly successRate = computed(() => {
    const s = this._snap();
    if (!s) return 100;
    const done = (s.received || 0) + (s.errors || 0);
    return done ? (s.received / done) * 100 : 100;
  });

  /** Goodput (received / generated), %. Load-fulfillment: of the offered load
   *  the pacer produced, how much succeeded end-to-end. Throttled, errors,
   *  aborted and still-pending all lower it — so a heavily-throttled run does
   *  NOT read as a clean pass. Falls back to `sent` when `generated` is absent
   *  (an older worker that doesn't report it). */
  readonly goodput = computed(() => {
    const s = this._snap();
    if (!s) return 100;
    const offered = s.generated ?? s.sent ?? 0;
    return offered ? ((s.received || 0) / offered) * 100 : 100;
  });

  readonly tpsPath = computed(() => this.path((p) => p.tps));
  readonly p95Path = computed(() => this.path((p) => p.p95));
  readonly p99Path = computed(() => this.path((p) => p.p99, this.maxLatency()));

  private maxLatency(): number {
    return Math.max(1, ...this._points().map((p) => p.p99));
  }

  fmtElapsed(sec: number): string {
    const total = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(total / 60);
    const r = total % 60;
    return m ? `${m}m ${r}s` : `${r}s`;
  }

  private path(pick: (p: Point) => number, maxOverride?: number): string {
    const pts = this._points();
    if (pts.length === 0) return "";
    const max = maxOverride ?? Math.max(1, ...pts.map(pick));
    const n = Math.max(1, pts.length - 1);
    return pts
      .map((p, i) => {
        const x = (i / n) * 320;
        const y = 78 - (pick(p) / max) * 74;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }
}
