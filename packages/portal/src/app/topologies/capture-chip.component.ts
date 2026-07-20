import {
  Component,
  OnDestroy,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";

import { ParticipantsService } from "../participants/participants.service";
import { IconComponent } from "../shared/ui/icon.component";

/**
 * Trace-capture awareness chip for the map bars. Capture is OFF by default
 * (buffer churn under load), so an operator watching live traffic on the map
 * can reasonably assume it's being recorded — and then find Network Trace
 * empty. This chip surfaces the gate where the traffic is being watched and
 * offers the one-click fix. Renders nothing while capture is on or unknown.
 */
@Component({
  selector: "app-capture-chip",
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (capturing() === false) {
      <span
        class="chip"
        title="Listeners keep serving — only trace recording is off"
      >
        <pp-icon name="alert-circle" [size]="13" />
        Trace capture off
        <button
          type="button"
          class="chip__btn"
          (click)="resume()"
          [disabled]="busy()"
        >
          {{ busy() ? "…" : "Resume" }}
        </button>
      </span>
    }
  `,
  styles: [
    `
      :host {
        display: inline-flex;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: var(--text-xs);
        font-weight: var(--weight-medium);
        color: var(--color-warning);
        background: color-mix(in srgb, var(--color-warning) 12%, transparent);
        border-radius: var(--radius-full);
        padding: 2px var(--space-2) 2px var(--space-2-5);
      }
      .chip pp-icon {
        flex: none;
      }
      .chip__btn {
        border: none;
        cursor: pointer;
        font-size: var(--text-xs);
        font-weight: var(--weight-semibold);
        color: var(--bg-surface);
        background: var(--color-warning);
        border-radius: var(--radius-full);
        padding: 1px var(--space-2);
      }
      .chip__btn:hover {
        filter: brightness(1.08);
      }
      .chip__btn:disabled {
        opacity: 0.6;
        cursor: default;
      }
    `,
  ],
})
export class CaptureChipComponent implements OnInit, OnDestroy {
  private readonly svc = inject(ParticipantsService);

  /** null until known — the chip stays hidden rather than guessing. */
  readonly capturing = signal<boolean | null>(null);
  readonly busy = signal(false);
  private poll: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.refresh();
    // modest re-check so a gate flipped elsewhere (trace page, MCP tool,
    // another tab) is reflected here without a reload.
    this.poll = setInterval(() => this.refresh(), 15000);
  }
  ngOnDestroy(): void {
    if (this.poll) clearInterval(this.poll);
  }

  refresh(): void {
    this.svc.captureState().subscribe({
      next: (s) => this.capturing.set(s.capturing),
      error: () => this.capturing.set(null),
    });
  }

  resume(): void {
    this.busy.set(true);
    this.svc.setCapture(true).subscribe({
      next: (s) => {
        this.capturing.set(s.capturing);
        this.busy.set(false);
      },
      error: () => {
        this.busy.set(false);
        this.refresh();
      },
    });
  }
}
