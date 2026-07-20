import { Component, Input } from "@angular/core";

export type BadgeTone =
  | "brand"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "neutral";

@Component({
  selector: "pp-badge",
  standalone: true,
  template: `<span class="badge" [class]="'t-' + tone"
    ><span class="dot"></span><ng-content
  /></span>`,
  styles: [
    `
      .badge {
        display: inline-flex;
        align-items: center;
        gap: var(--space-1-5);
        font-size: var(--text-xs);
        font-weight: var(--weight-medium);
        line-height: 1;
        padding: var(--space-1) var(--space-2-5);
        border-radius: var(--radius-full);
      }
      .dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: currentColor;
        opacity: 0.9;
      }
      .t-brand {
        background: color-mix(in srgb, var(--brand) 14%, transparent);
        color: var(--brand);
      }
      .t-success {
        background: color-mix(in srgb, var(--color-success) 16%, transparent);
        color: var(--color-success);
      }
      .t-warning {
        background: color-mix(in srgb, var(--color-warning) 18%, transparent);
        color: var(--color-warning);
      }
      .t-danger {
        background: color-mix(in srgb, var(--color-danger) 16%, transparent);
        color: var(--color-danger);
      }
      .t-info {
        background: color-mix(in srgb, var(--color-info) 16%, transparent);
        color: var(--color-info);
      }
      .t-neutral {
        background: var(--bg-muted);
        color: var(--text-secondary);
      }
    `,
  ],
})
export class BadgeComponent {
  @Input() tone: BadgeTone = "neutral";
}
