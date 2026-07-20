import { Component, Input, ChangeDetectionStrategy } from "@angular/core";

import { PollHealth } from "../poll-health";
import { IconComponent } from "./icon.component";

/**
 * Inline staleness indicator for any polling page — renders nothing while the
 * backend answers, and an amber "Stale" chip with the age of the data once
 * polls start failing. One component so every page tells the same truth the
 * same way (the maps pioneered the pattern; this is its portable form).
 */
@Component({
  selector: "pp-stale-chip",
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (health.stale()) {
      <span
        class="chip"
        role="alert"
        title="The backend stopped answering — retrying automatically"
      >
        <pp-icon name="alert-circle" [size]="13" />
        Stale
        @if (health.sinceOk() !== null) {
          <span class="age">· data {{ health.sinceOk() }}s old</span>
        } @else {
          <span class="age">· nothing received yet</span>
        }
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
        gap: 5px;
        font-size: var(--text-xs);
        font-weight: var(--weight-medium);
        color: var(--color-warning);
        background: color-mix(in srgb, var(--color-warning) 12%, transparent);
        border-radius: var(--radius-full);
        padding: 2px var(--space-2-5);
      }
      .chip pp-icon {
        flex: none;
      }
      .age {
        font-weight: var(--weight-regular);
      }
    `,
  ],
})
export class StaleChipComponent {
  @Input({ required: true }) health!: PollHealth;
}
