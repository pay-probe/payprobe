import {
  Component,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { NavigationEnd, Router, RouterOutlet } from "@angular/router";
import { filter } from "rxjs/operators";

import { UiOverlayComponent } from "./shared/ui-overlay.component";
import { SidenavComponent } from "./layout/sidenav.component";
import { TopbarComponent } from "./layout/topbar.component";
import { AssistantPanelComponent } from "./assistant/assistant-panel.component";

@Component({
  selector: "app-root",
  standalone: true,
  imports: [
    RouterOutlet,
    UiOverlayComponent,
    SidenavComponent,
    TopbarComponent,
    AssistantPanelComponent,
  ],
  template: `
    @if (showShell()) {
      <div class="pp-shell">
        <pp-sidenav />
        <div class="pp-main">
          @if (showTopbar()) {
            <pp-topbar />
          }
          <main class="pp-content"><router-outlet /></main>
        </div>
      </div>
      <pp-assistant-panel />
    } @else {
      <router-outlet />
    }
    <app-ui-overlay />
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .pp-shell {
        display: flex;
        height: 100vh;
        overflow: hidden;
      }
      .pp-main {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
      }
      .pp-content {
        flex: 1;
        min-height: 0;
        overflow: auto;
      }
    `,
  ],
})
export class AppComponent {
  private readonly router = inject(Router);
  /** The constructor editor is a full-bleed canvas with its own toolbar. */
  private readonly currentUrl = signal(this.router.url);

  readonly showTopbar = computed(() => {
    const url = this.currentUrl();
    return !url.startsWith("/constructor/");
  });

  /** The login page is full-bleed — no sidenav/topbar chrome. */
  readonly showShell = computed(() => !this.currentUrl().startsWith("/login"));

  constructor() {
    this.router.events
      .pipe(filter((e) => e instanceof NavigationEnd))
      .subscribe((e) =>
        this.currentUrl.set((e as NavigationEnd).urlAfterRedirects),
      );
  }
}
