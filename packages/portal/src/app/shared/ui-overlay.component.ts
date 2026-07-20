import { Component, inject, ChangeDetectionStrategy } from "@angular/core";

import { UiService } from "./ui.service";

/** Renders toasts and the confirm dialog. Mounted once in AppComponent. */
@Component({
  selector: "app-ui-overlay",
  standalone: true,
  imports: [],
  template: `
    <div class="toasts">
      @for (t of ui.toasts(); track t.id) {
        <div
          class="toast"
          [class.toast--error]="t.kind === 'error'"
          (click)="ui.dismiss(t.id)"
        >
          {{ t.kind === "success" ? "✓" : "✕" }} {{ t.text }}
        </div>
      }
    </div>

    @if (ui.confirmRequest(); as req) {
      <div class="confirm-backdrop" (click)="ui.resolveConfirm(false)"></div>
      <div class="confirm" role="dialog" aria-modal="true">
        <h3>{{ req.title }}</h3>
        <p>{{ req.message }}</p>
        <div class="confirm__actions">
          <button class="btn" (click)="ui.resolveConfirm(false)">Cancel</button>
          <button
            class="btn"
            [class.btn--danger]="req.danger"
            [class.btn--primary]="!req.danger"
            (click)="ui.resolveConfirm(true)"
          >
            {{ req.confirmLabel }}
          </button>
        </div>
      </div>
    }
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .toasts {
        position: fixed;
        bottom: var(--space-5);
        right: var(--space-5);
        display: flex;
        flex-direction: column;
        gap: var(--space-2);
        z-index: var(--z-toast);
      }

      .toast {
        background: var(--bg-surface);
        border: 1px solid
          color-mix(in srgb, var(--color-success) 50%, transparent);
        color: var(--color-success);
        padding: var(--space-3) var(--space-4);
        border-radius: var(--radius-md);
        font-size: var(--text-sm);
        font-family: var(--font-sans);
        cursor: pointer;
        box-shadow: var(--shadow-lg);
      }

      .toast--error {
        border-color: color-mix(in srgb, var(--color-danger) 55%, transparent);
        color: var(--color-danger);
      }

      .confirm-backdrop {
        position: fixed;
        inset: 0;
        background: var(--backdrop);
        z-index: var(--z-modal);
      }

      .confirm {
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        width: 380px;
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-5);
        z-index: var(--z-modal);
        color: var(--text-primary);
        font-family: var(--font-sans);
        box-shadow: var(--shadow-lg);
      }

      .confirm h3 {
        margin: 0 0 var(--space-2);
        font-size: var(--text-md);
      }

      .confirm p {
        margin: 0 0 var(--space-5);
        font-size: var(--text-sm);
        color: var(--text-secondary);
        white-space: pre-line;
      }

      .confirm__actions {
        display: flex;
        justify-content: flex-end;
        gap: var(--space-2);
      }

      .btn {
        padding: 7px var(--space-4);
        border: 1px solid var(--border-strong);
        border-radius: var(--radius-md);
        background: var(--bg-subtle);
        color: var(--text-primary);
        font-size: var(--text-sm);
        font-weight: var(--weight-semibold);
        cursor: pointer;
      }

      .btn:hover {
        border-color: var(--brand);
      }

      .btn:focus-visible {
        outline: 2px solid transparent;
        box-shadow: 0 0 0 3px var(--focus-ring);
      }

      .btn--primary {
        background: var(--brand);
        border-color: transparent;
        color: var(--on-brand);
      }

      .btn--danger {
        background: color-mix(in srgb, var(--color-danger) 15%, transparent);
        border-color: color-mix(in srgb, var(--color-danger) 55%, transparent);
        color: var(--color-danger);
      }
    `,
  ],
})
export class UiOverlayComponent {
  readonly ui = inject(UiService);
}
