import { Component, Input, ChangeDetectionStrategy } from "@angular/core";
import { IconComponent } from "./icon.component";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

@Component({
  selector: "pp-button",
  standalone: true,
  imports: [IconComponent],
  template: `
    <button
      class="btn"
      [class]="'v-' + variant + ' s-' + size"
      [disabled]="disabled"
      [attr.type]="type"
    >
      @if (icon) {
        <pp-icon [name]="icon" [size]="iconSize" />
      }
      <ng-content />
    </button>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: var(--space-2);
        font-family: inherit;
        font-weight: var(--weight-semibold);
        border: 1px solid transparent;
        border-radius: var(--radius-md);
        cursor: pointer;
        white-space: nowrap;
        transition:
          background var(--duration-fast) var(--ease-out),
          border-color var(--duration-fast) var(--ease-out),
          color var(--duration-fast) var(--ease-out),
          box-shadow var(--duration-fast) var(--ease-out);
      }
      .btn:focus-visible {
        outline: none;
        box-shadow: 0 0 0 3px var(--focus-ring);
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .s-sm {
        font-size: var(--text-sm);
        padding: var(--space-1-5) var(--space-3);
      }
      .s-md {
        font-size: var(--text-sm);
        padding: var(--space-2-5) var(--space-4);
      }
      .s-lg {
        font-size: var(--text-base);
        padding: var(--space-3) var(--space-5);
      }
      .v-primary {
        background: var(--brand);
        color: var(--text-on-brand);
        box-shadow: var(--shadow-sm);
      }
      .v-primary:hover:not(:disabled) {
        background: var(--brand-hover);
      }
      .v-primary:active:not(:disabled) {
        background: var(--brand-active);
      }
      .v-secondary {
        background: var(--bg-subtle);
        color: var(--text-primary);
        border-color: var(--border-strong);
      }
      .v-secondary:hover:not(:disabled) {
        border-color: var(--brand);
        color: var(--brand);
      }
      .v-ghost {
        background: transparent;
        color: var(--brand);
      }
      .v-ghost:hover:not(:disabled) {
        background: color-mix(in srgb, var(--brand) 10%, transparent);
      }
      .v-danger {
        background: var(--color-danger);
        color: #fff;
      }
      .v-danger:hover:not(:disabled) {
        filter: brightness(0.93);
      }
    `,
  ],
})
export class ButtonComponent {
  @Input() variant: ButtonVariant = "primary";
  @Input() size: ButtonSize = "md";
  @Input() icon?: string;
  @Input() disabled = false;
  @Input() type: "button" | "submit" = "button";
  get iconSize() {
    return this.size === "sm" ? 16 : this.size === "lg" ? 20 : 18;
  }
}
