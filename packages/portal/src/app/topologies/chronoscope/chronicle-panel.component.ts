import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";

import { Evt } from "./chronoscope.model";

/**
 * The chronicle: notable network events (downs, recoveries, decline bursts,
 * went-quiet, joins/leaves), newest first. Clicking one asks the parent to
 * jump the time machine to that moment.
 */
@Component({
  selector: "app-chrono-chronicle",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="panel">
      <div class="panel__head">Chronicle</div>
      @if (!events().length) {
        <p class="muted sm panel__hint">
          Nothing notable yet. Downs, recoveries, decline bursts and joins land
          here — click one to jump back to it.
        </p>
      } @else {
        <ul class="chron">
          @for (e of recent(); track e.key) {
            <li class="chron__row" [attr.data-tone]="e.tone">
              <button class="chron__btn" (click)="jump.emit(e)">
                <span class="chron__dot"></span>
                <span class="chron__txt">{{ e.text }}</span>
                <span class="chron__t mono">{{ e.time }}</span>
              </button>
            </li>
          }
        </ul>
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

      .chron {
        list-style: none;
        margin: 0;
        padding: 0;
        max-height: 260px;
        overflow: auto;
      }
      .chron__btn {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        width: 100%;
        text-align: left;
        border: none;
        background: transparent;
        color: var(--text-primary);
        cursor: pointer;
        padding: var(--space-1-5) var(--space-1);
        border-radius: var(--radius-sm);
        font-size: var(--text-xs);
      }
      .chron__btn:hover {
        background: var(--bg-subtle);
      }
      .chron__dot {
        flex: none;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--text-muted);
      }
      .chron__row[data-tone="ok"] .chron__dot {
        background: var(--color-success);
      }
      .chron__row[data-tone="warn"] .chron__dot {
        background: var(--color-warning);
      }
      .chron__row[data-tone="bad"] .chron__dot {
        background: var(--color-danger);
      }
      .chron__row[data-tone="info"] .chron__dot {
        background: var(--brand);
      }
      .chron__txt {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .chron__t {
        flex: none;
        color: var(--text-muted);
        font-size: 10px;
      }
    `,
  ],
})
export class ChroniclePanelComponent {
  readonly events = input.required<Evt[]>();
  readonly jump = output<Evt>();

  readonly recent = computed(() => [...this.events()].reverse().slice(0, 30));
}
