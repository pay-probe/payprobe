import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from "@angular/core";

import { GraphNode } from "../../run-monitor/run-api.service";
import { IconComponent } from "../../shared/ui/icon.component";
import {
  MixRow,
  POLL_MS,
  SPARK_H,
  SPARK_W,
  kindIcon,
  mixRows,
  nodeSub,
  seriesPoints,
} from "./chronoscope.model";

/**
 * Chronoscope side panel: the selected node's status, rate history over the
 * buffer (up to the displayed frame), and its DE39 / MTI traffic mix.
 * Purely presentational — everything derives from the inputs.
 */
@Component({
  selector: "app-chrono-inspector",
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="panel">
      <div class="panel__head">Node inspector</div>
      @if (!node()) {
        <p class="muted sm panel__hint">
          Click a node on the scope to inspect its history and traffic mix.
        </p>
      } @else {
        <div class="insp">
          <div class="insp__title">
            <span class="insp__ico"
              ><pp-icon [name]="icon()" [size]="16"
            /></span>
            <span class="insp__name">{{ node()!.label }}</span>
            <span class="insp__kind">{{ node()!.kind }}</span>
          </div>
          <dl class="insp__facts">
            <div>
              <dt>Status</dt>
              <dd [attr.data-status]="node()!.status">{{ node()!.status }}</dd>
            </div>
            <div>
              <dt>Endpoint</dt>
              <dd class="mono">{{ sub() || "—" }}</dd>
            </div>
            <div>
              <dt>Peers</dt>
              <dd>{{ node()!.peers ?? "—" }}</dd>
            </div>
            <div>
              <dt>Messages</dt>
              <dd>{{ node()!.received }}</dd>
            </div>
            <div>
              <dt>Rate</dt>
              <dd>{{ rate() }}/s</dd>
            </div>
          </dl>
          @if (spark()) {
            <div class="insp__sec">Rate · last {{ sparkSpan() }}s</div>
            <svg
              [attr.viewBox]="'0 0 ' + SPARK_W + ' ' + SPARK_H"
              preserveAspectRatio="none"
              class="insp__spark"
            >
              <polygon [attr.points]="sparkArea()" class="spark__area" />
              <polyline [attr.points]="spark()" class="spark__line" />
            </svg>
          }
          @if (rcMix().length) {
            <div class="insp__sec">Responses (DE39)</div>
            @for (r of rcMix(); track r.k) {
              <div class="mix">
                <span class="mono mix__k">{{ r.k }}</span>
                <span class="mix__track"
                  ><i
                    [style.width.%]="r.w"
                    [class.mix__bar--ok]="r.ok"
                    [class.mix__bar--bad]="!r.ok"
                    class="mix__bar"
                  ></i
                ></span>
                <span class="mix__v">{{ r.v }}</span>
              </div>
            }
          }
          @if (mtiMix().length) {
            <div class="insp__sec">Traffic mix (MTI)</div>
            @for (r of mtiMix(); track r.k) {
              <div class="mix">
                <span class="mono mix__k">{{ r.k }}</span>
                <span class="mix__track"
                  ><i [style.width.%]="r.w" class="mix__bar"></i
                ></span>
                <span class="mix__v">{{ r.v }}</span>
              </div>
            }
          }
        </div>
      }
    </section>
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono);
      }
      .panel {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-3) var(--space-4);
        box-shadow: var(--shadow-xs);
      }
      .panel__head {
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-muted);
        margin-bottom: var(--space-2);
      }
      .panel__hint {
        margin: var(--space-1) 0;
      }

      .insp__title {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-2);
      }
      .insp__ico {
        display: inline-flex;
        color: var(--text-secondary);
      }
      .insp__name {
        font-weight: var(--weight-semibold);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .insp__kind {
        margin-left: auto;
        font-size: var(--text-xs);
        color: var(--text-muted);
        border: 1px solid var(--border);
        border-radius: var(--radius-full);
        padding: 1px var(--space-2);
      }
      .insp__facts {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-1) var(--space-3);
        margin: 0 0 var(--space-2);
      }
      .insp__facts div {
        display: flex;
        justify-content: space-between;
        gap: var(--space-2);
        font-size: var(--text-sm);
        min-width: 0;
      }
      .insp__facts dt {
        color: var(--text-muted);
      }
      .insp__facts dd {
        margin: 0;
        font-variant-numeric: tabular-nums;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .insp__facts dd[data-status="up"] {
        color: var(--color-success);
      }
      .insp__facts dd[data-status="down"],
      .insp__facts dd[data-status="unresolved"] {
        color: var(--color-danger);
      }
      .insp__sec {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-muted);
        margin: var(--space-2-5) 0 var(--space-1);
      }
      .insp__spark {
        width: 100%;
        height: 40px;
        background: var(--bg-base);
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
      }
      .spark__area {
        fill: var(--brand);
        opacity: 0.14;
      }
      .spark__line {
        fill: none;
        stroke: var(--brand);
        stroke-width: 1.5;
        vector-effect: non-scaling-stroke;
      }

      .mix {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-xs);
        margin-bottom: 3px;
      }
      .mix__k {
        flex: none;
        width: 42px;
        color: var(--text-secondary);
      }
      .mix__track {
        flex: 1;
        height: 5px;
        background: var(--bg-subtle);
        border-radius: 3px;
        overflow: hidden;
      }
      .mix__bar {
        display: block;
        height: 100%;
        background: var(--brand);
        border-radius: 3px;
      }
      .mix__bar--ok {
        background: var(--color-success);
      }
      .mix__bar--bad {
        background: var(--color-danger);
      }
      .mix__v {
        flex: none;
        min-width: 34px;
        text-align: right;
        font-variant-numeric: tabular-nums;
        color: var(--text-muted);
      }
    `,
  ],
})
export class NodeInspectorComponent {
  readonly SPARK_W = SPARK_W;
  readonly SPARK_H = SPARK_H;

  readonly node = input<GraphNode | null>(null);
  readonly rate = input(0);
  /** Rate history (msg/s per frame) up to the displayed frame. */
  readonly sparkVals = input<number[]>([]);

  readonly icon = computed(() => kindIcon(this.node()?.kind ?? ""));
  readonly sub = computed(() => {
    const n = this.node();
    return n ? nodeSub(n) : "";
  });
  readonly sparkSpan = computed(
    () => this.sparkVals().length * (POLL_MS / 1000),
  );
  readonly spark = computed(() =>
    seriesPoints(this.sparkVals(), SPARK_W, SPARK_H),
  );
  readonly sparkArea = computed(() => {
    const pts = this.spark();
    if (!pts) return "";
    return "0," + SPARK_H + " " + pts + " " + SPARK_W + "," + SPARK_H;
  });
  readonly rcMix = computed<MixRow[]>(() =>
    mixRows(this.node()?.by_rc, (k) => k === "00"),
  );
  readonly mtiMix = computed<MixRow[]>(() =>
    mixRows(this.node()?.by_mti, () => true).map((r) => ({ ...r, ok: false })),
  );
}
