import { Component, Input } from "@angular/core";

@Component({
  selector: "pp-card",
  standalone: true,
  template: `
    @if (heading) {
      <header class="card-head">
        <div>
          <h3>{{ heading }}</h3>
          @if (subheading) {
            <p>{{ subheading }}</p>
          }
        </div>
        <ng-content select="[card-actions]" />
      </header>
    }
    <div class="card-body" [class.flush]="flush"><ng-content /></div>
  `,
  styles: [
    `
      :host {
        display: block;
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        box-shadow: var(--shadow-xs);
        overflow: hidden;
      }
      .card-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: var(--space-4);
        padding: var(--space-5) var(--space-5) 0;
      }
      .card-head h3 {
        font-size: var(--text-base);
      }
      .card-head p {
        margin: var(--space-1) 0 0;
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .card-body {
        padding: var(--space-5);
      }
      .card-body.flush {
        padding: 0;
      }
    `,
  ],
})
export class CardComponent {
  @Input() heading?: string;
  @Input() subheading?: string;
  @Input() flush = false;
}
