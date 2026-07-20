import { Component, Input, ChangeDetectionStrategy } from "@angular/core";

/**
 * Shared page header — the consistent toolbar that sits directly under the app
 * topbar on every routed page. The topbar owns the page *title*; this bar carries
 * an optional one-line description on the left and page actions on the right.
 *
 * Usage:
 *   <pp-page-header subtitle="Short description of the page">
 *     <button class="btn btn--primary">+ New thing</button>
 *   </pp-page-header>
 *
 * For rich subtitles (markup, computed status, breadcrumbs) project into the
 * `[subtitle]` slot instead of using the `subtitle` input:
 *   <pp-page-header>
 *     <nav subtitle>…breadcrumb…</nav>
 *     <button>…action…</button>
 *   </pp-page-header>
 *
 * Anything not matched by `[subtitle]` is projected into the right-aligned
 * actions area, so existing buttons / inputs / selects drop straight in.
 */
@Component({
  selector: "pp-page-header",
  standalone: true,
  template: `
    <header class="pp-pageheader">
      <div class="pp-pageheader__lead">
        <ng-content select="[subtitle]" />
        @if (subtitle) {
          <p class="pp-pageheader__sub">{{ subtitle }}</p>
        }
      </div>
      <div class="pp-pageheader__actions">
        <ng-content />
      </div>
    </header>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
        flex: none;
      }
      .pp-pageheader {
        display: flex;
        align-items: center;
        gap: var(--space-4);
        min-height: 52px;
        padding: var(--space-3) var(--space-6);
        border-bottom: 1px solid var(--border);
        background: var(--bg-base);
      }
      .pp-pageheader__lead {
        min-width: 0;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 2px;
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .pp-pageheader__sub {
        margin: 0;
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .pp-pageheader__actions {
        margin-left: auto;
        display: flex;
        align-items: center;
        gap: var(--space-2);
      }
    `,
  ],
})
export class PageHeaderComponent {
  @Input() subtitle?: string;
}
