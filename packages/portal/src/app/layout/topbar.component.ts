import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { ActivatedRoute, NavigationEnd, Router } from "@angular/router";
import { filter } from "rxjs/operators";
import { IconComponent } from "../shared/ui/icon.component";
import { ThemeService } from "../shared/theme.service";
import { AuthService } from "../auth/auth.service";
import { AssistantService } from "../assistant/assistant.service";
import { ActiveEnvironmentService } from "../shared/active-environment.service";
import { PlatformHealthService } from "../shared/platform-health.service";
import {
  NetworkAlert,
  NetworkAlertsService,
} from "../shared/network-alerts.service";

@Component({
  selector: "pp-topbar",
  standalone: true,
  imports: [IconComponent],
  template: `
    <header class="topbar">
      <div class="left">
        <h1 class="page-title">{{ title() }}</h1>
      </div>

      <div class="search">
        <span class="s-ico"><pp-icon name="search" [size]="18" /></span>
        <input type="text" placeholder="Search…" aria-label="Search" />
        <kbd>⌘K</kbd>
      </div>

      <div class="actions">
        <div class="envpick">
          <button
            class="env__btn"
            (click)="envOpen.set(!envOpen())"
            [attr.aria-expanded]="envOpen()"
            title="Active environment"
          >
            <pp-icon name="layers" [size]="16" />
            <span class="env__name">{{
              env.active() || "No environment"
            }}</span>
            @if (env.overridden()) {
              <span
                class="env__ovr"
                title="Session override — not the saved default"
                >session</span
              >
            }
            <pp-icon name="chevron" [size]="14" />
          </button>
          @if (envOpen()) {
            <div class="menu env__menu" (mouseleave)="envOpen.set(false)">
              <div class="menu__head">
                <div class="menu__name">Environment</div>
                <div class="menu__roles">
                  default · {{ env.defaultEnv() || "none" }}
                </div>
              </div>
              @for (e of env.options(); track e.name) {
                <button class="menu__item" (click)="pickEnv(e.name)">
                  <pp-icon
                    name="check"
                    [size]="14"
                    [class.env__hide]="e.name !== env.active()"
                  />
                  {{ e.label || e.name }}
                </button>
              } @empty {
                <div
                  class="menu__roles"
                  style="padding: var(--space-2) var(--space-3)"
                >
                  No environments yet.
                </div>
              }
              <div class="env__sep"></div>
              <button class="menu__item" (click)="setDefault()">
                <pp-icon name="shield" [size]="14" /> Set as platform default
              </button>
              @if (env.overridden()) {
                <button class="menu__item" (click)="clearOverride()">
                  <pp-icon name="undo" [size]="14" /> Clear session override
                </button>
              }
            </div>
          }
        </div>
        <button
          class="ibtn assistant"
          (click)="assistant.toggle()"
          [class.on]="assistant.open()"
          aria-label="Open assistant"
          title="Configuration assistant"
        >
          <pp-icon name="sparkles" [size]="18" />
        </button>
        <div class="alerts">
          <button
            class="ibtn"
            (click)="toggleAlerts()"
            [attr.aria-expanded]="alertsOpen()"
            [title]="
              alerts.unseen()
                ? alerts.unseen() + ' new network events'
                : 'Network events'
            "
            aria-label="Network events"
          >
            <pp-icon name="bell" [size]="18" />
            @if (alerts.unseen()) {
              <span class="abadge">{{
                alerts.unseen() > 9 ? "9+" : alerts.unseen()
              }}</span>
            }
          </button>
          @if (alertsOpen()) {
            <div class="menu menu--alerts" (mouseleave)="alertsOpen.set(false)">
              <div class="menu__head">
                <div class="menu__name">Network events</div>
                <div class="menu__roles">
                  node down · decline bursts · gone quiet
                </div>
              </div>
              @for (a of alerts.alerts(); track a.key) {
                <button class="menu__item aitem" (click)="openAlert(a)">
                  <span class="adot" [attr.data-tone]="a.tone"></span>
                  <span class="atext">{{ a.text }}</span>
                  <span class="atime">{{ a.time }}</span>
                </button>
              } @empty {
                <div class="menu__roles aempty">
                  Nothing yet — bursts of declines, listeners going down or
                  falling silent will show up here.
                </div>
              }
              <div class="asep"></div>
              <button class="menu__item" (click)="alerts.toggleNotify()">
                <pp-icon
                  [name]="alerts.notify() ? 'check' : 'bell'"
                  [size]="14"
                />
                Browser notifications {{ alerts.notify() ? "on" : "off" }}
              </button>
              @if (alerts.alerts().length) {
                <button class="menu__item" (click)="alerts.clear()">
                  <pp-icon name="x" [size]="14" /> Clear
                </button>
              }
            </div>
          }
        </div>
        <button
          class="ibtn"
          (click)="theme.toggle()"
          [attr.aria-label]="
            theme.theme() === 'dark' ? 'Switch to light' : 'Switch to dark'
          "
        >
          <pp-icon
            [name]="theme.theme() === 'dark' ? 'sun' : 'moon'"
            [size]="18"
          />
        </button>
        <button
          class="status"
          [attr.data-tone]="health.tone()"
          [title]="health.summary()"
          aria-label="Platform health — open diagnostics"
          (click)="openDiagnostics()"
        >
          <span class="status__dot"></span>
        </button>

        <div class="user">
          <button
            class="user__btn"
            (click)="menuOpen.set(!menuOpen())"
            [attr.aria-expanded]="menuOpen()"
            [title]="username()"
          >
            <span class="avatar">{{ initials() }}</span>
          </button>
          @if (menuOpen()) {
            <div class="menu" (mouseleave)="menuOpen.set(false)">
              <div class="menu__head">
                <div class="menu__name">{{ username() }}</div>
                @if (roles()) {
                  <div class="menu__roles">{{ roles() }}</div>
                }
              </div>
              <button class="menu__item" (click)="logout()">
                <pp-icon name="log-out" [size]="16" /> Sign out
              </button>
            </div>
          }
        </div>
      </div>
    </header>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./topbar.component.scss",
})
export class TopbarComponent implements OnInit {
  readonly title = signal("PayProbe");
  readonly theme = inject(ThemeService);
  readonly assistant = inject(AssistantService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  readonly env = inject(ActiveEnvironmentService);
  readonly health = inject(PlatformHealthService);
  readonly alerts = inject(NetworkAlertsService);
  readonly envOpen = signal(false);
  readonly alertsOpen = signal(false);

  readonly menuOpen = signal(false);
  readonly username = computed(() => this.auth.user()?.username ?? "Account");
  readonly roles = computed(() => (this.auth.user()?.roles ?? []).join(", "));
  readonly initials = computed(() => {
    const name = this.auth.user()?.username ?? "";
    return name.slice(0, 2).toUpperCase() || "PP";
  });

  pickEnv(name: string): void {
    this.env.select(name);
    this.envOpen.set(false);
  }
  setDefault(): void {
    this.env.pinActiveAsDefault();
    this.envOpen.set(false);
  }
  clearOverride(): void {
    this.env.clearOverride();
    this.envOpen.set(false);
  }

  logout(): void {
    this.menuOpen.set(false);
    this.auth.logout();
    this.router.navigate(["/login"]);
  }

  openDiagnostics(): void {
    this.router.navigate(["/diagnostics"]);
  }

  toggleAlerts(): void {
    this.alertsOpen.update((o) => !o);
    if (this.alertsOpen()) this.alerts.markSeen();
  }

  /** Jump to the live map focused on the node behind the alert. */
  openAlert(a: NetworkAlert): void {
    this.alertsOpen.set(false);
    this.router.navigate(["/network-map"], {
      queryParams: a.nodeId ? { focus: a.nodeId } : {},
    });
  }

  ngOnInit() {
    this.health.start();
    this.alerts.start();
    this.updateTitle();
    this.router.events
      .pipe(filter((e) => e instanceof NavigationEnd))
      .subscribe(() => this.updateTitle());
  }

  private updateTitle() {
    let r = this.route.firstChild;
    while (r?.firstChild) r = r.firstChild;
    const t = r?.snapshot.data?.["title"];
    this.title.set((t as string) ?? "PayProbe");
  }
}
