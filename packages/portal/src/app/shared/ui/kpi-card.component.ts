import { Component, Input, ChangeDetectionStrategy } from "@angular/core";
import { IconComponent } from "./icon.component";

@Component({
  selector: "pp-kpi-card",
  standalone: true,
  imports: [IconComponent],
  template: `
    <div class="kpi">
      <div class="top">
        <span class="label">{{ label }}</span>
        @if (icon) {
          <span class="ico"><pp-icon [name]="icon" [size]="18" /></span>
        }
      </div>

      <div class="value mono">{{ value }}</div>

      <div class="foot">
        @if (delta !== undefined) {
          <span
            class="delta"
            [class.up]="isPositive"
            [class.down]="!isPositive"
          >
            <pp-icon
              [name]="isPositive ? 'up' : 'down'"
              [size]="14"
              [strokeWidth]="2.5"
            />
            {{ deltaAbs }}{{ deltaSuffix }}
          </span>
        }
        @if (caption) {
          <span class="caption">{{ caption }}</span>
        }

        @if (spark.length > 1) {
          <svg
            class="spark"
            [attr.viewBox]="'0 0 ' + sparkW + ' ' + sparkH"
            preserveAspectRatio="none"
          >
            <polyline
              [attr.points]="sparkPoints"
              fill="none"
              [attr.stroke]="
                isPositive ? 'var(--color-success)' : 'var(--color-danger)'
              "
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        }
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
      }
      .kpi {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        padding: var(--space-5);
        box-shadow: var(--shadow-xs);
        height: 100%;
        transition:
          box-shadow var(--duration-normal) var(--ease-out),
          transform var(--duration-normal) var(--ease-out);
      }
      .kpi:hover {
        box-shadow: var(--shadow-md);
        transform: translateY(-2px);
      }
      .top {
        display: flex;
        align-items: center;
        justify-content: space-between;
      }
      .label {
        font-size: var(--text-sm);
        color: var(--text-secondary);
        font-weight: var(--weight-medium);
      }
      .ico {
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        border-radius: var(--radius-md);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
      }
      .value {
        font-size: var(--text-3xl);
        font-weight: var(--weight-semibold);
        letter-spacing: var(--tracking-tight);
        margin: var(--space-3) 0 var(--space-2);
        color: var(--text-primary);
      }
      .foot {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        flex-wrap: wrap;
      }
      .delta {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        font-size: var(--text-xs);
        font-weight: var(--weight-semibold);
        padding: 2px 7px 2px 5px;
        border-radius: var(--radius-full);
      }
      .delta.up {
        color: var(--color-success);
        background: color-mix(in srgb, var(--color-success) 14%, transparent);
      }
      .delta.down {
        color: var(--color-danger);
        background: color-mix(in srgb, var(--color-danger) 14%, transparent);
      }
      .caption {
        font-size: var(--text-xs);
        color: var(--text-muted);
      }
      .spark {
        width: 84px;
        height: 28px;
        margin-left: auto;
      }
    `,
  ],
})
export class KpiCardComponent {
  @Input({ required: true }) label = "";
  @Input({ required: true }) value: string | number = "";
  @Input() icon?: string;
  @Input() delta?: number;
  @Input() deltaSuffix = "%";
  @Input() caption?: string;
  /** when true, a positive delta is bad (e.g. latency) and shows red */
  @Input() invertColor = false;
  @Input() spark: number[] = [];

  readonly sparkW = 100;
  readonly sparkH = 28;

  get isPositive() {
    const up = (this.delta ?? 0) >= 0;
    return this.invertColor ? !up : up;
  }
  get deltaAbs() {
    return Math.abs(this.delta ?? 0);
  }

  get sparkPoints(): string {
    if (this.spark.length < 2) return "";
    const min = Math.min(...this.spark),
      max = Math.max(...this.spark);
    const range = max - min || 1;
    const step = this.sparkW / (this.spark.length - 1);
    const pad = 3;
    return this.spark
      .map((v, i) => {
        const x = i * step;
        const y =
          this.sparkH - pad - ((v - min) / range) * (this.sparkH - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }
}
