import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { Router, RouterLink, RouterLinkActive } from "@angular/router";
import { AuthService } from "../auth/auth.service";
import { NAV, NavItem } from "../core/nav.model";
import { IconComponent } from "../shared/ui/icon.component";

@Component({
  selector: "pp-sidenav",
  standalone: true,
  imports: [RouterLink, RouterLinkActive, IconComponent],
  template: `
    <aside class="rail" [class.collapsed]="collapsed()">
      <a class="brand" routerLink="/dashboard">
        <span class="logo">P</span>
        @if (!collapsed()) {
          <span class="brand-name">PayProbe</span>
        }
      </a>

      <nav class="nav">
        @for (item of nav(); track item.label) {
          @if (item.route) {
            <a
              class="item"
              [routerLink]="item.route"
              routerLinkActive="active"
              [routerLinkActiveOptions]="{ exact: false }"
              [attr.title]="collapsed() ? item.label : null"
              [style.--nav-accent]="item.accent"
            >
              <span class="item-ico"><pp-icon [name]="item.icon" /></span>
              @if (!collapsed()) {
                <span class="lbl">{{ item.label }}</span>
              }
            </a>
          } @else {
            <div
              class="group"
              [class.open]="isOpen(item)"
              [class.active]="isGroupActive(item)"
              [style.--nav-accent]="item.accent"
            >
              <button
                class="item grp-btn"
                (click)="toggle(item)"
                [attr.title]="collapsed() ? item.label : null"
                [attr.aria-expanded]="isOpen(item)"
              >
                <span class="item-ico"><pp-icon [name]="item.icon" /></span>
                @if (!collapsed()) {
                  <span class="lbl">{{ item.label }}</span>
                  <span class="chev"
                    ><pp-icon name="chevron" [size]="16"
                  /></span>
                }
              </button>

              @if (!collapsed()) {
                <div
                  class="children"
                  [style.maxHeight]="
                    isOpen(item) ? childHeight(item) + 'px' : '0'
                  "
                >
                  @for (c of item.children; track c.route) {
                    <a
                      class="child"
                      [routerLink]="c.route"
                      routerLinkActive="active"
                    >
                      @if (c.icon) {
                        <span class="child-ico"
                          ><pp-icon [name]="c.icon" [size]="15"
                        /></span>
                      } @else {
                        <span class="tick"></span>
                      }
                      <span class="lbl">{{ c.label }}</span>
                      @if (c.badge) {
                        <span class="cbadge">{{ c.badge }}</span>
                      }
                    </a>
                  }
                </div>
              } @else {
                <div class="flyout">
                  <div class="flyhead">{{ item.label }}</div>
                  @for (c of item.children; track c.route) {
                    <a
                      class="child"
                      [routerLink]="c.route"
                      routerLinkActive="active"
                    >
                      @if (c.icon) {
                        <span class="child-ico"
                          ><pp-icon [name]="c.icon" [size]="15"
                        /></span>
                      }
                      {{ c.label }}
                      @if (c.badge) {
                        <span class="cbadge">{{ c.badge }}</span>
                      }
                    </a>
                  }
                </div>
              }
            </div>
          }
        }
      </nav>

      <button
        class="collapse-btn"
        (click)="collapsed.set(!collapsed())"
        [attr.aria-label]="collapsed() ? 'Expand sidebar' : 'Collapse sidebar'"
      >
        <span class="cico" [class.flip]="collapsed()"
          ><pp-icon name="chevron" [size]="18"
        /></span>
        @if (!collapsed()) {
          <span class="lbl">Collapse</span>
        }
      </button>
    </aside>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./sidenav.component.scss",
})
export class SidenavComponent implements OnInit {
  private readonly auth = inject(AuthService);
  /** Nav filtered by role: admin-only items/children hide for non-admins. */
  readonly nav = computed<NavItem[]>(() => {
    const isAdmin = (this.auth.user()?.roles ?? []).includes("admin");
    return NAV.filter((i) => !i.adminOnly || isAdmin).map((i) =>
      i.children
        ? { ...i, children: i.children.filter((c) => !c.adminOnly || isAdmin) }
        : i,
    );
  });
  readonly collapsed = signal(false);
  // Start with every group expanded; the user can still collapse one via toggle.
  private readonly open = signal<Set<string>>(
    new Set(NAV.filter((i) => i.children?.length).map((i) => i.label)),
  );

  constructor(private router: Router) {}

  ngOnInit() {
    // keep all groups open by default; nothing to resolve from the URL
  }

  isOpen(item: NavItem) {
    return this.open().has(item.label);
  }

  toggle(item: NavItem) {
    const next = new Set(this.open());
    next.has(item.label) ? next.delete(item.label) : next.add(item.label);
    this.open.set(next);
  }

  isGroupActive(item: NavItem) {
    return !!item.children?.some((c) => this.router.url.startsWith(c.route));
  }

  childHeight(item: NavItem) {
    return (item.children?.length ?? 0) * 38 + 8;
  }
}
