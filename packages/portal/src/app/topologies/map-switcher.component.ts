import { Component, ChangeDetectionStrategy } from "@angular/core";
import { RouterLink, RouterLinkActive } from "@angular/router";

import { IconComponent } from "../shared/ui/icon.component";

/**
 * Segmented switcher between the three network surfaces (ATLAS §12 item 4:
 * cross-link them so they feel like views of one system, not three pages).
 * Manage → watch → replay, one click apart, identical on every bar.
 */
@Component({
  selector: "app-map-switcher",
  standalone: true,
  imports: [RouterLink, RouterLinkActive, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="sw" aria-label="Network views">
      <a
        class="sw__it"
        routerLink="/topology-map"
        routerLinkActive="sw__it--on"
      >
        <pp-icon name="server" [size]="13" /> Control
      </a>
      <a class="sw__it" routerLink="/network-map" routerLinkActive="sw__it--on">
        <pp-icon name="network" [size]="13" /> Live map
      </a>
      <a
        class="sw__it"
        routerLink="/topology-map-3"
        routerLinkActive="sw__it--on"
      >
        <pp-icon name="clock" [size]="13" /> Replay
      </a>
    </nav>
  `,
  styles: [
    `
      :host {
        display: inline-flex;
      }
      .sw {
        display: inline-flex;
        align-items: stretch;
        border: 1px solid var(--border);
        border-radius: var(--radius-full);
        background: var(--bg-base);
        padding: 2px;
        gap: 2px;
      }
      .sw__it {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px var(--space-3);
        border-radius: var(--radius-full);
        font-size: var(--text-xs);
        font-weight: var(--weight-medium);
        color: var(--text-muted);
        text-decoration: none;
        white-space: nowrap;
      }
      .sw__it:hover {
        color: var(--text-primary);
      }
      .sw__it--on {
        background: var(--bg-surface);
        color: var(--brand);
        box-shadow: var(--shadow-xs);
      }
      .sw__it pp-icon {
        color: currentColor;
      }
    `,
  ],
})
export class MapSwitcherComponent {}
