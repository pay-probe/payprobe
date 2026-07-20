import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  input,
  output,
} from "@angular/core";

import { STRIP_H, STRIP_W, StripMark } from "./chronoscope.model";

/**
 * The time machine's transport: play/pause + speed + frame-step controls and
 * the scrub strip (throughput backdrop, event marker dots with tooltips, and
 * the playhead). Emits a normalized 0..1 position on scrub — the parent maps
 * that to a frame.
 */
@Component({
  selector: "app-chrono-scrubber",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="strip">
      <div class="strip__ctl">
        <button
          class="tbtn"
          (click)="step.emit(-1)"
          aria-label="Back one frame"
        >
          ‹
        </button>
        <button
          class="tbtn tbtn--play"
          (click)="playToggle.emit()"
          [attr.aria-label]="playing() ? 'Pause replay' : 'Play replay'"
        >
          {{ playing() ? "❚❚" : "▶" }}
        </button>
        <button
          class="tbtn"
          (click)="step.emit(1)"
          aria-label="Forward one frame"
        >
          ›
        </button>
        <button
          class="tbtn tbtn--speed"
          (click)="speedCycle.emit()"
          aria-label="Playback speed"
        >
          {{ speed() }}×
        </button>
        <span class="muted sm">replay</span>
        <span class="spacer"></span>
        <span class="muted sm strip__keys"
          >← → step · space play · L live · esc clear</span
        >
      </div>
      <svg
        [attr.viewBox]="'0 0 ' + STRIP_W + ' ' + STRIP_H"
        preserveAspectRatio="none"
        class="strip__svg"
        (pointerdown)="onDown($event)"
        xmlns="http://www.w3.org/2000/svg"
      >
        <polygon [attr.points]="area()" class="strip__area" />
        <polyline [attr.points]="line()" class="strip__line" />
        @for (m of marks(); track m.key) {
          <circle
            [attr.cx]="m.x"
            cy="8"
            r="3"
            class="strip__evt"
            [attr.data-tone]="m.tone"
          >
            <title>{{ m.text }}</title>
          </circle>
        }
        <line
          [attr.x1]="playheadX()"
          [attr.x2]="playheadX()"
          y1="0"
          [attr.y2]="STRIP_H"
          class="strip__head"
        />
      </svg>
      <div class="strip__lbl">
        <span class="muted sm">−{{ spanLabel() }}</span>
        <span class="muted sm"
          >drag to rewind · hover the dots · chronicle jumps back</span
        >
        <span class="sm" [class.muted]="!live()" [class.strip__live]="live()">{{
          endLabel()
        }}</span>
      </div>
    </div>
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
      .spacer {
        flex: 1;
      }

      .strip {
        margin-top: var(--space-4);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-2) var(--space-3);
        box-shadow: var(--shadow-xs);
        touch-action: none;
        user-select: none;
      }
      .strip__svg {
        display: block;
        width: 100%;
        height: 44px;
        cursor: ew-resize;
      }
      .strip__ctl {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-1-5);
      }
      .tbtn {
        display: inline-grid;
        place-items: center;
        min-width: 26px;
        height: 24px;
        padding: 0 6px;
        border: 1px solid var(--border);
        background: var(--bg-surface);
        color: var(--text-secondary);
        border-radius: var(--radius-sm);
        cursor: pointer;
        font-size: 12px;
        line-height: 1;
      }
      .tbtn:hover {
        background: var(--bg-subtle);
        color: var(--text-primary);
      }
      .tbtn--play {
        color: var(--brand);
        font-size: 10px;
        min-width: 32px;
      }
      .tbtn--speed {
        font-variant-numeric: tabular-nums;
      }
      .strip__keys {
        font-size: var(--text-xs);
      }
      .strip__area {
        fill: var(--color-info, #38bdf8);
        opacity: 0.13;
      }
      .strip__line {
        fill: none;
        stroke: var(--color-info, #38bdf8);
        stroke-width: 1.5;
        vector-effect: non-scaling-stroke;
      }
      .strip__evt {
        stroke: var(--bg-surface);
        stroke-width: 1;
      }
      .strip__evt[data-tone="ok"] {
        fill: var(--color-success);
      }
      .strip__evt[data-tone="warn"] {
        fill: var(--color-warning);
      }
      .strip__evt[data-tone="bad"] {
        fill: var(--color-danger);
      }
      .strip__evt[data-tone="info"] {
        fill: var(--brand);
      }
      .strip__head {
        stroke: var(--brand);
        stroke-width: 2;
        vector-effect: non-scaling-stroke;
      }
      .strip__lbl {
        display: flex;
        justify-content: space-between;
        margin-top: 2px;
      }
      .strip__live {
        color: var(--color-success);
        font-weight: var(--weight-medium);
      }
    `,
  ],
})
export class TimeScrubberComponent {
  readonly STRIP_W = STRIP_W;
  readonly STRIP_H = STRIP_H;

  /** Throughput backdrop, precomputed by the parent (points strings). */
  readonly line = input.required<string>();
  readonly area = input.required<string>();
  readonly marks = input.required<StripMark[]>();
  readonly playheadX = input.required<number>();
  readonly spanLabel = input.required<string>();
  /** At the right edge (tracking live / end of file). */
  readonly live = input.required<boolean>();
  /** Right-edge caption: "live" for a live tape, "end" for a loaded file. */
  readonly endLabel = input("live");
  readonly playing = input.required<boolean>();
  readonly speed = input.required<number>();

  readonly scrub = output<number>(); // normalized 0..1
  readonly step = output<number>();
  readonly playToggle = output<void>();
  readonly speedCycle = output<void>();

  private scrubbing = false;
  private rect: DOMRect | null = null;

  onDown(ev: PointerEvent) {
    const el = ev.currentTarget as Element;
    this.rect = el.getBoundingClientRect();
    this.scrubbing = true;
    this.emitAt(ev.clientX);
  }
  @HostListener("document:pointermove", ["$event"])
  onDocMove(ev: PointerEvent) {
    if (this.scrubbing) this.emitAt(ev.clientX);
  }
  @HostListener("document:pointerup")
  onDocUp() {
    this.scrubbing = false;
    this.rect = null;
  }
  private emitAt(clientX: number) {
    const r = this.rect;
    if (!r || r.width <= 0) return;
    this.scrub.emit(Math.min(1, Math.max(0, (clientX - r.left) / r.width)));
  }
}
