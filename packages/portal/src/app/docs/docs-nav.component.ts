import { Component, ChangeDetectionStrategy } from "@angular/core";
import { RouterLink, RouterLinkActive } from "@angular/router";

/**
 * Secondary navigation for the Docs section — a tab strip shared by every docs
 * page. Keeps the global sidenav to a single "Docs" entry while presenting the
 * individual doc pages as a proper, scannable section instead of buried nav
 * leaves. Add a new doc page → add a tab here.
 */
@Component({
  selector: "app-docs-nav",
  standalone: true,
  imports: [RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="doctabs" aria-label="Documentation sections">
      <a
        routerLink="/docs"
        routerLinkActive="on"
        [routerLinkActiveOptions]="{ exact: true }"
        >Overview</a
      >
      <a routerLink="/docs/payshield" routerLinkActive="on"
        >payShield 10K HSM</a
      >
    </nav>
  `,
  styles: [
    `
      .doctabs {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
        padding: 0 0 4px;
        border-bottom: 1px solid var(--color-border);
        margin-bottom: 4px;
      }
      .doctabs a {
        font-size: 13px;
        font-weight: 600;
        text-decoration: none;
        color: var(--color-text-muted, var(--color-text));
        padding: 8px 12px;
        border-radius: 8px 8px 0 0;
        border-bottom: 2px solid transparent;
        margin-bottom: -5px;
      }
      .doctabs a:hover {
        color: var(--color-text);
        background: var(--color-node, transparent);
      }
      .doctabs a.on {
        color: var(--color-primary, #58a6ff);
        border-bottom-color: var(--color-primary, #58a6ff);
      }
    `,
  ],
})
export class DocsNavComponent {}
